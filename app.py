import os
import json
import time
import hmac
import hashlib
import random
import math
import base64
import logging
from io import BytesIO
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', template_folder='templates')

# Environment Variables
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://user:password@localhost:5432/lottery_db')
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID', 'YOUR_ADMIN_CHAT_ID')
WEB_APP_URL = os.environ.get('WEB_APP_URL', 'https://your-domain.com')

# Allowed Origins CORS
CORS(app, resources={r"/api/*": {"origins": [WEB_APP_URL, "https://telegram.org", "https://web.telegram.org"]}})

# Rate Limiter Setup
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Database Connection Pool Helper
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def release_db_connection(conn):
    if conn:
        conn.close()

# Telegram initData HMAC Security Validation
def verify_telegram_data(init_data_str):
    if not init_data_str or not BOT_TOKEN:
        return False
    try:
        parsed_data = dict(parse_qsl(init_data_str))
        if 'hash' not in parsed_data:
            return False
        received_hash = parsed_data.pop('hash')
        data_check_string = "\n".join([f"{k}={v}" for k, v in sorted(parsed_data.items())])
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(calculated_hash, received_hash)
    except Exception as e:
        logger.error(f"Telegram validation error: {e}")
        return False

# Pricing Logic
def calculate_total_price(ticket_count):
    price_per_ticket = 1000  # Default 1,000 ETB
    if ticket_count >= 10:
        price_per_ticket = 850
    elif ticket_count >= 5:
        price_per_ticket = 900
    return ticket_count * price_per_ticket

def send_telegram_admin_notification(user_name, user_phone, selected_numbers, total_price, referrer, receipt_base64, user_id, order_id):
    try:
        caption = (
            f"🚨 **አዲስ የቲኬት ትዕዛዝ ደርሷል!**\n\n"
            f"👤 **ስም:** {user_name}\n"
            f"📞 **ስልክ:** `{user_phone}`\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"🎟️ **ቲኬቶች ({len(selected_numbers)}):** {', '.join(map(str, selected_numbers))}\n"
            f"💰 **ጠቅላላ ዋጋ:** {total_price:,} ETB\n"
            f"🔗 **Referrer:** {referrer}\n"
            f"🧾 **Order ID:** `{order_id}`"
        )
        
        if receipt_base64 and "," in receipt_base64:
            header, encoded = receipt_base64.split(",", 1)
            file_data = base64.b64decode(encoded)
            files = {'photo': ('receipt.jpg', BytesIO(file_data), 'image/jpeg')}
            payload = {'chat_id': ADMIN_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=payload, files=files, timeout=10)
            res_json = res.json()
            if res_json.get('ok'):
                return res_json['result']['photo'][-1]['file_id']
        else:
            payload = {'chat_id': ADMIN_CHAT_ID, 'text': caption, 'parse_mode': 'Markdown'}
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, timeout=10)
        return None
    except Exception as e:
        logger.error(f"Telegram notification error: {e}")
        return None

# ================= REST API ENDPOINTS =================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Release expired reserved tickets automatically (Item 10)
        cursor.execute("""
            UPDATE tickets 
            SET status = 'available', reserved_at = NULL, user_id = NULL
            WHERE status = 'reserved' AND reserved_at < NOW() - INTERVAL '10 minutes';
        """)
        conn.commit()

        cursor.execute("SELECT number, status, reserved_at FROM tickets ORDER BY number ASC;")
        tickets = cursor.fetchall()
        
        # Calculate server-side remaining time for locked items
        now = datetime.utcnow()
        formatted_tickets = []
        for t in tickets:
            remaining_seconds = 0
            if t['status'] == 'reserved' and t['reserved_at']:
                expiry_time = t['reserved_at'] + timedelta(minutes=10)
                diff = (expiry_time - now).total_seconds()
                remaining_seconds = max(0, int(diff))
            
            formatted_tickets.append({
                "number": t['number'],
                "status": t['status'],
                "expires_in": remaining_seconds
            })

        return jsonify({"success": True, "tickets": formatted_tickets})
    except Exception as e:
        logger.error(f"Get tickets error: {e}")
        return jsonify({"success": False, "message": "መረጃዎችን መጫን አልተቻለም"}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/reserve-tickets', methods=['POST'])
@limiter.limit("30 per minute")
def reserve_tickets():
    data = request.json or {}
    numbers = data.get('numbers', [])
    user_id = str(data.get('user_id', ''))
    
    if not numbers or not user_id:
        return jsonify({"success": False, "message": "ትክክለኛ ያልሆነ መረጃ"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Lock rows with FOR UPDATE to handle concurrency (Item 9 / Locks)
        cursor.execute("""
            SELECT number FROM tickets 
            WHERE number = ANY(%s) AND (status = 'paid' OR status = 'pending' OR (status = 'reserved' AND user_id != %s AND reserved_at > NOW() - INTERVAL '10 minutes'))
            FOR UPDATE;
        """, (numbers, user_id))
        conflicts = cursor.fetchall()

        if conflicts:
            conn.rollback()
            return jsonify({"success": False, "message": "አንዳንድ የተረጡ ቁጥሮች አስቀድመው ተይዘዋል!"}), 409

        cursor.execute("""
            UPDATE tickets 
            SET status = 'reserved', user_id = %s, reserved_at = NOW()
            WHERE number = ANY(%s);
        """, (user_id, numbers))
        
        conn.commit()
        return jsonify({"success": True, "message": "ቁጥሮቹ ለ 10 ደቂቃ ተቆልፈዋል!"})
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"Reserve error: {e}")
        return jsonify({"success": False, "message": "ቁጥር መቆለፍ አልተቻለም"}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    data = request.json or {}
    init_data = data.get('initData', '')

    # Telegram auth check
    if init_data and not verify_telegram_data(init_data):
        return jsonify({"success": False, "message": "ያልተፈቀደ ጥያቄ!"}), 401

    selected_numbers = data.get('numbers', [])
    user_id = str(data.get('user_id', ''))
    user_name = data.get('user_name', '')
    user_phone = data.get('user_phone', '')
    referrer = data.get('referrer', 'የለም')
    receipt_base64 = data.get('receipt_base64', '')

    if not selected_numbers or not user_name or not user_phone:
        return jsonify({"success": False, "message": "እባክዎ ሙሉ መረጃ ያስገቡ!"}), 400

    order_id = f"ORD-{int(time.time())}-{random.randint(100, 999)}"
    total_price = calculate_total_price(len(selected_numbers))
    price_per_ticket = total_price / len(selected_numbers)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT number FROM tickets 
            WHERE number = ANY(%s) AND status IN ('paid', 'pending')
            FOR UPDATE;
        """, (selected_numbers,))
        if cursor.fetchall():
            conn.rollback()
            return jsonify({"success": False, "message": "አንዳንድ የተመረጡ ቁጥሮች ተይዘዋል!"}), 400

        file_id = send_telegram_admin_notification(user_name, user_phone, selected_numbers, total_price, referrer, receipt_base64, user_id, order_id)

        cursor.execute('''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s, receipt_file_id = %s, price_paid = %s, order_id = %s, updated_at = NOW()
            WHERE number = ANY(%s)
        ''', (user_id, user_name, user_phone, referrer, file_id or "uploaded", price_per_ticket, order_id, selected_numbers))

        conn.commit()
        return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል!", "order_id": order_id})
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"Submit order error: {e}")
        return jsonify({"success": False, "message": "ትዕዛዝ መላክ አልተቻለም"}), 500
    finally:
        if conn: release_db_connection(conn)

# Item 4: Broadcast Message to Ticket Holders
@app.route('/api/admin/broadcast', methods=['POST'])
def admin_broadcast():
    data = request.json or {}
    admin_secret = data.get('admin_secret', '')
    message = data.get('message', '')
    target_status = data.get('status', 'all') # 'all', 'paid', 'pending'

    if admin_secret != os.environ.get('ADMIN_SECRET', 'admin123'):
        return jsonify({"success": False, "message": "ያልተፈቀደ መግቢያ"}), 403

    if not message:
        return jsonify({"success": False, "message": "ባዶ መልእክት መላክ አይቻልም"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = "SELECT DISTINCT user_id FROM tickets WHERE user_id IS NOT NULL"
        if target_status in ['paid', 'pending']:
            query += f" AND status = '{target_status}'"
        
        cursor.execute(query)
        users = cursor.fetchall()

        sent_count = 0
        for u in users:
            uid = u['user_id']
            try:
                res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": uid,
                    "text": message,
                    "parse_mode": "HTML"
                }, timeout=5)
                if res.json().get('ok'):
                    sent_count += 1
            except Exception as ex:
                logger.error(f"Failed sending to {uid}: {ex}")

        return jsonify({"success": True, "sent_count": sent_count, "total_targets": len(users)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# Item 3: Optimized Telegram Webhook Handler
@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    update = request.json or {}
    
    # Process updates (Contact sharing response - Item 5)
    if "message" in update and "contact" in update["message"]:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        phone_number = msg["contact"]["phone_number"]
        
        reply_text = f"✅ ስልክ ቁጥርዎ ({phone_number}) ተቀብለናል! እባክዎ ወደ Mini App ይመለሱ።"
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
            "chat_id": chat_id,
            "text": reply_text
        })

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
