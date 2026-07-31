import os
import json
import base64
import hmac
import hashlib
import logging
import time
import asyncio
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qsl

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import jwt

# Telethon integration
from telethon import TelegramClient

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
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN")
JWT_SECRET = os.environ.get("JWT_SECRET")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ALLOWED_ORIGIN = os.environ.get("WEB_APP_URL", "*")

TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": [ALLOWED_ORIGIN, "https://telegram.org"]}})

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
    for q in sse_clients:
        try:
            q.append(event_data)
        except Exception:
            pass

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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY,
                status VARCHAR(20) DEFAULT 'available',
                user_id VARCHAR(50),
                user_name VARCHAR(100),
                user_phone VARCHAR(50),
                referrer VARCHAR(100),
                receipt_file_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                number INTEGER,
                action VARCHAR(20),
                user_id VARCHAR(50),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            SET status = 'available', user_id = NULL, user_name = NULL, user_phone = NULL, referrer = NULL
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

def admin_required(f):
    def decorator(*args, **kwargs):
        token = request.headers.get("Authorization")
        if not token:
            return jsonify({"message": "Token አልተገኘም!"}), 401
        try:
            token = token.split(" ")[1] if " " in token else token
            jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except Exception:
            return jsonify({"message": "ያልተፈቀደ Token!"}), 401
        return f(*args, **kwargs)
    decorator.__name__ = f.__name__
    return decorator

# --- API ENDPOINTS ---

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

@app.route('/api/stream')
def stream():
    def event_stream():
        client_queue = []
        sse_clients.append(client_queue)
        try:
            while True:
                if client_queue:
                    data = client_queue.pop(0)
                    yield f"data: {json.dumps(data)}\n\n"
                time.sleep(1)
        except GeneratorExit:
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
        return jsonify({}), 500
    finally:
        if conn:
            release_db_connection(conn)

# 🔍 ADMIN SEARCH (በስም፣ በስልክ፣ በቲኬት ቁጥር)
@app.route('/api/admin/search', methods=['GET'])
@admin_required
def admin_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Check if searching for a specific number or text
        if query.isdigit():
            cursor.execute("""
                SELECT * FROM tickets 
                WHERE number = %s OR user_phone LIKE %s OR user_id = %s
            """, (int(query), f"%{query}%", query))
        else:
            cursor.execute("""
                SELECT * FROM tickets 
                WHERE ILIKE(user_name, %s) OR user_phone LIKE %s
            """, (f"%{query}%", f"%{query}%"))
            
        rows = cursor.fetchall()
        cursor.close()
        return jsonify(rows)
    except Exception as e:
        logger.error(f"Search Error: {e}")
        return jsonify([]), 500
    finally:
        if conn:
            release_db_connection(conn)

# 📈 REVENUE CHARTS DATA FOR ADMIN
@app.route('/api/admin/revenue-chart', methods=['GET'])
@admin_required
def get_revenue_chart_data():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT DATE(updated_at) as date, COUNT(*) * 3000 as daily_revenue
            FROM tickets 
            WHERE status = 'sold'
            GROUP BY DATE(updated_at)
            ORDER BY date ASC
            LIMIT 30;
        """)
        rows = cursor.fetchall()
        cursor.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify([]), 500
    finally:
        if conn:
            release_db_connection(conn)

# 📱 PUSH NOTIFICATION BROADCAST TO USERS
@app.route('/api/admin/broadcast', methods=['POST'])
@admin_required
def broadcast_notification():
    data = request.json or {}
    message_text = data.get('message')
    if not message_text:
        return jsonify({"success": False, "message": "መልእክት አያስፈልግም!"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT DISTINCT user_id FROM tickets WHERE user_id IS NOT NULL")
        users = cursor.fetchall()
        cursor.close()

        count = 0
        for u in users:
            uid = u['user_id']
            if uid and uid.isdigit():
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": uid,
                    "text": message_text,
                    "parse_mode": "Markdown"
                })
                count += 1

        return jsonify({"success": True, "sent_count": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("5 per minute")
def submit_order():
    data = request.json or {}
    init_data = data.get('initData')
    
    if init_data and not verify_telegram_data(init_data):
        return jsonify({"success": False, "message": "Forbidden!"}), 403

    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_base64 = data.get('receipt_base64')

    if not selected_numbers:
        return jsonify({"success": False, "message": "ምንም ቁጥር አልተመረጠም!"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        placeholders = ','.join(['%s'] * len(selected_numbers))
        cursor.execute(f"SELECT number FROM tickets WHERE number IN ({placeholders}) AND status != 'available'", selected_numbers)
        taken = cursor.fetchall()

        if taken:
            taken_nums = [t['number'] for t in taken]
            cursor.close()
            return jsonify({"success": False, "message": f"ቁጥሮቹ ቀደም ብለው ተይዘዋል፡ {taken_nums}"}), 400

        cursor.execute(f'''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s, receipt_file_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE number IN ({placeholders})
        ''', [user_id, user_name, user_phone, referrer, receipt_base64] + selected_numbers)

        conn.commit()
        cursor.close()

        notify_clients({"type": "UPDATE_NUMBERS", "numbers": selected_numbers, "status": "pending"})

        # Send Telegram alert to Admin
        if ADMIN_CHAT_ID and BOT_TOKEN:
            msg = f"🆕 **አዲስ ትዕዛዝ ተልኳል!**\n\n👤 **ስም:** {user_name}\n📞 **ስልክ:** {user_phone}\n🎟️ **ቁጥሮች:** {selected_numbers}"
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": ADMIN_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            })

        return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል!"})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": "የሰርቨር ስህተት"}), 500
    finally:
        if conn: release_db_connection(conn)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
