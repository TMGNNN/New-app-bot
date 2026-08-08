import os
import json
import base64
import hmac
import hashlib
import logging
import time
import io
import atexit
import random

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
from apscheduler.schedulers.background import BackgroundScheduler

# LOGGING SETUP
log_handler = RotatingFileHandler('app.log', maxBytes=5*1024*1024, backupCount=3)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "*")

ADMIN_IDS = os.environ.get("ADMIN_IDS", "8982566651,987654321").split(",")
ADMIN_IDS = [aid.strip() for aid in ADMIN_IDS if aid.strip()]

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

CORS(app, resources={r"/api/*": {"origins": [WEB_APP_URL, "https://telegram.org", "*"]}})

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

def close_db_pool():
    if db_pool:
        db_pool.closeall()
        logger.info("✅ DB Pool ተዘግቷል!")

atexit.register(close_db_pool)

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

def is_authorized_admin(req):
    user_id = req.headers.get('X-User-Id') or (req.json or {}).get('user_id') or req.args.get('user_id')
    if user_id and str(user_id) in ADMIN_IDS:
        return True
    
    init_data = req.headers.get('X-Telegram-Init-Data')
    if init_data and verify_telegram_data(init_data):
        try:
            parsed = dict(parse_qsl(init_data))
            user_info = json.loads(parsed.get('user', '{}'))
            return str(user_info.get('id')) in ADMIN_IDS
        except Exception:
            pass
    return False

def send_telegram_push(chat_id, text):
    if not (BOT_TOKEN and chat_id):
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Push Notification Error: {e}")

def log_audit_action(admin_id, action, details):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (admin_id, action, details) VALUES (%s, %s, %s)",
            (str(admin_id), action, details)
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Audit log error: {e}")
    finally:
        if conn: release_db_connection(conn)

def init_db():
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(50) PRIMARY KEY,
                first_name VARCHAR(100),
                username VARCHAR(100),
                phone_number VARCHAR(50),
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

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
                order_id VARCHAR(50),
                reserved_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS winners (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                ticket_number INTEGER,
                round VARCHAR(50),
                photo TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                admin_id VARCHAR(50),
                action VARCHAR(100),
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id);")
        cursor.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMP;")
        cursor.execute("SELECT COUNT(*) FROM tickets;")
        cursor.execute("SELECT COUNT(*) FROM tickets;")
        count = cursor.fetchone()['count']
        
        if count < 2200:
            tickets_data = [(i, 'available') for i in range(1, 2201)]
            cursor.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT (number) DO NOTHING", tickets_data)
            
        for admin_id in ADMIN_IDS:
            cursor.execute("""
                INSERT INTO users (user_id, is_admin) 
                VALUES (%s, TRUE) 
                ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE
            """, (admin_id,))
        
        conn.commit()
        cursor.close()
        logger.info("✅ Database Initialized!")
    except Exception as e:
        logger.error(f"❌ DB Init error: {e}")
    finally:
        if conn:
            release_db_connection(conn)

def cleanup_expired_pendings():
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        fifteen_mins_ago = datetime.utcnow() - timedelta(minutes=15)
        
        cursor.execute("""
            UPDATE tickets 
            SET status = 'available', user_id = NULL, user_name = NULL, user_phone = NULL, referrer = NULL, receipt_file_id = NULL, price_paid = NULL, order_id = NULL, reserved_at = NULL
            WHERE status IN ('pending', 'reserved') AND (updated_at < %s OR reserved_at < %s)
            RETURNING number;
        """, (fifteen_mins_ago, fifteen_mins_ago))
        
        released_rows = cursor.fetchall()
        conn.commit()
        
        if released_rows:
            released_numbers = [r['number'] for r in released_rows]
            notify_clients({"type": "UPDATE_NUMBERS", "numbers": released_numbers, "status": "available"})
            logger.info(f"Released expired tickets: {released_numbers}")
            
        cursor.close()
    except Exception as e:
        logger.error(f"Error cleaning expired tickets: {e}")
    finally:
        if conn:
            release_db_connection(conn)

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(cleanup_expired_pendings, 'interval', minutes=1)
scheduler.start()

init_db()

def calculate_total_price(ticket_count):
    base_price = 3000
    total = ticket_count * base_price
    if ticket_count >= 5:
        total -= 1500
    elif ticket_count >= 3:
        total -= 500
    return max(total, 0)

def send_telegram_admin_notification(user_name, user_phone, selected_numbers, total_price, referrer, receipt_base64, user_id, order_id):
    if not (ADMIN_CHAT_ID and BOT_TOKEN):
        return None

    nums_str = ",".join(map(str, selected_numbers))
    caption = (
        f"🆕 **አዲስ የቲኬት ትዕዛዝ!**\n\n"
        f"🆔 **Order ID:** `{order_id}`\n"
        f"👤 **ስም:** {user_name}\n"
        f"📞 **ስልክ:** {user_phone}\n"
        f"🎟️ **ቁጥሮች:** {nums_str}\n"
        f"💰 **ጠቅላላ ዋጋ:** {total_price:,} ብር\n"
        f"🔗 **የጋበዘው:** {referrer}\n"
        f"🆔 **የተጠቃሚ አይዲ:** {user_id}"
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
            res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=payload, files=files, timeout=30)
            res_json = res.json()
            if res_json.get('ok'):
                photos = res_json['result'].get('photo', [])
                if photos:
                    file_id = photos[-1]['file_id']
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}")

    return file_id

# API ENDPOINTS

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "admin_count": len(ADMIN_IDS)}), 200

@app.route('/bot/webhook', methods=['POST'])
def bot_webhook_handler():
    return jsonify({"status": "ok"}), 200

@app.route('/api/reserve-tickets', methods=['POST'])
def reserve_tickets():
    data = request.json or {}
    numbers = data.get('numbers', [])
    user_id = str(data.get('user_id', ''))
    
    if not numbers or not user_id:
        return jsonify({"success": False, "message": "አስፈላጊ መረጃ ጎድሏል!"}), 400
        
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT number FROM tickets WHERE number = ANY(%s) AND status != 'available'", (numbers,))
        taken = cursor.fetchall()
        if taken:
            return jsonify({"success": False, "message": "አንዳንድ የተመረጡ ቁጥሮች በሌላ ሰው ተይዘዋል!"}), 400
            
        cursor.execute("""
            UPDATE tickets 
            SET status = 'reserved', user_id = %s, reserved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        """, (user_id, numbers))
        conn.commit()
        cursor.close()
        
        notify_clients({"type": "UPDATE_NUMBERS", "numbers": numbers, "status": "reserved"})
        return jsonify({"success": True, "reserved_at": datetime.utcnow().isoformat()})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/referral-stats', methods=['GET'])
def get_referral_stats():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"success": False}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT COUNT(DISTINCT user_id) as total_referred, 
                   COUNT(CASE WHEN status = 'sold' THEN 1 END) as successful_purchases
            FROM tickets 
            WHERE referrer = %s;
        """, (str(user_id),))
        stats = cursor.fetchone()
        cursor.close()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/recent-purchases', methods=['GET'])
def get_recent_purchases():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT number, updated_at 
            FROM tickets 
            WHERE status = 'sold' 
            ORDER BY updated_at DESC LIMIT 10;
        """)
        recent = cursor.fetchall()
        cursor.close()
        return jsonify(recent)
    except Exception as e:
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/get-user-info', methods=['GET'])
def get_user_info():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"success": False}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT first_name, phone_number, is_admin FROM users WHERE user_id = %s", (str(user_id),))
        user = cursor.fetchone()
        cursor.close()
        if user:
            return jsonify({
                "success": True, 
                "name": user['first_name'], 
                "phone": user['phone_number'],
                "is_admin": user.get('is_admin', False)
            })
        return jsonify({"success": False}), 404
    except Exception as e:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

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
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT number, status, updated_at FROM tickets")
        rows = cursor.fetchall()
        cursor.close()
        
        tickets_info = {}
        for row in rows:
            tickets_info[row['number']] = {
                "status": row['status'],
                "updated_at": row['updated_at'].isoformat() if row['updated_at'] else None
            }
        res = jsonify(tickets_info)
        res.headers['Cache-Control'] = 'public, max-age=10'
        return res
    except Exception as e:
        return jsonify({}), 500
    finally:
        if conn: release_db_connection(conn)

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
            SELECT number, status, price_paid, updated_at, receipt_file_id, order_id
            FROM tickets 
            WHERE user_id = %s AND status IN ('pending', 'sold', 'reserved')
            ORDER BY updated_at DESC
        """, (str(user_id),))
        rows = cursor.fetchall()
        cursor.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/winners', methods=['GET'])
def get_winners():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT name, ticket_number, round, photo FROM winners ORDER BY created_at DESC;")
        winners = cursor.fetchall()
        cursor.close()
        return jsonify(winners)
    except Exception as e:
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    data = request.json or {}
    selected_numbers = data.get('numbers', [])
    user_id = str(data.get('user_id', ''))
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_base64 = data.get('receipt_base64')

    if not selected_numbers or not user_name or not user_phone:
        return jsonify({"success": False, "message": "እባክዎ ሁሉንም አስፈላጊ መረጃዎች ይሙሉ!"}), 400

    invalid_numbers = [n for n in selected_numbers if not isinstance(n, int) or n < 1 or n > 2200]
    if invalid_numbers:
        return jsonify({"success": False, "message": "የተሳሳተ የቲኬት ቁጥር ተመርጧል!"}), 400

    order_id = f"ORD-{int(time.time())}-{random.randint(100,999)}"
    total_price = calculate_total_price(len(selected_numbers))
    price_per_ticket = total_price / len(selected_numbers)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("SELECT number FROM tickets WHERE number = ANY(%s) AND status NOT IN ('available', 'reserved')", (selected_numbers,))
        taken = cursor.fetchall()
        if taken:
            return jsonify({"success": False, "message": "አንዳንድ የተመረጡ ቁጥሮች ቀደም ብለው ተይዘዋል!"}), 400

        file_id = send_telegram_admin_notification(user_name, user_phone, selected_numbers, total_price, referrer, receipt_base64, user_id, order_id)

        cursor.execute('''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s, receipt_file_id = %s, price_paid = %s, order_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        ''', (user_id, user_name, user_phone, referrer, file_id or "uploaded", price_per_ticket, order_id, selected_numbers))

        conn.commit()
        cursor.close()

        notify_clients({"type": "UPDATE_NUMBERS", "numbers": selected_numbers, "status": "pending"})

        return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል!", "order_id": order_id})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": f"የሰርቨር ስህተት፦ {str(e)}"}), 500
    finally:
        if conn: release_db_connection(conn)

# ADMIN ENDPOINTS

@app.route('/api/admin/verify-auth', methods=['POST'])
def verify_admin_auth():
    if is_authorized_admin(request):
        return jsonify({"success": True, "is_admin": True}), 200
    return jsonify({"error": "Unauthorized"}), 401

@app.route('/api/admin/analytics', methods=['POST'])
def admin_analytics():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT 
                COUNT(CASE WHEN status = 'sold' THEN 1 END) as sold_count,
                COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_count,
                COALESCE(SUM(CASE WHEN status = 'sold' THEN price_paid ELSE 0 END), 0) as total_revenue
            FROM tickets;
        """)
        stats = cursor.fetchone()

        cursor.execute("""
            SELECT DATE(updated_at) as date, COUNT(*) as count, SUM(price_paid) as revenue 
            FROM tickets 
            WHERE status = 'sold' 
            GROUP BY DATE(updated_at) 
            ORDER BY DATE(updated_at) ASC LIMIT 7;
        """)
        daily_trend = cursor.fetchall()
        stats['daily_trend'] = daily_trend
        cursor.close()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/broadcast', methods=['POST'])
def broadcast_message():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json or {}
    message_text = data.get('message')
    admin_id = data.get('user_id', 'Admin')

    if not message_text:
        return jsonify({"error": "Message is required"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT DISTINCT user_id FROM tickets WHERE user_id IS NOT NULL AND user_id != '';")
        rows = cursor.fetchall()
        cursor.close()

        user_ids = [r['user_id'] for r in rows if r['user_id']]
        sent_count = 0

        for uid in user_ids:
            send_telegram_push(uid, f"📢 **የሎተሪ ማሳወቂያ፦**\n\n{message_text}")
            sent_count += 1

        log_audit_action(admin_id, "BROADCAST_MESSAGE", f"Sent broadcast to {sent_count} ticket holders")
        return jsonify({"success": True, "message": f"መልእክቱ ለ {sent_count} ቲኬት ባለቤቶች ተልኳል!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/export-orders', methods=['GET'])
def export_orders():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT number, status, user_name, user_phone, price_paid, order_id, updated_at FROM tickets WHERE status != 'available' ORDER BY updated_at DESC")
        orders = cursor.fetchall()
        cursor.close()

        csv_data = "Order_ID,Ticket,Status,Name,Phone,Price,Date\n"
        for o in orders:
            csv_data += f"{o['order_id']},{o['number']},{o['status']},{o['user_name']},{o['user_phone']},{o['price_paid']},{o['updated_at']}\n"

        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=orders.csv"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/update-ticket-status', methods=['POST'])
def update_ticket_status():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json or {}
    admin_id = data.get('user_id', 'Admin')
    ticket_numbers = data.get('ticket_numbers', [])
    new_status = data.get('status')
    
    if not ticket_numbers or new_status not in ['sold', 'available']:
        return jsonify({"error": "Invalid request"}), 400
    
    ticket_numbers = [n for n in ticket_numbers if isinstance(n, int) and 1 <= n <= 2200]
    if not ticket_numbers:
        return jsonify({"error": "No valid tickets provided"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT DISTINCT user_id FROM tickets WHERE number = ANY(%s)", (ticket_numbers,))
        users_to_notify = [r['user_id'] for r in cursor.fetchall() if r['user_id']]

        cursor.execute("""
            UPDATE tickets 
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        """, (new_status, ticket_numbers))
        
        conn.commit()
        cursor.close()
        
        log_audit_action(admin_id, "UPDATE_TICKET_STATUS", f"Updated tickets {ticket_numbers} to {new_status}")
        
        msg = f"🎉 የቲኬት ትዕዛዝዎ ፀድቋል! ቁጥሮች፦ {ticket_numbers}" if new_status == 'sold' else f"⚠️ የቲኬት ቁጥሮችዎ {ticket_numbers} ተሰርዘዋል/ተለቀዋል።"
        for uid in users_to_notify:
            send_telegram_push(uid, msg)

        notify_clients({"type": "UPDATE_NUMBERS", "numbers": ticket_numbers, "status": new_status})
        
        return jsonify({"success": True, "message": f"{len(ticket_numbers)} ቲኬቶች ተሻሻሉ!"})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/add-winner', methods=['POST'])
def add_winner():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json or {}
    admin_id = data.get('user_id', 'Admin')
    name = data.get('name')
    ticket_number = data.get('ticket_number')
    round_name = data.get('round', 'Round 1')
    photo = data.get('photo', '')
    
    if not name or not isinstance(ticket_number, int) or ticket_number < 1 or ticket_number > 2200:
        return jsonify({"error": "Name and valid ticket number required"}), 400
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            INSERT INTO winners (name, ticket_number, round, photo)
            VALUES (%s, %s, %s, %s)
        """, (name, ticket_number, round_name, photo))
        
        conn.commit()
        cursor.close()
        
        log_audit_action(admin_id, "ADD_WINNER", f"Added winner {name} for ticket #{ticket_number}")
        
        return jsonify({"success": True, "message": "አሸናፊ ተጨምሯል!"})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/audit-logs', methods=['GET'])
def get_audit_logs():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 50;")
        logs = cursor.fetchall()
        cursor.close()
        return jsonify(logs)
    except Exception as e:
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)
            
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
