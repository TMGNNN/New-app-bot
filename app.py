import os
import sys
import json
import base64
import hmac
import hashlib
import logging
import time
import io
import atexit
import random
import csv
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import requests
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# ==================== LOGGING ====================
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if logger.handlers:
    logger.handlers.clear()
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(_handler)

# ==================== CONFIG ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "*")
DRAW_END_AT = os.environ.get("DRAW_END_AT")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin2024")
IS_VERCEL = os.environ.get("VERCEL") == "1"

# ✅ FIX: Three admin IDs configured as fallback
ADMIN_IDS = [a.strip() for a in os.environ.get(
    "ADMIN_IDS",
    "8982566651,6349936975,800715162"
).split(",") if a.strip()]

# Log admin IDs at startup for verification
logger.info(f"🔍 ADMIN_IDS loaded: {len(ADMIN_IDS)} configured")
for i, aid in enumerate(ADMIN_IDS, 1):
    logger.info(f"   [{i}] {aid}")

# ==================== PRICING ====================
TOTAL_TICKETS = 3500
BASE_PRICE = 3500
MAX_TICKETS_PER_ORDER = 20
RESERVATION_TIMEOUT_MINUTES = 15

PRICING_TIERS = [
    {"min_qty": 5, "discount": 1500},
    {"min_qty": 3, "discount": 500},
]

def calculate_total_price(ticket_count):
    if ticket_count <= 0:
        return 0
    total = ticket_count * BASE_PRICE
    for tier in PRICING_TIERS:
        if ticket_count >= tier["min_qty"]:
            total -= tier["discount"]
            break
    return max(total, 0)

# ==================== DEFAULT SETTINGS ====================
DEFAULT_SETTINGS = {
    "product_name": "BYD Leopard 5",
    "subtitle": "Black",
    "image_url": "https://images.unsplash.com/photo-1617814076367-b759c7d7e738?auto=format&fit=crop&w=900&q=80",
    "draw_end_at": "",
    "base_price": str(BASE_PRICE),
    "total_tickets": str(TOTAL_TICKETS),
    "max_per_order": str(MAX_TICKETS_PER_ORDER),
    "reservation_minutes": str(RESERVATION_TIMEOUT_MINUTES),
    "pricing_tiers": json.dumps(PRICING_TIERS),
    "telebirr_number": "0924242419",
    "telebirr_name": "Getachew",
    "cbe_number": "1000528139489",
    "cbe_name": "Getachew Fikadu Jirata",
    "draw_title": "BYD Leopard 5 Giveaway",
}

# ==================== APP ====================
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

CORS(app, resources={r"/api/*": {
    "origins": "*",
    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "X-Telegram-Init-Data", "X-Admin-Token", "X-User-Id"]
}}, supports_credentials=False)

limiter = Limiter(
    get_remote_address, app=app,
    default_limits=["1000 per day", "500 per hour"],
    storage_uri="memory://"
)

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/favicon.ico')
def favicon():
    return '', 204

# ==================== DB POOL ====================
db_pool = None

def init_db_pool():
    global db_pool
    if not DATABASE_URL:
        return
    try:
        db_url = DATABASE_URL
        if ("supabase.co" in db_url or "pooler.supabase.com" in db_url) and "sslmode" not in db_url:
            sep = "&" if "?" in db_url else "?"
            db_url += f"{sep}sslmode=require"
        db_pool = ThreadedConnectionPool(minconn=1, maxconn=20, dsn=db_url)
        logger.info("✅ DB Pool ready")
    except Exception as e:
        logger.error(f"❌ DB Pool failed: {e}")

def close_db_pool():
    if db_pool:
        db_pool.closeall()

atexit.register(close_db_pool)

def get_db_connection():
    if not db_pool:
        raise Exception("DATABASE_URL not configured")
    return db_pool.getconn()

def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def _direct_db_connection():
    db_url = DATABASE_URL
    if ("supabase.co" in db_url or "pooler.supabase.com" in db_url) and "sslmode" not in db_url:
        sep = "&" if "?" in db_url else "?"
        db_url += f"{sep}sslmode=require"
    return psycopg2.connect(db_url)

# ==================== AUTH ====================
def verify_telegram_data(init_data: str) -> bool:
    if not BOT_TOKEN or not init_data:
        return False
    try:
        parsed = dict(parse_qsl(init_data))
        if 'hash' not in parsed:
            return False
        auth_date = int(parsed.get('auth_date', '0'))
        if not auth_date or time.time() - auth_date > 86400:
            return False
        hash_to_check = parsed.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, hash_to_check)
    except Exception as e:
        logger.error(f"initData verify error: {e}")
        return False

def get_verified_user_id(req):
    json_data = req.get_json(silent=True) or {}
    init_data = req.headers.get("X-Telegram-Init-Data") or json_data.get("initData")
    if not init_data or not verify_telegram_data(init_data):
        return None
    try:
        parsed = dict(parse_qsl(init_data))
        user_info = json.loads(parsed.get("user", "{}"))
        return str(user_info.get("id")) if user_info.get("id") is not None else None
    except Exception:
        return None

def require_verified_user(req, supplied_user_id=None):
    verified = get_verified_user_id(req)
    if not verified or (supplied_user_id is not None and str(supplied_user_id) != verified):
        return None
    return verified

def _expected_admin_token():
    if not BOT_TOKEN:
        logger.warning("⚠️ BOT_TOKEN missing")
    return hashlib.sha256(f"{ADMIN_PASSWORD}:{BOT_TOKEN or 'insecure'}".encode()).hexdigest()

def is_authorized_admin(req):
    # Method 1: Token from password login
    token = req.headers.get('X-Admin-Token')
    if token and hmac.compare_digest(token, _expected_admin_token()):
        return True
    # Method 2: Telegram initData user ID in ADMIN_IDS
    init_data = req.headers.get('X-Telegram-Init-Data')
    if init_data and verify_telegram_data(init_data):
        try:
            parsed = dict(parse_qsl(init_data))
            ui = json.loads(parsed.get('user', '{}'))
            return str(ui.get('id')) in ADMIN_IDS
        except Exception:
            pass
    return False

# ==================== TELEGRAM HELPERS ====================
def send_telegram_push(chat_id, text, retries=2):
    if not (BOT_TOKEN and chat_id):
        return
    for attempt in range(retries):
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=5
            )
            return
        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"Push error: {e}")
            else:
                time.sleep(0.5)

def send_telegram_admin_notification(user_name, user_phone, numbers, total_price, referrer, receipt_b64, user_id, order_id):
    if not (ADMIN_CHAT_ID and BOT_TOKEN):
        return None
    nums_str = ",".join(map(str, numbers))
    caption = (
        f"🆕 **አዲስ የቲኬት ትዕዛዝ**\n\n"
        f"🆔 `{order_id}`\n"
        f"👤 {user_name}\n"
        f"📞 {user_phone}\n"
        f"🎟️ {nums_str}\n"
        f"💰 {total_price:,} ብር\n"
        f"🔗 {referrer}\n"
        f"🆔 {user_id}"
    )
    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ ፅድቅ", "callback_data": f"app:{order_id}"},
            {"text": "❌ ሰርዝ", "callback_data": f"rej:{order_id}"}
        ]]
    }
    file_id = None
    try:
        if receipt_b64 and "," in receipt_b64:
            _, encoded = receipt_b64.split(",", 1)
            image_data = base64.b64decode(encoded)
            files = {'photo': ('receipt.jpg', io.BytesIO(image_data), 'image/jpeg')}
            payload = {
                'chat_id': ADMIN_CHAT_ID, 'caption': caption,
                'parse_mode': 'Markdown', 'reply_markup': json.dumps(reply_markup)
            }
            res = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data=payload, files=files, timeout=30
            ).json()
            if res.get('ok'):
                photos = res['result'].get('photo', [])
                if photos:
                    file_id = photos[-1]['file_id']
    except Exception as e:
        logger.error(f"Admin notify error: {e}")
    return file_id

def log_audit_action(admin_id, action, details):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (admin_id, action, details) VALUES (%s, %s, %s)",
            (str(admin_id), action, details)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Audit error: {e}")
    finally:
        if conn: release_db_connection(conn)

# ==================== SETTINGS ====================
def get_setting(key, default=None):
    conn = None
    from_pool = False
    try:
        if db_pool:
            conn = get_db_connection()
            from_pool = True
        else:
            conn = _direct_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        if row:
            return row['value']
        return default if default is not None else DEFAULT_SETTINGS.get(key)
    except Exception:
        return default if default is not None else DEFAULT_SETTINGS.get(key)
    finally:
        if conn:
            if from_pool: release_db_connection(conn)
            else: conn.close()

def set_setting(key, value):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """, (key, str(value)))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"set_setting error: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: release_db_connection(conn)

def get_all_settings():
    result = dict(DEFAULT_SETTINGS)
    conn = None
    from_pool = False
    try:
        if db_pool:
            conn = get_db_connection()
            from_pool = True
        else:
            conn = _direct_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT key, value FROM settings")
        for r in cur.fetchall():
            result[r['key']] = r['value']
        cur.close()
    except Exception as e:
        logger.error(f"get_all_settings: {e}")
    finally:
        if conn:
            if from_pool: release_db_connection(conn)
            else: conn.close()
    return result

def get_int_setting(key, fallback):
    try:
        return int(get_setting(key, fallback))
    except Exception:
        return fallback

# ==================== DB INIT ====================
def init_db():
    """✅ FIX: No more DROP TABLE settings CASCADE"""
    if not DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL not set")
        return

    try:
        conn = _direct_db_connection()
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(50) PRIMARY KEY,
                first_name VARCHAR(100),
                username VARCHAR(100),
                phone_number VARCHAR(50),
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''
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
        cur.execute('''
            CREATE TABLE IF NOT EXISTS winners (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                ticket_number INTEGER,
                round VARCHAR(50),
                photo TEXT,
                user_phone VARCHAR(50),
                lottery_name VARCHAR(200),
                prize VARCHAR(200),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                admin_id VARCHAR(50),
                action VARCHAR(100),
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);",
            "CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_tickets_order_id ON tickets(order_id);",
            "CREATE INDEX IF NOT EXISTS idx_tickets_referrer ON tickets(referrer);",
            "CREATE INDEX IF NOT EXISTS idx_winners_created ON winners(created_at DESC);",
        ]:
            try: cur.execute(stmt)
            except Exception: pass

        for stmt in [
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS price_paid NUMERIC(10, 2);",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS order_id VARCHAR(50);",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMP;",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS referrer VARCHAR(100);",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS receipt_file_id TEXT;",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS user_name VARCHAR(100);",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS user_phone VARCHAR(50);",
            "ALTER TABLE winners ADD COLUMN IF NOT EXISTS user_phone VARCHAR(50);",
            "ALTER TABLE winners ADD COLUMN IF NOT EXISTS lottery_name VARCHAR(200);",
            "ALTER TABLE winners ADD COLUMN IF NOT EXISTS prize VARCHAR(200);",
        ]:
            try: cur.execute(stmt)
            except Exception as e: logger.info(f"⚠️ Migration: {e}")

        cur.execute("SELECT COUNT(*) AS count FROM tickets;")
        count = cur.fetchone()['count']
        total = int(get_setting("total_tickets", TOTAL_TICKETS))
        if count < total:
            data = [(i, 'available') for i in range(1, total + 1)]
            cur.executemany(
                "INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT (number) DO NOTHING",
                data
            )
            logger.info(f"✅ Populated {total - count} tickets")

        # ✅ Register all 3 admins
        for aid in ADMIN_IDS:
            cur.execute(
                "INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE) "
                "ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE",
                (aid,)
            )
        logger.info(f"✅ Registered {len(ADMIN_IDS)} admins")

        for k, v in DEFAULT_SETTINGS.items():
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )

        cur.close()
        conn.close()
        logger.info("✅ DB ready")
    except Exception as e:
        logger.error(f"❌ DB init: {e}")

init_db_pool()
init_db()

# ==================== CLEANUP ====================
def cleanup_expired_pendings():
    if not DATABASE_URL:
        return 0
    timeout_min = get_int_setting("reservation_minutes", RESERVATION_TIMEOUT_MINUTES)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_min)
    for attempt in range(2):
        conn = None
        try:
            if attempt == 0 and db_pool:
                conn = get_db_connection()
            else:
                conn = _direct_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                UPDATE tickets
                SET status='available', user_id=NULL, user_name=NULL, user_phone=NULL,
                    referrer=NULL, receipt_file_id=NULL, price_paid=NULL, order_id=NULL,
                    reserved_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('pending', 'reserved')
                  AND COALESCE(reserved_at, updated_at) < %s
                RETURNING number;
            """, (cutoff,))
            released = [r['number'] for r in cur.fetchall()]
            conn.commit()
            cur.close()
            if attempt == 0 and db_pool:
                release_db_connection(conn)
            else:
                conn.close()
            conn = None
            if released:
                logger.info(f"✅ Released {len(released)} expired tickets")
            return len(released)
        except psycopg2.OperationalError as e:
            if conn:
                try:
                    if attempt == 0 and db_pool: release_db_connection(conn)
                    else: conn.close()
                except Exception: pass
                conn = None
            if "ssl" in str(e).lower() and attempt == 0:
                time.sleep(1)
                continue
            return 0
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            if conn:
                try:
                    if attempt == 0 and db_pool: release_db_connection(conn)
                    else: conn.close()
                except Exception: pass
            return 0
    return 0

if not IS_VERCEL:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(cleanup_expired_pendings, 'interval', minutes=1)
        scheduler.start()
        logger.info("✅ Scheduler started")
    except Exception as e:
        logger.warning(f"Scheduler: {e}")

# ==================== PUBLIC APIs ====================
@app.route('/api/config', methods=['GET'])
def public_config():
    s = get_all_settings()
    try:
        tiers = json.loads(s.get("pricing_tiers") or "[]")
    except Exception:
        tiers = PRICING_TIERS
    draw_at = s.get("draw_end_at") or DRAW_END_AT or (datetime.now(timezone.utc) + timedelta(days=30)).isoformat() + 'Z'
    return jsonify({
        "success": True,
        "draw_end_at": draw_at,
        "ticket_count": int(s.get("total_tickets") or TOTAL_TICKETS),
        "ticket_price": int(s.get("base_price") or BASE_PRICE),
        "max_per_order": int(s.get("max_per_order") or MAX_TICKETS_PER_ORDER),
        "reservation_minutes": int(s.get("reservation_minutes") or RESERVATION_TIMEOUT_MINUTES),
        "pricing_tiers": tiers,
        "product_name": s.get("product_name"),
        "subtitle": s.get("subtitle"),
        "image_url": s.get("image_url"),
        "draw_title": s.get("draw_title"),
        "telebirr_number": s.get("telebirr_number"),
        "telebirr_name": s.get("telebirr_name"),
        "cbe_number": s.get("cbe_number"),
        "cbe_name": s.get("cbe_name"),
    })

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "admin_count": len(ADMIN_IDS)
    }), 200

@app.route('/api/cron/cleanup', methods=['GET', 'POST'])
def cron_cleanup():
    secret = request.args.get('secret') or (request.get_json(silent=True) or {}).get('secret')
    if secret != (os.environ.get("CRON_SECRET") or "cleanup"):
        return jsonify({"success": False, "error": "unauthorized"}), 401
    n = cleanup_expired_pendings()
    return jsonify({"success": True, "released": n})

# ==================== ADMIN VERIFY ====================
@app.route('/api/admin/verify-password', methods=['POST'])
@limiter.limit("10 per minute")
def verify_admin_password():
    data = request.get_json(silent=True) or {}
    pwd = data.get('password', '')
    if not pwd:
        return jsonify({"success": False, "error": "Password required"}), 400
    if hmac.compare_digest(pwd, ADMIN_PASSWORD):
        return jsonify({"success": True, "token": _expected_admin_token()})
    return jsonify({"success": False, "error": "የተሳሳተ የይለፍ ቃል"}), 401

# ==================== WHOAMI (Debug) ====================
@app.route('/api/admin/whoami', methods=['GET'])
def whoami():
    init_data = request.headers.get('X-Telegram-Init-Data')
    uid = None
    if init_data and verify_telegram_data(init_data):
        try:
            parsed = dict(parse_qsl(init_data))
            ui = json.loads(parsed.get('user', '{}'))
            uid = str(ui.get('id'))
        except: pass
    return jsonify({
        "your_id": uid,
        "is_admin": uid in ADMIN_IDS if uid else False,
        "total_admins": len(ADMIN_IDS),
        "admin_ids": ADMIN_IDS
    })

# ==================== SUMMARY ====================
@app.route('/api/tickets/summary', methods=['GET'])
def tickets_summary():
    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='sold') AS sold,
                COUNT(*) FILTER (WHERE status='available') AS available,
                COUNT(*) FILTER (WHERE status='pending') AS pending,
                COUNT(*) FILTER (WHERE status='reserved') AS reserved,
                COALESCE(SUM(price_paid) FILTER (WHERE status='sold'), 0) AS total_revenue,
                COUNT(DISTINCT user_id) FILTER (WHERE user_id IS NOT NULL) AS unique_users
            FROM tickets;
        """)
        stats = cur.fetchone()
        stats['total'] = total
        stats['sold_pct'] = round((stats['sold'] / total) * 100, 1) if total else 0
        cur.close()
        res = jsonify(stats)
        res.headers['Cache-Control'] = 'public, max-age=15'
        return res
    except Exception as e:
        logger.error(f"Summary: {e}")
        return jsonify({"sold":0,"available":total,"pending":0,"reserved":0,"total":total,"sold_pct":0,"unique_users":0,"total_revenue":0}), 200
    finally:
        if conn: release_db_connection(conn)

# ==================== TICKETS LIST ====================
@app.route('/api/tickets/list', methods=['GET'])
def tickets_list():
    conn = None
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(500, max(20, int(request.args.get('per_page', 100))))
        filter_status = request.args.get('filter', 'all')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where = ""
        params = []
        if filter_status in ('available', 'sold', 'pending', 'reserved'):
            where = "WHERE status = %s"
            params.append(filter_status)

        offset = (page - 1) * per_page
        cur.execute(
            f"SELECT number, status FROM tickets {where} ORDER BY number LIMIT %s OFFSET %s",
            params + [per_page, offset]
        )
        rows = cur.fetchall()
        cur.close()
        tickets = {r['number']: {"status": r['status']} for r in rows}
        res = jsonify({
            "success": True, "page": page, "per_page": per_page,
            "tickets": tickets, "has_more": len(rows) == per_page
        })
        res.headers['Cache-Control'] = 'public, max-age=10'
        return res
    except Exception as e:
        logger.error(f"List: {e}")
        return jsonify({"success": False, "tickets": {}}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== TICKET SEARCH ====================
@app.route('/api/tickets/search', methods=['GET'])
def tickets_search():
    conn = None
    try:
        number = int(request.args.get('number', 0))
        total = get_int_setting("total_tickets", TOTAL_TICKETS)
        if number < 1 or number > total:
            return jsonify({"success": False, "message": "የተሳሳተ ቁጥር"}), 400
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, status FROM tickets WHERE number = %s", (number,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"success": False, "message": "አልተገኘም"}), 404
        return jsonify({"success": True, "ticket": {"number": row['number'], "status": row['status']}})
    except Exception as e:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== PRICING ====================
@app.route('/api/pricing', methods=['POST'])
def pricing_api():
    data = request.get_json(silent=True) or {}
    qty = int(data.get('quantity', 0))
    max_order = get_int_setting("max_per_order", MAX_TICKETS_PER_ORDER)
    if qty < 0 or qty > max_order:
        return jsonify({"success": False, "message": "የተሳሳተ ብዛት"}), 400
    try:
        tiers = json.loads(get_setting("pricing_tiers", json.dumps(PRICING_TIERS)) or "[]")
    except Exception:
        tiers = PRICING_TIERS
    base_price = get_int_setting("base_price", BASE_PRICE)
    if qty <= 0:
        return jsonify({"success": True, "pricing": {"total": 0, "unit": base_price, "discount": 0, "base_total": 0}})
    base_total = qty * base_price
    discount = 0
    for tier in tiers:
        if qty >= tier.get("min_qty", 999999):
            discount = tier.get("discount", 0)
            break
    total = max(base_total - discount, 0)
    return jsonify({"success": True, "pricing": {
        "total": total, "base_total": base_total, "discount": discount,
        "unit": round(total / qty, 2)
    }})

# ==================== RESERVE ====================
@app.route('/api/reserve-tickets', methods=['POST'])
def reserve_tickets():
    data = request.get_json(silent=True) or {}
    numbers = data.get('numbers', [])
    user_id = require_verified_user(request, data.get('user_id'))
    max_order = get_int_setting("max_per_order", MAX_TICKETS_PER_ORDER)
    timeout_min = get_int_setting("reservation_minutes", RESERVATION_TIMEOUT_MINUTES)

    if not numbers or not user_id or len(numbers) != len(set(numbers)):
        return jsonify({"success": False, "message": "አስፈላጊ መረጃ ጎድሏል"}), 400
    if len(numbers) > max_order:
        return jsonify({"success": False, "message": f"ቢያንስ {max_order} ቲኬቶች ብቻ"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, status FROM tickets WHERE number = ANY(%s) FOR UPDATE", (numbers,))
        rows = cur.fetchall()
        if len(rows) != len(set(numbers)) or any(r['status'] != 'available' for r in rows):
            conn.rollback()
            return jsonify({"success": False, "message": "አንዳንድ ቁጥሮች ተይዘዋል"}), 409

        cur.execute("""
            UPDATE tickets SET status='reserved', user_id=%s,
                reserved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        """, (user_id, numbers))
        conn.commit()
        cur.close()

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=timeout_min)).isoformat()
        return jsonify({
            "success": True,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "minutes": timeout_min
        })
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== SUBMIT ORDER ====================
@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    data = request.get_json(silent=True) or {}
    selected = data.get('numbers', [])
    user_id = require_verified_user(request, data.get('user_id'))
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_b64 = data.get('receipt_base64')

    if not selected or not user_id or not user_name or not user_phone or len(selected) != len(set(selected)):
        return jsonify({"success": False, "message": "እባክዎ ሁሉንም ይሙሉ"}), 400

    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    invalid = [n for n in selected if not isinstance(n, int) or n < 1 or n > total]
    if invalid:
        return jsonify({"success": False, "message": "የተሳሳተ ቁጥር"}), 400

    base_price = get_int_setting("base_price", BASE_PRICE)
    try:
        tiers = json.loads(get_setting("pricing_tiers", json.dumps(PRICING_TIERS)) or "[]")
    except Exception:
        tiers = PRICING_TIERS
    base_total = len(selected) * base_price
    discount = 0
    for tier in tiers:
        if len(selected) >= tier.get("min_qty", 999999):
            discount = tier.get("discount", 0)
            break
    total_price = max(base_total - discount, 0)

    order_id = f"ORD-{int(time.time())}-{random.randint(100, 999)}"
    price_per = Decimal(total_price) / Decimal(len(selected))
    price_per = price_per.quantize(Decimal('0.01'))

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT number, status, user_id FROM tickets WHERE number = ANY(%s) FOR UPDATE",
            (selected,)
        )
        rows = cur.fetchall()
        if len(rows) != len(set(selected)):
            conn.rollback()
            return jsonify({"success": False, "message": "ቁጥሮች አልተገኙም"}), 400
        if any(r['status'] != 'reserved' or str(r['user_id']) != user_id for r in rows):
            conn.rollback()
            return jsonify({"success": False, "message": "Reservation ጊዜው አልቋል"}), 409

        file_id = send_telegram_admin_notification(
            user_name, user_phone, selected, total_price,
            referrer, receipt_b64, user_id, order_id
        )

        cur.execute("""
            UPDATE tickets SET status='pending', user_id=%s, user_name=%s,
                user_phone=%s, referrer=%s, receipt_file_id=%s, price_paid=%s,
                order_id=%s, updated_at=CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        """, (user_id, user_name, user_phone, referrer,
              file_id or "uploaded", price_per, order_id, selected))
        conn.commit()
        cur.close()

        return jsonify({
            "success": True,
            "message": "ትዕዛዝዎ ተልኳል",
            "order_id": order_id,
            "total_price": total_price
        })
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== MY TICKETS ====================
@app.route('/api/my-tickets', methods=['GET'])
def get_my_tickets():
    phone_query = request.args.get('phone')

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if phone_query:
            phone_clean = phone_query.strip()
            cur.execute("""
                SELECT number, status, price_paid, updated_at, order_id, reserved_at
                FROM tickets
                WHERE (user_phone = %s OR user_phone LIKE %s)
                  AND status IN ('pending', 'sold', 'reserved')
                ORDER BY updated_at DESC
                LIMIT 100
            """, (phone_clean, f"%{phone_clean}%"))
        else:
            user_id = require_verified_user(request, request.args.get('user_id'))
            if not user_id:
                return jsonify([]), 400
            cur.execute("""
                SELECT number, status, price_paid, updated_at, order_id, reserved_at
                FROM tickets
                WHERE user_id = %s AND status IN ('pending', 'sold', 'reserved')
                ORDER BY updated_at DESC
            """, (str(user_id),))

        rows = cur.fetchall()
        cur.close()
        timeout_min = get_int_setting("reservation_minutes", RESERVATION_TIMEOUT_MINUTES)
        now = datetime.now(timezone.utc)
        for r in rows:
            if r['status'] == 'reserved' and r['reserved_at']:
                reserved = r['reserved_at']
                if reserved.tzinfo is None:
                    reserved = reserved.replace(tzinfo=timezone.utc)
                elapsed = (now - reserved).total_seconds()
                r['expires_in_seconds'] = int(max(0, timeout_min * 60 - elapsed))
            else:
                r['expires_in_seconds'] = None
            r['updated_at'] = r['updated_at'].isoformat() if r['updated_at'] else None
            r['reserved_at'] = r['reserved_at'].isoformat() if r['reserved_at'] else None
            r['price_paid'] = float(r['price_paid']) if r['price_paid'] else None
        return jsonify(rows)
    except Exception as e:
        logger.error(f"get_my_tickets: {e}")
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== USER INFO ====================
@app.route('/api/get-user-info', methods=['GET'])
def get_user_info():
    user_id = require_verified_user(request, request.args.get('user_id'))
    if not user_id:
        return jsonify({"success": False}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT first_name, phone_number, is_admin FROM users WHERE user_id = %s", (str(user_id),))
        user = cur.fetchone()
        cur.close()
        if user:
            return jsonify({
                "success": True,
                "name": user['first_name'],
                "phone": user['phone_number'],
                "is_admin": user.get('is_admin', False) or str(user_id) in ADMIN_IDS
            })
        return jsonify({"success": True, "name": None, "phone": None, "is_admin": str(user_id) in ADMIN_IDS})
    except Exception as e:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== REFERRAL ====================
@app.route('/api/referral-stats', methods=['GET'])
def get_referral_stats():
    user_id = require_verified_user(request, request.args.get('user_id'))
    if not user_id:
        return jsonify({"success": False}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT COUNT(DISTINCT user_id) AS total_referred,
                   COUNT(*) FILTER (WHERE status='sold') AS successful_purchases
            FROM tickets WHERE referrer = %s
        """, (str(user_id),))
        stats = cur.fetchone()
        cur.close()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== WINNERS ====================
@app.route('/api/winners', methods=['GET'])
def get_winners():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, name, ticket_number, round, photo,
                   user_phone, lottery_name, prize, created_at
            FROM winners
            ORDER BY created_at DESC
            LIMIT 50
        """)
        winners = cur.fetchall()
        cur.close()
        for w in winners:
            if w.get('created_at'):
                w['created_at'] = w['created_at'].isoformat()
        return jsonify(winners)
    except Exception as e:
        logger.error(f"get_winners: {e}")
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== BOT: Phone request ====================
def _send_phone_request(chat_id):
    if not chat_id:
        return
    try:
        phone_text = (
            f"📱 *ስልክ ቁጥር ያጋሩ*\n\n"
            f"ለመቀጠል ስልክ ቁጥርዎን ማጋራት ያስፈልጋል።\n\n"
            f"👇 *ከታች ያለውን ቁልፍ ይጫኑ*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔒 ስልክ ቁጥርዎ ለደህንነት ብቻ ያገለግላል።"
        )
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
            "chat_id": chat_id, "text": phone_text, "parse_mode": "Markdown",
            "reply_markup": {
                "keyboard": [[{"text": "📱 ስልክ ቁጥር አጋራ", "request_contact": True}]],
                "resize_keyboard": True, "one_time_keyboard": True
            }
        }, timeout=10)
    except Exception as e:
        logger.error(f"_send_phone_request: {e}")

# ==================== ADMIN BOT COMMANDS ====================
def _admin_help_text():
    return (
        "🛡️ *የአድሚን ትዕዛዞች*\n\n"
        "📊 `/stats` — ጠቅላላ ስታቲስቲክስ\n"
        "📋 `/pending` — በመጠባበቅ ላይ ያሉ ትዕዛዞች\n"
        "✅ `/approve ORD-xxx` — ትዕዛዝ አጽድቅ\n"
        "❌ `/reject ORD-xxx` — ትዕዛዝ ሰርዝ\n"
        "🔍 `/search 2456` — ቲኬት ፈልግ\n"
        "🎟️ `/status 1,2,3 sold` — የቲኬት ሁኔታ ቀይር\n"
        "🏆 `/winners` — የአሸናፊዎች ዝርዝር\n"
        "➕ `/addwinner Name | Phone | Ticket | Lottery | Prize`\n"
        "🗑️ `/delwinner ID` — አሸናፊ ሰርዝ\n"
        "📢 `/broadcast <መልእክት>` — ለሁሉም ላክ\n"
        "🆔 `/whoami` — የእርስዎ ID\n"
        "🔄 `/refresh` — አድስ"
    )

def _reply_admin(chat_id, text, parse_mode="Markdown", keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json=payload, timeout=10)
    except Exception as e:
        logger.error(f"reply_admin: {e}")

def _admin_stats():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='sold') AS sold,
                COUNT(*) FILTER (WHERE status='available') AS available,
                COUNT(*) FILTER (WHERE status='pending') AS pending,
                COUNT(*) FILTER (WHERE status='reserved') AS reserved,
                COALESCE(SUM(price_paid) FILTER (WHERE status='sold'), 0) AS revenue,
                COUNT(DISTINCT user_id) FILTER (WHERE user_id IS NOT NULL) AS users
            FROM tickets;
        """)
        d = cur.fetchone()
        cur.close()
        return (
            f"📊 *ስታቲስቲክስ*\n\n"
            f"✅ የተሸጡ: *{d['sold']:,}*\n"
            f"🟢 ነፃ: *{d['available']:,}*\n"
            f"🟡 በመጠባበቅ: *{d['pending']:,}*\n"
            f"🔵 የተያዙ: *{d['reserved']:,}*\n"
            f"💰 ገቢ: *{int(d['revenue'] or 0):,}* ብር\n"
            f"👥 ተጠቃሚዎች: *{d['users']:,}*"
        )
    except Exception as e:
        logger.error(f"_admin_stats: {e}")
        return "❌ ስህተት ተፈጥሯል"
    finally:
        if conn: release_db_connection(conn)

def _admin_pending():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT order_id, MAX(user_name) AS name, MAX(user_phone) AS phone,
                   COALESCE(SUM(price_paid), 0) AS total,
                   ARRAY_AGG(number ORDER BY number) AS nums,
                   MAX(updated_at) AS created
            FROM tickets
            WHERE order_id IS NOT NULL AND status='pending'
            GROUP BY order_id
            ORDER BY MAX(updated_at) DESC
            LIMIT 15
        """)
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return "✨ በመጠባበቅ ላይ ያለ ትዕዛዝ የለም"
        text = f"📋 *በመጠባበቅ ላይ ({len(rows)})*\n\n"
        for r in rows:
            nums_str = ",".join(map(str, r['nums'] or []))[:40]
            text += (
                f"🆔 `{r['order_id']}`\n"
                f"👤 {r['name'] or '-'}\n"
                f"📞 {r['phone'] or '-'}\n"
                f"🎟️ {nums_str}\n"
                f"💰 {int(r['total']):,} ብር\n"
                f"✅ /approve {r['order_id']}\n"
                f"❌ /reject {r['order_id']}\n"
                f"━━━━━━━━━━━━━━━\n"
            )
        return text
    except Exception as e:
        logger.error(f"_admin_pending: {e}")
        return "❌ ስህተት"
    finally:
        if conn: release_db_connection(conn)

def _admin_approve(order_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id, status FROM tickets WHERE order_id=%s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets:
            return "❌ ትዕዛዙ አልተገኘም"
        if any(t['status'] != 'pending' for t in tickets):
            return "⚠️ አስቀድሞ ተስተካክሏል"
        nums = [t['number'] for t in tickets]
        target = tickets[0]['user_id']
        cur.execute("UPDATE tickets SET status='sold', updated_at=CURRENT_TIMESTAMP WHERE order_id=%s", (order_id,))
        conn.commit()
        cur.close()
        send_telegram_push(target, f"🎉 *ትዕዛዝዎ ፀድቋል!*\n\n🆔 `{order_id}`\n🎟️ ቁጥሮች: `{','.join(map(str, nums))}`\n\n🎊 መልካም ዕድል!")
        log_audit_action('Bot', "APPROVE", order_id)
        return f"✅ *ተፀድቋል*\n🆔 `{order_id}`\n🎟️ {','.join(map(str, nums))}"
    except Exception as e:
        if conn: conn.rollback()
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_reject(order_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id FROM tickets WHERE order_id=%s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets:
            return "❌ ትዕዛዙ አልተገኘም"
        nums = [t['number'] for t in tickets]
        target = tickets[0]['user_id']
        cur.execute("""
            UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
                user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
                order_id=NULL, reserved_at=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE order_id=%s
        """, (order_id,))
        conn.commit()
        cur.close()
        send_telegram_push(target, f"❌ *ትዕዛዝዎ ተሰርዟል*\n\n🆔 `{order_id}`")
        log_audit_action('Bot', "REJECT", order_id)
        return f"❌ *ተሰርዟል*\n🆔 `{order_id}`\n🎟️ {','.join(map(str, nums))}"
    except Exception as e:
        if conn: conn.rollback()
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_search(number):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT number, status, user_name, user_phone, price_paid, order_id
            FROM tickets WHERE number=%s
        """, (number,))
        r = cur.fetchone()
        cur.close()
        if not r:
            return f"❌ ቁጥር #{number} አልተገኘም"
        emoji = {'sold':'✅','available':'🟢','pending':'🟡','reserved':'🔵'}.get(r['status'], '❓')
        text = f"🎟️ *ቲኬት #{r['number']}*\n\n"
        text += f"{emoji} ሁኔታ: *{r['status'].upper()}*\n"
        if r['user_name']: text += f"👤 {r['user_name']}\n"
        if r['user_phone']: text += f"📞 {r['user_phone']}\n"
        if r['price_paid']: text += f"💰 {float(r['price_paid']):,.2f} ብር\n"
        if r['order_id']: text += f"🆔 `{r['order_id']}`"
        return text
    except Exception as e:
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_update_tickets(nums, status):
    if status not in ('sold', 'available', 'pending', 'reserved'):
        return "❌ ሁኔታ: sold / available / pending / reserved"
    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    nums = [n for n in nums if 1 <= n <= total]
    if not nums:
        return "❌ የተሳሳተ ቁጥር"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if status == 'available':
            cur.execute("""
                UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
                    user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
                    order_id=NULL, reserved_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE number = ANY(%s)
            """, (nums,))
        else:
            cur.execute("UPDATE tickets SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE number = ANY(%s)",
                        (status, nums))
        conn.commit()
        cur.close()
        log_audit_action('Bot', "UPDATE_STATUS", f"{nums} → {status}")
        return f"✅ {len(nums)} ቲኬቶች → *{status.upper()}*"
    except Exception as e:
        if conn: conn.rollback()
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_winners_list():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, name, ticket_number, user_phone, lottery_name, prize FROM winners ORDER BY created_at DESC LIMIT 15")
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return "🏆 አሸናፊ የለም"
        text = f"🏆 *አሸናፊዎች ({len(rows)})*\n\n"
        for w in rows:
            text += f"🆔 `{w['id']}` — 🏆 *{w['name']}*\n"
            if w['user_phone']: text += f"📞 {w['user_phone']}\n"
            text += f"🎟️ #{w['ticket_number']}"
            if w['lottery_name']: text += f" · 🎰 {w['lottery_name']}"
            if w['prize']: text += f"\n🎁 {w['prize']}"
            text += f"\n🗑️ /delwinner {w['id']}\n━━━━━━━━━━━━━━━\n"
        return text
    except Exception as e:
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_add_winner(args):
    parts = [p.strip() for p in args.split('|')]
    if len(parts) < 3:
        return ("❌ አጠቃቀም:\n"
                "`/addwinner Name | Phone | Ticket | Lottery | Prize`\n\n"
                "ምሳሌ:\n"
                "`/addwinner Abel | 251911508813 | 1749 | 4 Gech | BYD YUAN UP`")
    name = parts[0]
    phone = parts[1]
    try:
        ticket = int(parts[2])
    except:
        return "❌ ቲኬት ቁጥር የተሳሳተ ነው"
    lottery = parts[3] if len(parts) > 3 else ''
    prize = parts[4] if len(parts) > 4 else ''
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO winners (name, ticket_number, round, photo, user_phone, lottery_name, prize)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (name, ticket, 'Round 1', '', phone, lottery, prize))
        conn.commit()
        cur.close()
        log_audit_action('Bot', "ADD_WINNER", f"{name} #{ticket}")
        return f"✅ *ተጨምሯል*\n🏆 {name}\n🎟️ #{ticket}"
    except Exception as e:
        if conn: conn.rollback()
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_del_winner(wid):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM winners WHERE id=%s", (wid,))
        conn.commit()
        deleted = cur.rowcount
        cur.close()
        return f"✅ ተሰርዟል" if deleted else "❌ አልተገኘም"
    except Exception as e:
        if conn: conn.rollback()
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

def _admin_broadcast(msg):
    if not msg.strip():
        return "❌ መልእክት አስገቡ"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT user_id FROM tickets WHERE user_id IS NOT NULL")
        uids = [r['user_id'] for r in cur.fetchall() if r['user_id']]
        cur.close()
        sent = 0
        for uid in uids:
            send_telegram_push(uid, f"📢 *ማሳወቂያ*\n\n{msg}")
            sent += 1
            if sent % 25 == 0:
                time.sleep(1)
        log_audit_action('Bot', "BROADCAST", f"{sent} users")
        return f"✅ ለ *{sent}* ተጠቃሚዎች ተልኳል"
    except Exception as e:
        return f"❌ {e}"
    finally:
        if conn: release_db_connection(conn)

# ==================== BOT WEBHOOK ====================
@app.route('/bot/webhook', methods=['POST'])
def bot_webhook_handler():
    if not BOT_TOKEN:
        return jsonify({"status": "error"}), 500
    data = request.get_json(silent=True) or {}

    message = data.get('message')
    if message:
        chat_id = message.get('chat', {}).get('id')
        text = (message.get('text') or '').strip()
        contact = message.get('contact')
        first_name = message.get('from', {}).get('first_name', 'Friend')
        username = message.get('from', {}).get('username', '')

        # ==================== ADMIN COMMANDS ====================
        if text == '/help':
            _reply_admin(chat_id, _admin_help_text())
            return jsonify({"status": "ok"}), 200

        if text.startswith('/') and str(chat_id) in ADMIN_IDS:
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ''

            if cmd == '/admin' or cmd == '/menu':
                _reply_admin(chat_id, _admin_help_text())

            elif cmd == '/stats':
                _reply_admin(chat_id, _admin_stats())

            elif cmd == '/pending':
                _reply_admin(chat_id, _admin_pending())

            elif cmd == '/approve':
                if not args:
                    _reply_admin(chat_id, "❌ አጠቃቀም: `/approve ORD-xxx`")
                else:
                    _reply_admin(chat_id, _admin_approve(args))

            elif cmd == '/reject':
                if not args:
                    _reply_admin(chat_id, "❌ አጠቃቀም: `/reject ORD-xxx`")
                else:
                    _reply_admin(chat_id, _admin_reject(args))

            elif cmd == '/search':
                try:
                    n = int(args)
                    _reply_admin(chat_id, _admin_search(n))
                except:
                    _reply_admin(chat_id, "❌ አጠቃቀም: `/search 2456`")

            elif cmd == '/status':
                parts2 = args.split(maxsplit=1)
                if len(parts2) < 2:
                    _reply_admin(chat_id, "❌ አጠቃቀም: `/status 1,2,3 sold`")
                else:
                    try:
                        nums = [int(n.strip()) for n in parts2[0].split(',') if n.strip().isdigit()]
                        _reply_admin(chat_id, _admin_update_tickets(nums, parts2[1].strip().lower()))
                    except:
                        _reply_admin(chat_id, "❌ ቁጥሮች የተሳሳቱ ናቸው")

            elif cmd == '/winners':
                _reply_admin(chat_id, _admin_winners_list())

            elif cmd == '/addwinner':
                _reply_admin(chat_id, _admin_add_winner(args))

            elif cmd == '/delwinner':
                try:
                    _reply_admin(chat_id, _admin_del_winner(int(args)))
                except:
                    _reply_admin(chat_id, "❌ አጠቃቀም: `/delwinner 5`")

            elif cmd == '/broadcast':
                _reply_admin(chat_id, _admin_broadcast(args))

            elif cmd == '/whoami':
                _reply_admin(chat_id,
                    f"🆔 Your ID: `{chat_id}`\n"
                    f"👤 Admin: *✅ YES*\n"
                    f"📊 Total admins: *{len(ADMIN_IDS)}*\n"
                    f"🔢 IDs: `{','.join(ADMIN_IDS)}`"
                )

            elif cmd == '/refresh':
                _reply_admin(chat_id, "🔄 ተዘምኗል\n\n" + _admin_stats())

            return jsonify({"status": "ok"}), 200

        # ==================== REGULAR BOT LOGIC ====================
        if text == '/start':
            welcome_text = (
                f"👋 ሰላም *{first_name}*!\n\n"
                f"🚗 *Getachew Fikadu Jirata* የቲኬት ዕድል መተግበሪያ እንኳን ደህና መጡ!\n\n"
                f"🎟️ የ BYD Leopard 5 መኪና ዕድል ለመግዛት ይህ ቦት ያገለግልዎታል።\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🌍 *እባክዎ ቋንቋ ይምረጡ*"
            )
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": welcome_text, "parse_mode": "Markdown",
                "reply_markup": {
                    "keyboard": [
                        [{"text": "🇪🇹 አማርኛ"}],
                        [{"text": "🇪🇹 Afaan Oromoo"}],
                        [{"text": "🇬🇧 English"}]
                    ],
                    "resize_keyboard": True, "one_time_keyboard": True
                }
            }, timeout=10)

        elif any(lang in text for lang in ["አማርኛ", "Oromoo", "English"]):
            _send_phone_request(chat_id)

        if contact:
            phone = contact.get('phone_number')
            uid = str(message.get('from', {}).get('id'))
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO users (user_id, first_name, username, phone_number)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        phone_number = EXCLUDED.phone_number,
                        first_name = EXCLUDED.first_name,
                        username = EXCLUDED.username
                """, (uid, first_name, username, phone))
                conn.commit()
                cur.close()

                success_text = (
                    f"✅ *ስልክ ቁጥርዎ ተቀብለናል!*\n\n"
                    f"📱 ስልክ: `{phone}`\n"
                    f"👤 ስም: *{first_name}*\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🎟️ *የቲኬት መተግበሪያውን ለመክፈት* ከታች ያለውን ቁልፍ ይጫኑ"
                )
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id, "text": success_text, "parse_mode": "Markdown",
                    "reply_markup": {"inline_keyboard": [[{
                        "text": "🎟️ የቲኬት መተግበሪያን ክፈት",
                        "web_app": {"url": WEB_APP_URL}
                    }]]}
                }, timeout=10)
            except Exception as e:
                logger.error(f"Contact save: {e}")
            finally:
                if conn: release_db_connection(conn)
        return jsonify({"status": "ok"}), 200

    # ==================== CALLBACKS ====================
    cb = data.get('callback_query')
    if not cb:
        return jsonify({"status": "ok"}), 200

    cb_id = cb.get('id')
    from_id = str(cb.get('from', {}).get('id', ''))
    raw = cb.get('data', '')

    if raw.startswith('lang_'):
        lang_code = raw.replace('lang_', '')
        lang_names = {'am': 'አማርኛ', 'om': 'Afaan Oromoo', 'en': 'English'}
        lang_name = lang_names.get(lang_code, 'አማርኛ')
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": f"✅ {lang_name}"}, timeout=5)
        msg = cb.get('message') or {}
        chat_id = msg.get('chat', {}).get('id') or cb.get('from', {}).get('id')
        if chat_id:
            _send_phone_request(chat_id)
        return jsonify({"status": "ok"}), 200

    if from_id not in ADMIN_IDS:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": "❌ አድሚን አይደሉም", "show_alert": True}, timeout=5)
        return jsonify({"status": "unauthorized"}), 200

    if ':' not in raw or raw.split(':', 1)[0] not in ('app', 'rej'):
        return jsonify({"status": "ignored"}), 200
    action, order_id = raw.split(':', 1)

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id, status FROM tickets WHERE order_id = %s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets:
            msg = "⚠️ ትዕዛዙ አልተገኘም"
        elif any(t['status'] != 'pending' for t in tickets):
            msg = "⚠️ አስቀድሞ ተስተካክሏል"
        else:
            nums = [t['number'] for t in tickets]
            target = tickets[0]['user_id']
            if action == 'app':
                cur.execute("UPDATE tickets SET status='sold', updated_at=CURRENT_TIMESTAMP WHERE order_id=%s", (order_id,))
                msg = f"✅ #{order_id} ፀድቋል"
                push = f"🎉 *ትዕዛዝዎ ፀድቋል!*\n\n🆔 Order: `{order_id}`\n🎟️ ቁጥሮች: `{','.join(map(str, nums))}`\n\n🎊 መልካም ዕድል!"
            else:
                cur.execute("""UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
                    user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
                    order_id=NULL, reserved_at=NULL WHERE order_id=%s""", (order_id,))
                msg = f"❌ #{order_id} ተሰርዟል"
                push = f"❌ *ትዕዛዝዎ ተሰርዟል*\n\n🆔 Order: `{order_id}`\n\nእባክዎ እንደገና ይሞክሩ።"
            conn.commit()
            send_telegram_push(target, push)
            log_audit_action(from_id, f"CALLBACK_{action.upper()}", f"Order {order_id}")
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": msg, "show_alert": True}, timeout=5)
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"CB error: {e}")
    finally:
        if conn: release_db_connection(conn)
    return jsonify({"status": "ok"}), 200

# ==================== ADMIN: SETTINGS ====================
@app.route('/api/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if request.method == 'GET':
        s = get_all_settings()
        try:
            s['pricing_tiers'] = json.loads(s.get('pricing_tiers') or "[]")
        except Exception:
            s['pricing_tiers'] = PRICING_TIERS
        for k in ('base_price', 'total_tickets', 'max_per_order', 'reservation_minutes'):
            try: s[k] = int(s[k])
            except Exception: pass
        return jsonify({"success": True, "settings": s})

    data = request.get_json(silent=True) or {}
    allowed = [
        "product_name", "subtitle", "image_url", "draw_end_at", "draw_title",
        "base_price", "total_tickets", "max_per_order", "reservation_minutes",
        "pricing_tiers", "telebirr_number", "telebirr_name", "cbe_number", "cbe_name"
    ]
    saved = []
    for key in allowed:
        if key in data:
            val = data[key]
            if key == "pricing_tiers" and isinstance(val, (list, dict)):
                val = json.dumps(val)
            if set_setting(key, val):
                saved.append(key)

    log_audit_action('Admin', "UPDATE_SETTINGS", f"Keys: {saved}")
    return jsonify({"success": True, "saved": saved})

# ==================== ADMIN: ORDERS ====================
@app.route('/api/admin/orders', methods=['GET'])
def admin_orders_list():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    status_filter = request.args.get('status', 'pending')
    limit = min(500, max(20, int(request.args.get('limit', 200))))
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT order_id,
                   MAX(user_name) AS user_name,
                   MAX(user_phone) AS user_phone,
                   COALESCE(SUM(price_paid), 0) AS total_price,
                   ARRAY_AGG(number ORDER BY number) AS numbers,
                   MAX(updated_at) AS created_at,
                   MAX(status) AS status
            FROM tickets
            WHERE order_id IS NOT NULL AND status = %s
            GROUP BY order_id
            ORDER BY MAX(updated_at) DESC
            LIMIT %s
        """, (status_filter, limit))
        rows = cur.fetchall()
        cur.close()
        out = []
        for r in rows:
            out.append({
                "order_id": r['order_id'],
                "user_name": r['user_name'],
                "user_phone": r['user_phone'],
                "total_price": float(r['total_price'] or 0),
                "numbers": r['numbers'] or [],
                "created_at": r['created_at'].isoformat() if r['created_at'] else None,
                "status": r['status']
            })
        return jsonify({"success": True, "orders": out})
    except Exception as e:
        logger.error(f"admin orders: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ADMIN: APPROVE ====================
@app.route('/api/admin/approve-order', methods=['POST'])
def admin_approve_order():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({"success": False, "error": "order_id required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id, status FROM tickets WHERE order_id = %s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets:
            return jsonify({"success": False, "error": "Order not found"}), 404
        if any(t['status'] != 'pending' for t in tickets):
            return jsonify({"success": False, "error": "Order already processed"}), 409
        nums = [t['number'] for t in tickets]
        target = tickets[0]['user_id']
        cur.execute("UPDATE tickets SET status='sold', updated_at=CURRENT_TIMESTAMP WHERE order_id=%s", (order_id,))
        conn.commit()
        cur.close()
        send_telegram_push(target, f"🎉 ትዕዛዝዎ #{order_id} ፀድቋል! ቁጥሮች: {nums}")
        log_audit_action('Admin', "APPROVE_ORDER", order_id)
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ADMIN: REJECT ====================
@app.route('/api/admin/reject-order', methods=['POST'])
def admin_reject_order():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({"success": False, "error": "order_id required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id FROM tickets WHERE order_id = %s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets:
            return jsonify({"success": False, "error": "Order not found"}), 404
        nums = [t['number'] for t in tickets]
        target = tickets[0]['user_id']
        cur.execute("""
            UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
                user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
                order_id=NULL, reserved_at=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE order_id=%s
        """, (order_id,))
        conn.commit()
        cur.close()
        send_telegram_push(target, f"❌ ትዕዛዝዎ #{order_id} ተሰርዟል")
        log_audit_action('Admin', "REJECT_ORDER", order_id)
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ADMIN: BROADCAST ====================
@app.route('/api/admin/broadcast', methods=['POST'])
def broadcast_message():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    msg = data.get('message')
    if not msg:
        return jsonify({"error": "Message required"}), 400
    result = _admin_broadcast(msg)
    return jsonify({"success": True, "message": result})

# ==================== ADMIN: EXPORT ====================
@app.route('/api/admin/export-orders', methods=['GET'])
def export_orders():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT number, status, user_name, user_phone, price_paid, order_id, updated_at
            FROM tickets WHERE status != 'available' ORDER BY updated_at DESC
        """)
        orders = cur.fetchall()
        cur.close()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['Order_ID', 'Ticket', 'Status', 'Name', 'Phone', 'Price', 'Date'])
        for o in orders:
            w.writerow([o['order_id'], o['number'], o['status'], o['user_name'],
                        o['user_phone'], o['price_paid'], o['updated_at']])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-disposition": "attachment; filename=orders.csv"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ADMIN: UPDATE TICKET ====================
@app.route('/api/admin/update-ticket-status', methods=['POST'])
def update_ticket_status():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    nums = data.get('ticket_numbers', [])
    new_status = data.get('status')
    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    if not nums or new_status not in ['sold', 'available', 'reserved', 'pending']:
        return jsonify({"error": "Invalid"}), 400
    nums = [n for n in nums if isinstance(n, int) and 1 <= n <= total]
    if not nums:
        return jsonify({"error": "No valid tickets"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT user_id FROM tickets WHERE number = ANY(%s)", (nums,))
        uids = [r['user_id'] for r in cur.fetchall() if r['user_id']]

        if new_status == 'available':
            cur.execute("""
                UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
                    user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
                    order_id=NULL, reserved_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE number = ANY(%s)
            """, (nums,))
        else:
            cur.execute("UPDATE tickets SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE number = ANY(%s)",
                        (new_status, nums))
        conn.commit()
        cur.close()
        log_audit_action('Admin', "UPDATE_STATUS", f"{nums} → {new_status}")

        if new_status == 'sold':
            push = f"🎉 ትዕዛዝዎ ፀድቋል! ቁጥሮች: {nums}"
            for uid in uids: send_telegram_push(uid, push)
        elif new_status == 'available':
            push = f"⚠️ ቁጥሮችዎ {nums} ተሰርዘዋል"
            for uid in uids: send_telegram_push(uid, push)
        return jsonify({"success": True, "message": f"{len(nums)} ቲኬቶች ተሻሻሉ"})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ADMIN: ADD WINNER ====================
@app.route('/api/admin/add-winner', methods=['POST'])
def add_winner():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    tn = data.get('ticket_number')
    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    if not name or not isinstance(tn, int) or tn < 1 or tn > total:
        return jsonify({"error": "Invalid"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO winners
                (name, ticket_number, round, photo, user_phone, lottery_name, prize)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            name, tn,
            data.get('round', 'Round 1'),
            data.get('photo', ''),
            data.get('phone', ''),
            data.get('lottery', ''),
            data.get('prize', '')
        ))
        conn.commit()
        cur.close()
        log_audit_action('Admin', "ADD_WINNER", f"{name} #{tn}")
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ADMIN: DELETE WINNER ====================
@app.route('/api/admin/delete-winner', methods=['POST'])
def delete_winner():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    wid = data.get('id')
    if not wid:
        return jsonify({"error": "id required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM winners WHERE id = %s", (wid,))
        conn.commit()
        cur.close()
        log_audit_action('Admin', "DELETE_WINNER", f"id={wid}")
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ERROR HANDLER ====================
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled: {e}")
    return jsonify({"success": False, "error": str(e)}), 500

# ==================== RUN ====================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
