import os
import json
import base64
import hmac
import hashlib
import logging
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIGURATION & ENVIRONMENT CHECK ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
ALLOWED_ORIGIN = os.environ.get("WEB_APP_URL", "https://tangerine-entremet-b361e6.netlify.app/")[cite: 2]

missing_vars = []
if not BOT_TOKEN: missing_vars.append("BOT_TOKEN")
if not ADMIN_CHAT_ID: missing_vars.append("ADMIN_CHAT_ID")
if not DATABASE_URL: missing_vars.append("DATABASE_URL")

if missing_vars:
    logging.error(f"❌ የጎደሉ አስፈላጊ Environment Variables አሉ: {', '.join(missing_vars)}")

app = Flask(__name__)

# CORS Security (የተፈቀደው ዶሜይን ብቻ)
CORS(app, resources={r"/api/*": {"origins": [ALLOWED_ORIGIN, "https://telegram.org"]}})

# Rate Limiting (ለSpam መከላከያ)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# --- DATABASE SETUP ---
def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL አልተዋቀረም!")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY,
                status VARCHAR(20) DEFAULT 'available',
                user_id VARCHAR(50),
                user_name VARCHAR(100),
                user_phone VARCHAR(50),
                referrer VARCHAR(100),
                receipt_file_id TEXT
            );
        ''')
        
        cursor.execute("SELECT COUNT(*) FROM tickets;")
        count = cursor.fetchone()['count']
        
        if count == 0:
            tickets_data = [(i, 'available') for i in range(1, 2201)]
            cursor.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s)", tickets_data)
            conn.commit()
            logging.info("✅ 2200 ቁጥሮች በዳታቤዝ ውስጥ በስኬት ተፈጠሩ!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"❌ የዳታቤዝ ማስጀመሪያ ስህተት: {e}")

init_db()

# --- SECURITY: TELEGRAM INITDATA VERIFICATION ---
def verify_telegram_init_data(init_data_str):
    if not init_data_str or not BOT_TOKEN:
        return False
    try:
        from urllib.parse import parse_qs, unquote
        parsed_data = parse_qs(init_data_str)
        hash_from_telegram = parsed_data.get('hash', [None])[0]
        if not hash_from_telegram:
            return False

        data_check_list = []
        for key, value in sorted(parsed_data.items()):
            if key != 'hash':
                data_check_list.append(f"{key}={value[0]}")
        data_check_string = "\n".join(data_check_list)

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        return calculated_hash == hash_from_telegram
    except Exception as e:
        logging.error(f"InitData Verification Error: {e}")
        return False

# --- HELPER FUNCTIONS ---
def send_admin_notification(numbers, user_name, user_phone, referrer, user_id, receipt_b64):
    nums_str = ", ".join(map(str, numbers))
    total_price = len(numbers) * 3000
    
    msg_text = (
        f"🚨 *አዲስ የቲኬት ደረሰኝ ደርሷል!*\n\n"
        f"👤 *ደንበኛ፡* {user_name}\n"
        f"📞 *ስልክ/ID፡* `{user_id}`\n"
        f"🎟️ *የተመረጡ ቁጥሮች፡* `{nums_str}`\n"
        f"💰 *ጠቅላላ ክፍያ፡* {total_price:,} Birr\n"
        f"✍️ *ቆራጭ/አስገባጭ፡* {referrer}\n\n"
        f"እባክዎን ደረሰኙን አጣርተው ያጽድቁ ወይም ውድቅ ያድርጉ።"
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve (አጽድቅ)", "callback_data": f"approve_{user_id}_{'-'.join(map(str, numbers))}"},
            {"text": "❌ Reject (ሰርዝ)", "callback_data": f"reject_{user_id}_{'-'.join(map(str, numbers))}"}
        ]]
    }

    url_send_photo = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    url_send_msg = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    if receipt_b64 and "," in receipt_b64:
        try:
            image_data = base64.b64decode(receipt_b64.split(",")[1])
            files = {'photo': ('receipt.jpg', image_data, 'image/jpeg')}
            payload = {
                'chat_id': ADMIN_CHAT_ID,
                'caption': msg_text,
                'parse_mode': 'Markdown',
                'reply_markup': json.dumps(reply_markup)
            }
            res = requests.post(url_send_photo, data=payload, files=files)
            if res.status_code == 200:
                return
        except Exception as e:
            logging.error(f"Photo send failed via API: {e}")

    payload = {
        'chat_id': ADMIN_CHAT_ID,
        'text': msg_text,
        'parse_mode': 'Markdown',
        'reply_markup': json.dumps(reply_markup)
    }
    requests.post(url_send_msg, json=payload)

# --- API ENDPOINTS ---

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "database": "connected" if DATABASE_URL else "missing"}), 200

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT number, status FROM tickets")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        tickets_status = {row['number']: row['status'] for row in rows}
        return jsonify(tickets_status)
    except Exception as e:
        logging.error(f"Error fetching tickets: {e}")
        return jsonify({"error": "የቲኬት መረጃ መጫን አልተቻለም"}), 500

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("5 per minute")
def submit_order():
    data = request.json or {}
    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_b64 = data.get('receipt_url')

    # Base64 Receipt Size Check (Max 1MB limit check)
    if receipt_b64 and len(receipt_b64) > 1.5 * 1024 * 1024:
        return jsonify({"success": False, "message": "የፎቶው መጠን በጣም ትልቅ ነው! እባክዎን አነስ ያለ ፎቶ ይጠቀሙ።"}), 400

    if not selected_numbers:
        return jsonify({"success": False, "message": "ምንም ቁጥር አልተመረጠም!"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        placeholders = ','.join(['%s'] * len(selected_numbers))
        cursor.execute(f"SELECT number FROM tickets WHERE number IN ({placeholders}) AND status != 'available'", selected_numbers)
        taken = cursor.fetchall()

        if taken:
            conn.rollback()
            cursor.close()
            conn.close()
            taken_nums = [t['number'] for t in taken]
            return jsonify({"success": False, "message": f"እነዚህ ቁጥሮች ቀደም ብለው ተይዘዋል፡ {taken_nums}"}), 400

        cursor.execute(f'''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s
            WHERE number IN ({placeholders})
        ''', [user_id, user_name, user_phone, referrer] + selected_numbers)

        conn.commit()
        cursor.close()
        conn.close()

        send_admin_notification(selected_numbers, user_name, user_phone, referrer, user_id, receipt_b64)

        return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል!"})
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        logging.error(f"Error in submit_order: {e}")
        return jsonify({"success": False, "message": "የሰርቨር ስህተት አጋጥሟል"}), 500

@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json() or {}
    if "callback_query" in update:
        query = update["callback_query"]
        message = query.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat", {}).get("id")
        
        callback_data = query.get("data", "").split('_')
        
        if len(callback_data) >= 3:
            action = callback_data[0]
            user_id = callback_data[1]
            numbers = list(map(int, callback_data[2].split('-')))
            
            conn = None
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                placeholders = ','.join(['%s'] * len(numbers))

                if action == "approve":
                    cursor.execute(f"UPDATE tickets SET status = 'sold' WHERE number IN ({placeholders})", numbers)
                    conn.commit()
                    user_msg = f"🎉 **እንኳን ደስ አለዎት!**\n\nየቆረጧቸው ቲኬቶች (ቁጥር፡ {numbers}) በስኬት ጸድቀዋል።"
                    admin_status_text = f"\n\n✅ **Approved by Admin**"
                elif action == "reject":
                    cursor.execute(f"UPDATE tickets SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL WHERE number IN ({placeholders})", numbers)
                    conn.commit()
                    user_msg = f"⚠️ **ማሳወቂያ፡**\n\nየላኩት የክፍያ ደረሰኝ ውድቅ ስለተደረገ የተያዙት ቁጥሮች ({numbers}) ተመልሰው ነፃ ሆነዋል።"
                    admin_status_text = f"\n\n❌ **Rejected by Admin**"

                cursor.close()
                conn.close()

                # 1. Notify User
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": user_id,
                    "text": user_msg,
                    "parse_mode": "Markdown"
                })

                # 2. Update Admin Message (Remove Callback Buttons & Add Status Badge)
                current_caption = message.get("caption") or message.get("text") or ""
                updated_text = current_caption + admin_status_text

                if "caption" in message:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption", json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "caption": updated_text,
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": []} # Remove Buttons
                    })
                else:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": updated_text,
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": []} # Remove Buttons
                    })

            except Exception as e:
                if conn:
                    conn.rollback()
                    conn.close()
                logging.error(f"Webhook Callback Error: {e}")

    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
