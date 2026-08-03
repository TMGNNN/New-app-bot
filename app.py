import os
import json
import base64
import hmac
import hashlib
import logging
import time
import io
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qsl

import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# --- LOGGING SETUP ---
log_handler = RotatingFileHandler('app.log', maxBytes=5*1024*1024, backupCount=3)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# --- ENVIRONMENT CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://your-mini-app-url.com")
ALLOWED_ORIGIN = os.environ.get("WEB_APP_URL", "*")

app = Flask(__name__)
# ፎቶዎችን እስከ 16MB መፍቀድ
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

CORS(app, resources={r"/api/*": {"origins": [ALLOWED_ORIGIN, "https://telegram.org", "*"]}})

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per day", "100 per hour"],
    storage_uri="memory://"
)

db_pool = None
if DATABASE_URL:
    try:
        db_pool = ThreadedConnectionPool(minconn=1, maxconn=20, dsn=DATABASE_URL)
        logger.info("✅ Database Connection Pool ተከፍቷል!")
    except Exception as e:
        logger.error(f"❌ DB Pool መክፈት አልተቻለም: {e}")

def get_db_connection():
    if not db_pool:
        raise Exception("DATABASE_URL አልተዋቀረም!")
    return db_pool.getconn()

def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

sse_clients = []

def notify_clients(event_data):
    dead_clients = []
    for q in sse_clients:
        try:
            q.append(event_data)
        except Exception:
            dead_clients.append(q)
    for dc in dead_clients:
        if dc in sse_clients:
            sse_clients.remove(dc)

def verify_telegram_data(init_data: str) -> bool:
    if not BOT_TOKEN or not init_data:
        return False
    try:
        parsed_data = dict(parse_qsl(init_data))
        if 'hash' not in parsed_data:
            return False
        
        hash_to_check = parsed_data.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(calculated_hash, hash_to_check)
    except Exception as e:
        logger.error(f"Telegram initData verification error: {e}")
        return False

def init_db():
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # 1. የተጠቃሚዎች ቴብል (ስልክ ቁጥርና መረጃ የሚቀመጥበት)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(50) PRIMARY KEY,
                first_name VARCHAR(100),
                username VARCHAR(100),
                phone_number VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # 2. የቲኬቶች ቴብል
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY,
                status VARCHAR(20) DEFAULT 'available',
                user_id VARCHAR(50),
                user_name VARCHAR(100),
                user_phone VARCHAR(50),
                referrer VARCHAR(100),
                receipt_file_id TEXT,
                price_paid NUMERIC(10, 2),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # 3. የReferrals ቴብል
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                user_id VARCHAR(50) PRIMARY KEY,
                referred_by VARCHAR(50),
                successful_invites INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id);")

        cursor.execute("SELECT COUNT(*) FROM tickets;")
        count = cursor.fetchone()['count']
        
        if count < 2200:
            tickets_data = [(i, 'available') for i in range(1, 2201)]
            cursor.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT (number) DO NOTHING", tickets_data)
            
        conn.commit()
        cursor.close()
        logger.info("✅ Database Initialization & Migration Successfully Completed!")
    except Exception as e:
        logger.error(f"❌ DB Init error: {e}")
    finally:
        if conn:
            release_db_connection(conn)

def cleanup_expired_pendings():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        fifteen_mins_ago = datetime.utcnow() - timedelta(minutes=15)
        
        cursor.execute("""
            UPDATE tickets 
            SET status = 'available', user_id = NULL, user_name = NULL, user_phone = NULL, referrer = NULL, receipt_file_id = NULL, price_paid = NULL
            WHERE status = 'pending' AND updated_at < %s
            RETURNING number;
        """, (fifteen_mins_ago,))
        
        released_rows = cursor.fetchall()
        conn.commit()
        
        if released_rows:
            released_numbers = [r['number'] for r in released_rows]
            notify_clients({"type": "UPDATE_NUMBERS", "numbers": released_numbers, "status": "available"})
            
        cursor.close()
    except Exception as e:
        logger.error(f"Error cleaning expired tickets: {e}")
    finally:
        if conn:
            release_db_connection(conn)

init_db()

def calculate_total_price(ticket_count):
    """የቅናሽ ዋጋ ስሌት (Server-side Validation)"""
    base_price = 3000
    total = ticket_count * base_price
    if ticket_count >= 5:
        total -= 1500
    elif ticket_count >= 3:
        total -= 500
    return max(total, 0)

def send_telegram_admin_notification(user_name, user_phone, selected_numbers, total_price, referrer, receipt_base64):
    if not (ADMIN_CHAT_ID and BOT_TOKEN):
        return None

    nums_str = ",".join(map(str, selected_numbers))
    caption = (
        f"🆕 **አዲስ የቲኬት ትዕዛዝ!**\n\n"
        f"👤 **ስም:** {user_name}\n"
        f"📞 **ስልክ:** {user_phone}\n"
        f"🎟️ **ቁጥሮች:** {nums_str}\n"
        f"💰 **ጠቅላላ ዋጋ:** {total_price:,} ብር\n"
        f"🔗 **የጋበዘው:** {referrer}"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ ፅድቅ (Approve)", "callback_data": f"app_{nums_str}"},
                {"text": "❌ ሰርዝ (Reject)", "callback_data": f"rej_{nums_str}"}
            ]
        ]
    }

    file_id = None
    try:
        if receipt_base64 and isinstance(receipt_base64, str) and "," in receipt_base64:
            header, encoded = receipt_base64.split(",", 1)
            image_data = base64.b64decode(encoded)
            files = {'photo': ('receipt.jpg', io.BytesIO(image_data), 'image/jpeg')}
            payload = {
                'chat_id': ADMIN_CHAT_ID,
                'caption': caption,
                'parse_mode': 'Markdown',
                'reply_markup': json.dumps(reply_markup)
            }
            res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=payload, files=files)
            res_json = res.json()
            if res_json.get('ok'):
                photos = res_json['result'].get('photo', [])
                if photos:
                    file_id = photos[-1]['file_id']
        else:
            payload = {
                'chat_id': ADMIN_CHAT_ID,
                'text': caption,
                'parse_mode': 'Markdown',
                'reply_markup': reply_markup
            }
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}")

    return file_id

def send_user_notification(user_id, text, reply_markup=None):
    if not BOT_TOKEN or not user_id:
        return
    payload = {
        "chat_id": user_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

# --- API ENDPOINTS ---

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

@app.route('/api/stream')
def stream():
    def event_stream():
        client_queue = []
        sse_clients.append(client_queue)
        last_ping = time.time()
        try:
            while True:
                if client_queue:
                    data = client_queue.pop(0)
                    yield f"data: {json.dumps(data)}\n\n"
                
                if time.time() - last_ping > 15:
                    yield ": ping\n\n"
                    last_ping = time.time()
                    
                time.sleep(1)
        except GeneratorExit:
            if client_queue in sse_clients:
                sse_clients.remove(client_queue)

    return Response(event_stream(), content_type='text/event-stream')

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    cleanup_expired_pendings()
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT number, status FROM tickets")
        rows = cursor.fetchall()
        cursor.close()
        return jsonify({row['number']: row['status'] for row in rows})
    except Exception as e:
        logger.error(f"Get tickets error: {e}")
        return jsonify({}), 500
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/get-user-info', methods=['GET'])
def get_user_info():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"success": False, "message": "User ID ያስፈልጋል"}), 400
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT phone_number, first_name FROM users WHERE user_id = %s", (str(user_id),))
        user = cursor.fetchone()
        cursor.close()
        
        if user:
            return jsonify({"success": True, "phone": user['phone_number'], "name": user['first_name']})
        return jsonify({"success": False, "message": "ተጠቃሚ አልተገኘም"}), 404
    except Exception as e:
        logger.error(f"Fetch user info error: {e}")
        return jsonify({"success": False, "message": "የሰርቨር ስህተት"}), 500
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/my-tickets', methods=['GET'])
def get_my_tickets():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify([]), 400
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT number, status, price_paid, updated_at 
            FROM tickets 
            WHERE user_id = %s AND status IN ('pending', 'sold')
            ORDER BY updated_at DESC
        """, (str(user_id),))
        rows = cursor.fetchall()
        cursor.close()
        return jsonify(rows)
    except Exception as e:
        logger.error(f"Error fetching user tickets: {e}")
        return jsonify([]), 500
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    data = request.json or {}
    init_data = data.get('initData')
    
    if init_data and not verify_telegram_data(init_data):
        return jsonify({"success": False, "message": "የቴሌግራም መረጃ ማረጋገጫ አልፈደም!"}), 403

    selected_numbers = data.get('numbers', [])
    user_id = str(data.get('user_id', ''))
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_base64 = data.get('receipt_base64')

    if not selected_numbers or not user_name or not user_phone:
        return jsonify({"success": False, "message": "እባክዎ ሁሉንም አስፈላጊ መረጃዎች ይሙሉ!"}), 400

    total_price = calculate_total_price(len(selected_numbers))
    price_per_ticket = total_price / len(selected_numbers)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute(
            "SELECT number FROM tickets WHERE number = ANY(%s) AND status != 'available'",
            (selected_numbers,)
        )
        taken = cursor.fetchall()

        if taken:
            taken_nums = [t['number'] for t in taken]
            cursor.close()
            return jsonify({"success": False, "message": f"ቁጥሮቹ ቀደም ብለው ተይዘዋል፡ {taken_nums}"}), 400

        file_id = send_telegram_admin_notification(user_name, user_phone, selected_numbers, total_price, referrer, receipt_base64)

        cursor.execute('''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s, receipt_file_id = %s, price_paid = %s, updated_at = CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        ''', (user_id, user_name, user_phone, referrer, file_id, price_per_ticket, selected_numbers))

        conn.commit()
        cursor.close()

        notify_clients({"type": "UPDATE_NUMBERS", "numbers": selected_numbers, "status": "pending"})

        return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል!"})
    except Exception as e:
        if conn: 
            conn.rollback()
        logger.error(f"Submit order error: {e}")
        return jsonify({"success": False, "message": f"የሰርቨር ስህተት፦ {str(e)}"}), 500
    finally:
        if conn: 
            release_db_connection(conn)

# --- TELEGRAM BOT WEBHOOK (START, PHONE SHARE & APPROVALS) ---
@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    update = request.json or {}

    # 1. Message Handling (/start & Phone Contact Sharing)
    if "message" in update:
        msg = update["message"]
        chat_id = str(msg.get("chat", {}).get("id"))
        first_name = msg.get("from", {}).get("first_name", "")
        username = msg.get("from", {}).get("username", "")

        # A. Contact Share ሲደረግ
        if "contact" in msg:
            phone_number = msg["contact"].get("phone_number")
            
            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO users (user_id, first_name, username, phone_number)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE 
                    SET phone_number = EXCLUDED.phone_number, first_name = EXCLUDED.first_name;
                """, (chat_id, first_name, username, phone_number))
                conn.commit()
                cursor.close()
            except Exception as e:
                logger.error(f"User contact save error: {e}")
            finally:
                if conn:
                    release_db_connection(conn)

            # Mini App መክፈቻ Inline Button
            web_app_markup = {
                "inline_keyboard": [
                    [{"text": "🚗 ቲኬት ቁረጡ (Open Mini App)", "web_app": {"url": WEB_APP_URL}}]
                ]
            }
            send_user_notification(
                chat_id, 
                f"✅ **ስልክ ቁጥርዎ ({phone_number}) በስኬት ተመዝግቧል!**\n\nአሁን ታች ያለውን አዝራር በመጫን የዕድል ቁጥርዎን መምረጥ ይችላሉ👇", 
                reply_markup=web_app_markup
            )

        # B. /start command ሲላክ
        elif "text" in msg and msg["text"].startswith("/start"):
            text = msg["text"]
            args = text.split()
            
            # Referral መመዝገብ
            if len(args) > 1 and args[1].startswith("ref_"):
                inviter_id = args[1].replace("ref_", "")
                if inviter_id != chat_id:
                    conn = None
                    try:
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT INTO referrals (user_id, referred_by)
                            VALUES (%s, %s)
                            ON CONFLICT (user_id) DO NOTHING;
                        """, (chat_id, inviter_id))
                        conn.commit()
                        cursor.close()
                    except Exception as e:
                        logger.error(f"Referral registration error: {e}")
                    finally:
                        if conn:
                            release_db_connection(conn)

            # Share Phone Button ማሳየት
            contact_markup = {
                "keyboard": [
                    [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            send_user_notification(
                chat_id,
                f"እንኳን ወደ **Jetour Dashing 2026** የመኪና ሎተሪ በደህና መጡ! 🚗✨\n\nለመቀጠል እባክዎ ከታች ያለውን **'📱 ስልክ ቁጥርዎን ያጋሩ'** የሚለውን አዝራር ይጫኑ።",
                reply_markup=contact_markup
            )

    # 2. Callback Query Handling (Admin Approve/Reject)
    if "callback_query" in update:
        cq = update["callback_query"]
        cb_data = cq.get("data", "")
        msg = cq.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if cb_data.startswith("app_") or cb_data.startswith("rej_"):
            action, nums_raw = cb_data.split("_", 1)
            numbers = [int(n) for n in nums_raw.split(",") if n.isdigit()]
            new_status = "sold" if action == "app" else "available"

            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor(cursor_factory=RealDictCursor)
                
                cursor.execute("SELECT DISTINCT user_id FROM tickets WHERE number = ANY(%s)", (numbers,))
                user_rows = cursor.fetchall()
                user_ids = [r['user_id'] for r in user_rows if r['user_id']]

                if new_status == 'sold':
                    cursor.execute("""
                        UPDATE tickets 
                        SET status = 'sold', updated_at = CURRENT_TIMESTAMP
                        WHERE number = ANY(%s)
                    """, (numbers,))

                    # Referral Bonus ቼክ ማድረግ
                    for uid in user_ids:
                        cursor.execute("SELECT referred_by FROM referrals WHERE user_id = %s", (uid,))
                        ref_row = cursor.fetchone()
                        if ref_row and ref_row['referred_by']:
                            inviter = ref_row['referred_by']
                            cursor.execute("""
                                UPDATE referrals 
                                SET successful_invites = successful_invites + 1 
                                WHERE user_id = %s 
                                RETURNING successful_invites;
                            """, (inviter,))
                            inv_res = cursor.fetchone()
                            if inv_res and inv_res['successful_invites'] % 5 == 0:
                                send_user_notification(
                                    inviter, 
                                    "🎉 **እንኳን ደስ አለዎት!**\n\n5 ጓደኞችን በመጋበዝዎ 1 **ነፃ የሎተሪ ቲኬት (Bonus Ticket)** አግኝተዋል! አድሚኑ ያነጋግርዎታል።"
                                )
                else:
                    cursor.execute("""
                        UPDATE tickets 
                        SET status = 'available', user_id = NULL, user_name = NULL, user_phone = NULL, referrer = NULL, receipt_file_id = NULL, price_paid = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE number = ANY(%s)
                    """, (numbers,))

                conn.commit()
                cursor.close()

                notify_clients({"type": "UPDATE_NUMBERS", "numbers": numbers, "status": new_status})

                nums_formatted = ", ".join([f"#{n}" for n in numbers])
                for uid in user_ids:
                    if new_status == "sold":
                        user_msg = f"🎉 **እንኳን ደስ አለዎት!**\n\nየመረጧቸው የቲኬት ቁጥሮች (**{nums_formatted}**) ክፍያቸው ፀድቋል። መልካም ዕድል!"
                    else:
                        user_msg = f"❌ **ትዕዛዝዎ አልፀደቀም**\n\nየመረጧቸው የቲኬት ቁጥሮች (**{nums_formatted}**) ክፍያ ስላልተረጋገጠ ተሰርዘዋል። እንደገና መሞከር ይችላሉ።"
                    send_user_notification(uid, user_msg)

                status_label = "✅ ጸድቋል (Sold)" if new_status == "sold" else "❌ ተሰርዟል (Available)"
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={
                    "callback_query_id": cq["id"],
                    "text": f"ቁጥሮች {numbers} {status_label} ሆነዋል!"
                })

                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption", json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "caption": f"{msg.get('caption', '')}\n\n**ሁኔታ፦** {status_label}",
                    "parse_mode": "Markdown"
                })
            except Exception as e:
                logger.error(f"Callback Query Handling Error: {e}")
            finally:
                if conn: 
                    release_db_connection(conn)

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
