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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
ALLOWED_ORIGIN = os.environ.get("WEB_APP_URL", "*")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per day", "100 per hour"],
    storage_uri="memory://"
)

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
        
        # Table update with TIMESTAMP for 30-min auto release
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY,
                status VARCHAR(20) DEFAULT 'available',
                user_id VARCHAR(50),
                user_name VARCHAR(100),
                user_phone VARCHAR(50),
                referrer VARCHAR(100),
                receipt_file_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        cursor.execute("SELECT COUNT(*) FROM tickets;")
        if cursor.fetchone()['count'] < 2200:
            tickets_data = [(i, 'available') for i in range(1, 2201)]
            cursor.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT (number) DO NOTHING", tickets_data)
            conn.commit()
            logging.info("✅ 2200 ቁጥሮች ተፈጠሩ!")
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"❌ DB Init Error: {e}")

init_db()

# Release pending tickets held for more than 30 minutes
def auto_release_pending_tickets():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE tickets 
            SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL
            WHERE status = 'pending' AND updated_at < CURRENT_TIMESTAMP - INTERVAL '30 minutes';
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"Auto release failed: {e}")

def send_telegram_msg(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        logging.error(f"Telegram notification error: {e}")

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    auto_release_pending_tickets()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT number, status FROM tickets")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({row['number']: row['status'] for row in rows})
    except Exception as e:
        return jsonify({}), 200

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    data = request.json or {}
    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')

    if not selected_numbers:
        return jsonify({"success": False, "message": "ቁጥር አልተመረጠም"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        placeholders = ','.join(['%s'] * len(selected_numbers))
        
        cursor.execute(f"SELECT number FROM tickets WHERE number IN ({placeholders}) AND status != 'available'", selected_numbers)
        if cursor.fetchall():
            conn.rollback()
            return jsonify({"success": False, "message": "የተመረጡት ቁጥሮች ቀደም ብለው ተይዘዋል!"}), 400

        cursor.execute(f'''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s, updated_at = CURRENT_TIMESTAMP
            WHERE number IN ({placeholders})
        ''', [user_id, user_name, user_phone, referrer] + selected_numbers)

        conn.commit()
        cursor.close()
        conn.close()

        # Notify User
        send_telegram_msg(user_id, f"⏳ *ትዕዛዝዎ ደርሶናል!*\n\nየያዟቸው ቁጥሮች ({selected_numbers}) በማረጋገጥ ላይ ናቸው። እስከ 30 ደቂቃ ካልፀደቀ አውቶማቲክ ነፃ ይደረጋል።")

        return jsonify({"success": True, "message": "ትዕዛዝዎ ተልኳል!"})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": "የሰርቨር ስህተት"}), 500

@app.route('/api/my-tickets', methods=['GET'])
def my_tickets():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify([]), 400
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT number, status, referrer FROM tickets WHERE user_id = %s", (user_id,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify([]), 500

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as cnt FROM tickets GROUP BY status")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        stats = {'sold': 0, 'pending': 0, 'available': 0}
        for r in rows:
            stats[r['status']] = r['cnt']
        
        stats['revenue'] = stats['sold'] * 3000
        return jsonify(stats)
    except Exception as e:
        return jsonify({'sold': 0, 'pending': 0, 'available': 2200, 'revenue': 0}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
