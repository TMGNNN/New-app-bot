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
import secrets
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
IS_VERCEL = os.environ.get("VERCEL") == "1"
IS_PRODUCTION = os.environ.get("FLASK_ENV") == "production" or os.environ.get("RENDER") == "true"

# ─── SECURITY: No default password! ───
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")           # plaintext (bootstrap)
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH") # scrypt$... (preferred)
CRON_SECRET = os.environ.get("CRON_SECRET")

if not ADMIN_PASSWORD and not ADMIN_PASSWORD_HASH:
    logger.critical("❌ ADMIN_PASSWORD or ADMIN_PASSWORD_HASH not set — admin login DISABLED")
if not CRON_SECRET:
    logger.critical("❌ CRON_SECRET not set — /api/cron/cleanup DISABLED")
if ADMIN_PASSWORD and not ADMIN_PASSWORD_HASH:
    logger.warning("⚠️ Using plaintext ADMIN_PASSWORD — set ADMIN_PASSWORD_HASH for production")

ADMIN_IDS = [a.strip() for a in os.environ.get("ADMIN_IDS", "").split(",") if a.strip()]

# ─── Password hashing (scrypt — stdlib, no external deps) ───
def hash_password(password: str, salt: bytes = None) -> str:
    """Format: scrypt$N$r$p$b64(salt)$b64(hash)"""
    if salt is None:
        salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"

def verify_password(password: str, hashed: str) -> bool:
    try:
        parts = hashed.split('$')
        if len(parts) != 6 or parts[0] != 'scrypt':
            return False
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
        dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False

# ==================== PRICING ====================
TOTAL_TICKETS = 3500
BASE_PRICE = 3500
MAX_TICKETS_PER_ORDER = 20
RESERVATION_TIMEOUT_MINUTES = 15
RECEIPT_MAX_BYTES = 5 * 1024 * 1024
RECEIPT_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}

PRICING_TIERS = [
    {"min_qty": 5, "discount": 1500},
    {"min_qty": 3, "discount": 500},
]

DEFAULT_SETTINGS = {
    "product_name": "BYD Sealion 6",
    "subtitle": "Shark Grey",
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
    "draw_title": "BYD Sealion 6 Giveaway",
}

# ==================== APP ====================
app = Flask(__name__, static_folder='.', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

CORS(app, resources={r"/api/*": {
    "origins": "*",
    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "X-Telegram-Init-Data", "X-Admin-Token"]
}}, supports_credentials=False)

limiter = Limiter(get_remote_address, app=app,
                  default_limits=["1000 per day", "500 per hour"],
                  storage_uri="memory://")

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
    if not DATABASE_URL: return
    try:
        db_url = DATABASE_URL
        if ("supabase.co" in db_url or "pooler.supabase.com" in db_url) and "sslmode" not in db_url:
            db_url += ("&" if "?" in db_url else "?") + "sslmode=require"
        db_pool = ThreadedConnectionPool(minconn=1, maxconn=20, dsn=db_url)
        logger.info("✅ DB Pool ready")
    except Exception as e:
        logger.error(f"❌ DB Pool failed: {e}")

def close_db_pool():
    if db_pool: db_pool.closeall()

atexit.register(close_db_pool)

def get_db_connection():
    if not db_pool: raise Exception("DB not configured")
    return db_pool.getconn()

def release_db_connection(conn):
    if db_pool and conn: db_pool.putconn(conn)

# ==================== AUTH ====================
def verify_telegram_data(init_data: str) -> bool:
    if not BOT_TOKEN or not init_data: return False
    try:
        parsed = dict(parse_qsl(init_data))
        if 'hash' not in parsed: return False
        auth_date = int(parsed.get('auth_date', '0'))
        if not auth_date or time.time() - auth_date > 86400: return False
        hash_to_check = parsed.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, hash_to_check)
    except Exception as e:
        logger.error(f"initData verify: {e}")
        return False

def get_verified_user_id(req):
    json_data = req.get_json(silent=True) or {}
    init_data = req.headers.get("X-Telegram-Init-Data") or json_data.get("initData")
    if not init_data or not verify_telegram_data(init_data): return None
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

# ─── ADMIN SESSIONS (server-restart invalidates all tokens) ───
_admin_sessions = {}  # {token: {"exp": ts, "uid": str, "ip": str}}
ADMIN_SESSION_TTL = 3600  # 1 hour

def _issue_admin_session(user_id: str, ip: str) -> str:
    tok = secrets.token_urlsafe(32)
    _admin_sessions[tok] = {
        "exp": time.time() + ADMIN_SESSION_TTL,
        "uid": str(user_id),
        "ip": ip,
    }
    # Prune expired
    now = time.time()
    for k in list(_admin_sessions.keys()):
        if _admin_sessions[k]["exp"] < now:
            _admin_sessions.pop(k, None)
    return tok

def _check_admin_session(tok: str, ip: str) -> dict:
    if not tok: return None
    s = _admin_sessions.get(tok)
    if not s: return None
    if s["exp"] < time.time():
        _admin_sessions.pop(tok, None)
        return None
    # Optional: bind to IP for defense in depth
    # if s["ip"] != ip: return None
    return s

def is_authorized_admin(req):
    token = req.headers.get('X-Admin-Token')
    if token and _check_admin_session(token, req.remote_addr):
        return True
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
def esc_md(s):
    if s is None: return ''
    return str(s).replace('\\', '\\\\').replace('*', '\\*').replace('_', '\\_') \
                 .replace('[', '\\[').replace(']', '\\]').replace('`', '\\`')

def send_telegram_push(chat_id, text):
    if not (BOT_TOKEN and chat_id): return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5)
    except Exception as e:
        logger.error(f"Push error: {e}")

def send_admin_notification(user_name, user_phone, numbers, total_price,
                             referrer, receipt_data_url, user_id, order_id):
    """Returns (file_id, ok). Called AFTER DB commit."""
    if not (ADMIN_CHAT_ID and BOT_TOKEN): return None, False
    nums_str = ",".join(map(str, numbers))
    caption = (
        f"🆕 *አዲስ የቲኬት ትዕዛዝ*\n\n"
        f"🆔 `{esc_md(order_id)}`\n"
        f"👤 {esc_md(user_name)}\n"
        f"📞 {esc_md(user_phone)}\n"
        f"🎟️ {esc_md(nums_str)}\n"
        f"💰 {total_price:,} ብር\n"
        f"🔗 {esc_md(referrer)}\n"
        f"🆔 {esc_md(user_id)}"
    )
    reply_markup = {"inline_keyboard": [[
        {"text": "✅ ፅድቅ", "callback_data": f"app:{order_id}"},
        {"text": "❌ ሰርዝ", "callback_data": f"rej:{order_id}"}
    ]]}
    file_id = None
    try:
        if receipt_data_url and "," in receipt_data_url:
            _, encoded = receipt_data_url.split(",", 1)
            image_data = base64.b64decode(encoded)
            files = {'photo': ('receipt.jpg', io.BytesIO(image_data), 'image/jpeg')}
            payload = {'chat_id': ADMIN_CHAT_ID, 'caption': caption,
                       'parse_mode': 'Markdown', 'reply_markup': json.dumps(reply_markup)}
            res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                                data=payload, files=files, timeout=30).json()
            if res.get('ok'):
                photos = res['result'].get('photo', [])
                if photos: file_id = photos[-1]['file_id']
                return file_id, True
        # Text-only fallback
        res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": ADMIN_CHAT_ID, "text": caption,
                                  "parse_mode": "Markdown",
                                  "reply_markup": reply_markup}, timeout=10).json()
        return None, res.get('ok', False)
    except Exception as e:
        logger.error(f"Admin notify: {e}")
        return None, False

def log_audit_action(admin_id, action, details):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO audit_logs (admin_id, action, details) VALUES (%s, %s, %s)",
                    (str(admin_id), action, details))
        conn.commit(); cur.close()
    except Exception as e:
        logger.error(f"Audit: {e}")
    finally:
        if conn: release_db_connection(conn)

# ==================== SETTINGS ====================
def get_setting(key, default=None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone(); cur.close()
        if row: return row['value']
        return default if default is not None else DEFAULT_SETTINGS.get(key)
    except Exception:
        return default if default is not None else DEFAULT_SETTINGS.get(key)
    finally:
        if conn: release_db_connection(conn)

def set_setting(key, value):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""INSERT INTO settings (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                       updated_at = CURRENT_TIMESTAMP""", (key, str(value)))
        conn.commit(); cur.close()
        return True
    except Exception as e:
        logger.error(f"set_setting: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: release_db_connection(conn)

def get_all_settings():
    result = dict(DEFAULT_SETTINGS)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT key, value FROM settings")
        for r in cur.fetchall(): result[r['key']] = r['value']
        cur.close()
    except Exception as e:
        logger.error(f"get_all_settings: {e}")
    finally:
        if conn: release_db_connection(conn)
    return result

def get_int_setting(key, fallback):
    try: return int(get_setting(key, fallback))
    except Exception: return fallback

# ==================== DB INIT (never drops, adds orders table) ====================
def init_db():
    if not DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL not set")
        return
    db_url = DATABASE_URL
    if ("supabase.co" in db_url or "pooler.supabase.com" in db_url) and "sslmode" not in db_url:
        db_url += ("&" if "?" in db_url else "?") + "sslmode=require"
    try:
        conn = psycopg2.connect(db_url); conn.autocommit = True
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ─── ORDERS TABLE (main entity) ───
        cur.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_id VARCHAR(50) PRIMARY KEY,
                user_id VARCHAR(50) NOT NULL,
                user_name VARCHAR(100),
                user_phone VARCHAR(50),
                referrer VARCHAR(100),
                ticket_count INTEGER NOT NULL DEFAULT 0,
                total_price NUMERIC(10,2) DEFAULT 0,
                receipt_file_id TEXT,
                receipt_mime VARCHAR(50),
                receipt_size INTEGER,
                order_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                payment_status VARCHAR(20) NOT NULL DEFAULT 'unpaid',
                rejection_reason TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT orders_status_chk CHECK (order_status IN ('pending','approved','rejected','expired')),
                CONSTRAINT orders_payment_chk CHECK (payment_status IN ('unpaid','paid','refunded'))
            );
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(order_status);")

        # ─── USERS ───
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(50) PRIMARY KEY,
                first_name VARCHAR(100),
                username VARCHAR(100),
                phone_number VARCHAR(50),
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # ─── TICKETS ───
        cur.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY,
                status VARCHAR(20) DEFAULT 'available',
                user_id VARCHAR(50),
                user_name VARCHAR(100),
                user_phone VARCHAR(50),
                referrer VARCHAR(100),
                receipt_file_id TEXT,
                price_paid NUMERIC(10,2),
                order_id VARCHAR(50),
                reserved_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # ─── WINNERS / AUDIT / SETTINGS ───
        cur.execute('''
            CREATE TABLE IF NOT EXISTS winners (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100), ticket_number INTEGER,
                round VARCHAR(50), photo TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                admin_id VARCHAR(50), action VARCHAR(100), details TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY, value TEXT,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_order_id ON tickets(order_id);")

        # Populate tickets if empty
        cur.execute("SELECT COUNT(*) AS count FROM tickets;")
        count = cur.fetchone()['count']
        total = int(get_setting("total_tickets", TOTAL_TICKETS))
        if count < total:
            existing = {r['number'] for r in cur.execute("SELECT number FROM tickets") or []}
            cur.execute("SELECT number FROM tickets")
            existing = {r['number'] for r in cur.fetchall()}
            data = [(i, 'available') for i in range(1, total + 1) if i not in existing]
            if data:
                cur.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT DO NOTHING", data)

        for aid in ADMIN_IDS:
            cur.execute("""INSERT INTO users (user_id, is_admin) VALUES (%s, TRUE)
                           ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE""", (aid,))

        for k, v in DEFAULT_SETTINGS.items():
            cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, v))

        cur.close(); conn.close()
        logger.info("✅ DB ready")
    except Exception as e:
        logger.error(f"❌ DB init: {e}")

init_db()
init_db_pool()

# ==================== RECEIPT VALIDATION ====================
def validate_receipt(receipt_data_url: str):
    """Returns (ok, mime, size, error_msg). Strict server-side validation."""
    if not receipt_data_url:
        return False, None, 0, "Receipt required"
    try:
        if "," not in receipt_data_url:
            return False, None, 0, "Invalid receipt format"
        header, encoded = receipt_data_url.split(",", 1)
        if not header.startswith("data:image/"):
            return False, None, 0, "Receipt must be an image"
        mime = header.split(";")[0].replace("data:", "")
        if mime not in RECEIPT_ALLOWED_MIMES:
            return False, None, 0, "Only JPEG/PNG/WebP allowed"
        # Base64 validate
        decoded = base64.b64decode(encoded, validate=True)
        size = len(decoded)
        if size > RECEIPT_MAX_BYTES:
            return False, None, 0, f"Receipt too large (max {RECEIPT_MAX_BYTES//1024//1024}MB)"
        if size < 100:
            return False, None, 0, "Receipt too small"
        # Magic byte check
        is_jpeg = decoded[:3] == b'\xff\xd8\xff'
        is_png = decoded[:8] == b'\x89PNG\r\n\x1a\n'
        is_webp = decoded[:4] == b'RIFF' and decoded[8:12] == b'WEBP'
        if not (is_jpeg or is_png or is_webp):
            return False, None, 0, "Invalid image file"
        return True, mime, size, None
    except Exception as e:
        return False, None, 0, f"Receipt decode error"

# ==================== CLEANUP ====================
def cleanup_expired_pendings():
    if not DATABASE_URL: return 0
    timeout_min = get_int_setting("reservation_minutes", RESERVATION_TIMEOUT_MINUTES)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_min)

    for attempt in range(2):
        conn = None
        try:
            if attempt == 0:
                conn = get_db_connection()
            else:
                db_url = DATABASE_URL
                if ("supabase.co" in db_url or "pooler.supabase.com" in db_url) and "sslmode" not in db_url:
                    db_url += ("&" if "?" in db_url else "?") + "sslmode=require"
                conn = psycopg2.connect(db_url)
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                UPDATE tickets
                SET status='available', user_id=NULL, user_name=NULL, user_phone=NULL,
                    referrer=NULL, receipt_file_id=NULL, price_paid=NULL, order_id=NULL,
                    reserved_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('reserved')
                  AND COALESCE(reserved_at, updated_at) < %s
                RETURNING number, order_id;
            """, (cutoff,))
            released = cur.fetchall()
            # Mark orders as expired
            order_ids = [r['order_id'] for r in released if r['order_id']]
            if order_ids:
                cur.execute("""UPDATE orders SET order_status='expired', updated_at=CURRENT_TIMESTAMP
                               WHERE order_id = ANY(%s) AND order_status='pending'""", (order_ids,))
            conn.commit(); cur.close()
            if attempt == 0:
                release_db_connection(conn); conn = None
            else:
                conn.close(); conn = None
            if released: logger.info(f"✅ Released {len(released)} tickets")
            return len(released)
        except psycopg2.OperationalError as e:
            if conn:
                try:
                    if attempt == 0: release_db_connection(conn)
                    else: conn.close()
                except: pass
            if "ssl" in str(e).lower() and attempt == 0:
                time.sleep(1); continue
            logger.error(f"Cleanup: {e}")
            return 0
        except Exception as e:
            logger.error(f"Cleanup: {e}")
            if conn:
                try:
                    if attempt == 0: release_db_connection(conn)
                    else: conn.close()
                except: pass
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
    try: tiers = json.loads(s.get("pricing_tiers") or "[]")
    except Exception: tiers = PRICING_TIERS
    draw_at = s.get("draw_end_at") or DRAW_END_AT or (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    return jsonify({
        "success": True, "draw_end_at": draw_at,
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
    return jsonify({"status": "healthy"}), 200

@app.route('/api/cron/cleanup', methods=['GET', 'POST'])
def cron_cleanup():
    if not CRON_SECRET:
        return jsonify({"success": False, "error": "cron disabled"}), 403
    secret = request.args.get('secret') or (request.get_json(silent=True) or {}).get('secret')
    if not secret or not hmac.compare_digest(secret, CRON_SECRET):
        return jsonify({"success": False, "error": "unauthorized"}), 401
    n = cleanup_expired_pendings()
    return jsonify({"success": True, "released": n})

# ==================== ADMIN LOGIN ====================
@app.route('/api/admin/verify-password', methods=['POST'])
@limiter.limit("10 per minute")
def verify_admin_password():
    if not ADMIN_PASSWORD and not ADMIN_PASSWORD_HASH:
        return jsonify({"success": False, "error": "Admin login disabled"}), 503
    data = request.get_json(silent=True) or {}
    pwd = data.get('password', '')
    if not pwd:
        return jsonify({"success": False, "error": "Password required"}), 400

    ok = False
    if ADMIN_PASSWORD_HASH:
        ok = verify_password(pwd, ADMIN_PASSWORD_HASH)
    elif ADMIN_PASSWORD:
        ok = hmac.compare_digest(pwd, ADMIN_PASSWORD)

    if ok:
        uid = data.get('user_id', 'admin')
        token = _issue_admin_session(uid, request.remote_addr or '')
        log_audit_action(uid, 'LOGIN', 'Password login')
        return jsonify({"success": True, "token": token, "expires_in": ADMIN_SESSION_TTL})
    return jsonify({"success": False, "error": "የተሳሳተ የይለፍ ቃል"}), 401

# ==================== TICKET APIs ====================
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
        res = jsonify(stats); res.headers['Cache-Control'] = 'public, max-age=15'
        return res
    except Exception as e:
        logger.error(f"Summary: {e}")
        return jsonify({"sold":0,"available":total,"pending":0,"reserved":0,
                        "total":total,"sold_pct":0,"unique_users":0,"total_revenue":0}), 200
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/tickets/list', methods=['GET'])
def tickets_list():
    conn = None
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(500, max(20, int(request.args.get('per_page', 100))))
        filter_status = request.args.get('filter', 'all')
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where, params = "", []
        if filter_status in ('available','sold','pending','reserved'):
            where = "WHERE status = %s"; params.append(filter_status)
        offset = (page - 1) * per_page
        cur.execute(f"SELECT number, status FROM tickets {where} ORDER BY number LIMIT %s OFFSET %s",
                    params + [per_page, offset])
        rows = cur.fetchall(); cur.close()
        tickets = {r['number']: {"status": r['status']} for r in rows}
        res = jsonify({"success": True, "page": page, "per_page": per_page,
                       "tickets": tickets, "has_more": len(rows) == per_page})
        res.headers['Cache-Control'] = 'public, max-age=10'
        return res
    except Exception as e:
        logger.error(f"List: {e}")
        return jsonify({"success": False, "tickets": {}}), 500
    finally:
        if conn: release_db_connection(conn)

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
        row = cur.fetchone(); cur.close()
        if not row: return jsonify({"success": False, "message": "አልተገኘም"}), 404
        return jsonify({"success": True, "ticket": {"number": row['number'], "status": row['status']}})
    except Exception:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/pricing', methods=['POST'])
def pricing_api():
    data = request.get_json(silent=True) or {}
    qty = int(data.get('quantity', 0))
    max_order = get_int_setting("max_per_order", MAX_TICKETS_PER_ORDER)
    if qty < 0 or qty > max_order:
        return jsonify({"success": False, "message": "የተሳሳተ ብዛት"}), 400
    try: tiers = json.loads(get_setting("pricing_tiers", json.dumps(PRICING_TIERS)) or "[]")
    except Exception: tiers = PRICING_TIERS
    base_price = get_int_setting("base_price", BASE_PRICE)
    if qty <= 0:
        return jsonify({"success": True, "pricing": {"total": 0, "unit": base_price, "discount": 0, "base_total": 0}})
    base_total = qty * base_price
    discount = 0
    for tier in tiers:
        if qty >= tier.get("min_qty", 999999):
            discount = tier.get("discount", 0); break
    total = max(base_total - discount, 0)
    return jsonify({"success": True, "pricing": {"total": total, "base_total": base_total,
                                                  "discount": discount, "unit": round(total / qty, 2)}})

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

        # ─── Business rule: user cannot hold duplicate active reservations ───
        cur.execute("""SELECT number FROM tickets WHERE user_id = %s AND status = 'reserved'
                       AND reserved_at > NOW() - INTERVAL '%s minutes'""", (user_id, timeout_min))
        existing = [r['number'] for r in cur.fetchall()]
        overlap = set(existing) & set(numbers)
        if overlap:
            conn.rollback()
            return jsonify({"success": False, "message": f"ቁጥሮች {list(overlap)} አስቀድመው ተይዘዋል"}), 409

        cur.execute("SELECT number, status FROM tickets WHERE number = ANY(%s) FOR UPDATE", (numbers,))
        rows = cur.fetchall()
        if len(rows) != len(set(numbers)) or any(r['status'] != 'available' for r in rows):
            conn.rollback()
            return jsonify({"success": False, "message": "አንዳንድ ቁጥሮች ተይዘዋል"}), 409

        cur.execute("""UPDATE tickets SET status='reserved', user_id=%s,
                       reserved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                       WHERE number = ANY(%s)""", (user_id, numbers))
        conn.commit(); cur.close()

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=timeout_min)).isoformat()
        return jsonify({"success": True,
                        "reserved_at": datetime.now(timezone.utc).isoformat(),
                        "expires_at": expires_at,
                        "minutes": timeout_min})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== SUBMIT ORDER (fixed flow: DB first, notify after) ====================
@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    data = request.get_json(silent=True) or {}
    selected = data.get('numbers', [])
    user_id = require_verified_user(request, data.get('user_id'))
    user_name = (data.get('user_name') or '').strip()
    user_phone = (data.get('user_phone') or '').strip()
    referrer = data.get('referrer', 'የለም')
    receipt_b64 = data.get('receipt_base64')

    if not selected or not user_id or not user_name or not user_phone or len(selected) != len(set(selected)):
        return jsonify({"success": False, "message": "እባክዎ ሁሉንም ይሙሉ"}), 400

    # ─── Server-side receipt validation ───
    ok, mime, size, err = validate_receipt(receipt_b64)
    if not ok:
        return jsonify({"success": False, "message": err}), 400

    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    invalid = [n for n in selected if not isinstance(n, int) or n < 1 or n > total]
    if invalid:
        return jsonify({"success": False, "message": "የተሳሳተ ቁጥር"}), 400

    base_price = get_int_setting("base_price", BASE_PRICE)
    try: tiers = json.loads(get_setting("pricing_tiers", json.dumps(PRICING_TIERS)) or "[]")
    except Exception: tiers = PRICING_TIERS
    base_total = len(selected) * base_price
    discount = 0
    for tier in tiers:
        if len(selected) >= tier.get("min_qty", 999999):
            discount = tier.get("discount", 0); break
    total_price = max(base_total - discount, 0)

    order_id = f"ORD-{int(time.time())}-{random.randint(100, 999)}"
    price_per = (Decimal(total_price) / Decimal(len(selected))).quantize(Decimal('0.01'))

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Lock tickets
        cur.execute("SELECT number, status, user_id FROM tickets WHERE number = ANY(%s) FOR UPDATE", (selected,))
        rows = cur.fetchall()
        if len(rows) != len(set(selected)):
            conn.rollback()
            return jsonify({"success": False, "code": "NOT_FOUND", "message": "ቁጥሮች አልተገኙም"}), 400

        # ─── Clear errors for expired/foreign reservations ───
        not_owned = [r['number'] for r in rows if str(r['user_id']) != user_id]
        if not_owned:
            conn.rollback()
            return jsonify({"success": False, "code": "NOT_YOURS",
                            "message": f"ቁጥሮች {not_owned} የእርስዎ አይደሉም"}), 409

        not_reserved = [r['number'] for r in rows if r['status'] != 'reserved']
        if not_reserved:
            conn.rollback()
            return jsonify({"success": False, "code": "RESERVATION_EXPIRED",
                            "message": f"የተያዙት ጊዜ አልቋል። እንደገና ይምረጡ።"}), 409

        # ─── STEP 1: Insert order (main entity) ───
        cur.execute("""
            INSERT INTO orders (order_id, user_id, user_name, user_phone, referrer,
                                ticket_count, total_price, receipt_mime, receipt_size,
                                order_status, payment_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'unpaid')
        """, (order_id, user_id, user_name, user_phone, referrer,
              len(selected), total_price, mime, size))

        # ─── STEP 2: Update tickets (link to order) ───
        cur.execute("""
            UPDATE tickets SET status='pending', user_id=%s, user_name=%s,
                user_phone=%s, referrer=%s, price_paid=%s, order_id=%s,
                updated_at=CURRENT_TIMESTAMP
            WHERE number = ANY(%s)
        """, (user_id, user_name, user_phone, referrer, price_per, order_id, selected))

        conn.commit()
        cur.close()

        # ─── STEP 3: Notify admin (AFTER commit — failure doesn't break order) ───
        file_id, notified = send_admin_notification(
            user_name, user_phone, selected, total_price,
            referrer, receipt_b64, user_id, order_id
        )

        # ─── STEP 4: Update receipt_file_id if notification succeeded ───
        if notified and file_id:
            try:
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute("UPDATE orders SET receipt_file_id=%s WHERE order_id=%s", (file_id, order_id))
                cur2.execute("UPDATE tickets SET receipt_file_id=%s WHERE order_id=%s", (file_id, order_id))
                conn2.commit(); cur2.close()
                release_db_connection(conn2)
            except Exception as e:
                logger.error(f"Post-notify update: {e}")

        return jsonify({"success": True, "message": "ትዕዛዝዎ ተልኳል",
                        "order_id": order_id, "total_price": total_price})
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"submit_order: {e}")
        return jsonify({"success": False, "message": "Internal error" if IS_PRODUCTION else str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== USER APIs ====================
@app.route('/api/my-tickets', methods=['GET'])
def get_my_tickets():
    user_id = require_verified_user(request, request.args.get('user_id'))
    if not user_id: return jsonify([]), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""SELECT number, status, price_paid, updated_at, order_id, reserved_at
                       FROM tickets WHERE user_id = %s AND status IN ('pending','sold','reserved')
                       ORDER BY updated_at DESC""", (str(user_id),))
        rows = cur.fetchall(); cur.close()
        timeout_min = get_int_setting("reservation_minutes", RESERVATION_TIMEOUT_MINUTES)
        now = datetime.now(timezone.utc)
        for r in rows:
            if r['status'] == 'reserved' and r['reserved_at']:
                r_at = r['reserved_at']
                if r_at.tzinfo is None: r_at = r_at.replace(tzinfo=timezone.utc)
                elapsed = (now - r_at).total_seconds()
                r['expires_in_seconds'] = int(max(0, timeout_min * 60 - elapsed))
            else:
                r['expires_in_seconds'] = None
            r['updated_at'] = r['updated_at'].isoformat() if r['updated_at'] else None
            r['reserved_at'] = r['reserved_at'].isoformat() if r['reserved_at'] else None
            r['price_paid'] = float(r['price_paid']) if r['price_paid'] else None
        return jsonify(rows)
    except Exception as e:
        logger.error(f"my-tickets: {e}")
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/get-user-info', methods=['GET'])
def get_user_info():
    user_id = require_verified_user(request, request.args.get('user_id'))
    if not user_id: return jsonify({"success": False}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT first_name, phone_number, is_admin FROM users WHERE user_id = %s", (str(user_id),))
        user = cur.fetchone(); cur.close()
        if user:
            return jsonify({"success": True, "name": user['first_name'],
                            "phone": user['phone_number'],
                            "is_admin": user.get('is_admin', False) or str(user_id) in ADMIN_IDS})
        return jsonify({"success": True, "name": None, "phone": None,
                        "is_admin": str(user_id) in ADMIN_IDS})
    except Exception:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/referral-stats', methods=['GET'])
def get_referral_stats():
    user_id = require_verified_user(request, request.args.get('user_id'))
    if not user_id: return jsonify({"success": False}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""SELECT COUNT(DISTINCT user_id) AS total_referred,
                              COUNT(*) FILTER (WHERE status='sold') AS successful_purchases
                       FROM tickets WHERE referrer = %s""", (str(user_id),))
        stats = cur.fetchone(); cur.close()
        return jsonify({"success": True, "stats": stats})
    except Exception:
        return jsonify({"success": False}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/winners', methods=['GET'])
def get_winners():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT name, ticket_number, round, photo FROM winners ORDER BY created_at DESC LIMIT 50;")
        winners = cur.fetchall(); cur.close()
        return jsonify(winners)
    except Exception:
        return jsonify([]), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== PHONE REQUEST ====================
def _send_phone_request(chat_id):
    if not chat_id: return None
    try:
        phone_text = (
            f"📱 *ስልክ ቁጥር ያጋሩ*\n\n"
            f"ለመቀጠል ስልክ ቁጥርዎን ማጋራት ያስፈልጋል።\n\n"
            f"👇 *ከታች ያለውን ቁልፍ ይጫኑ*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔒 ስልክ ቁጥርዎ ለደህንነት ብቻ ያገለግላል።\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        return requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
            "chat_id": chat_id, "text": phone_text, "parse_mode": "Markdown",
            "reply_markup": {"keyboard": [[{"text": "📱 ስልክ ቁጥር አጋራ", "request_contact": True}]],
                             "resize_keyboard": True, "one_time_keyboard": True}
        }, timeout=10)
    except Exception as e:
        logger.error(f"_send_phone_request: {e}")
        return None

# ==================== BOT WEBHOOK ====================
@app.route('/bot/webhook', methods=['POST'])
def bot_webhook_handler():
    if not BOT_TOKEN: return jsonify({"status": "error"}), 500
    data = request.get_json(silent=True) or {}

    message = data.get('message')
    if message:
        chat_id = message.get('chat', {}).get('id')
        text = (message.get('text') or '').strip()
        contact = message.get('contact')
        first_name = message.get('from', {}).get('first_name', 'Friend')
        username = message.get('from', {}).get('username', '')

        if text == '/start':
            welcome_text = (
                f"👋 ሰላም *{esc_md(first_name)}*!\n\n"
                f"🚗 *Getachew Fikadu Jirata* የቲኬት ዕድል መተግበሪያ እንኳን ደህና መጡ!\n\n"
                f"🎟️ የ BYD Sealion 6 መኪና ዕድል ለመግዛት ይህ ቦት ያገለግልዎታል።\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🌍 *እባክዎ ቋንቋ ይምረጡ*\n"
                f"🌍 *Maaloo Afaan filadhaa*\n"
                f"🌍 *Please select your language*\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            keyboard = {"keyboard": [
                [{"text": "🇪🇹 አማርኛ"}],
                [{"text": "🇪🇹 Afaan Oromoo"}],
                [{"text": "🇬🇧 English"}]
            ], "resize_keyboard": True, "one_time_keyboard": True}
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": welcome_text,
                                "parse_mode": "Markdown", "reply_markup": keyboard}, timeout=10)

        elif any(lang in text for lang in ["አማርኛ", "Oromoo", "English"]):
            _send_phone_request(chat_id)

        if contact:
            phone = contact.get('phone_number')
            uid = str(message.get('from', {}).get('id'))
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""INSERT INTO users (user_id, first_name, username, phone_number)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT (user_id) DO UPDATE SET
                                   phone_number = EXCLUDED.phone_number,
                                   first_name = EXCLUDED.first_name,
                                   username = EXCLUDED.username""",
                            (uid, first_name, username, phone))
                conn.commit(); cur.close()

                success_text = (
                    f"✅ *ስልክ ቁጥርዎ ተቀብለናል!*\n\n"
                    f"📱 ስልክ: `{esc_md(phone)}`\n"
                    f"👤 ስም: *{esc_md(first_name)}*\n\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🎟️ *የቲኬት መተግበሪያውን ለመክፈት* ከታች ያለውን ቁልፍ ይጫኑ\n\n"
                    f"🎁 *የቲኬት ዋጋ:* 3,500 ብር\n"
                    f"🏆 *ሽልማት:* BYD Sealion 6\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                mini_app_keyboard = {"inline_keyboard": [[{
                    "text": "🎟️ የቲኬት መተግበሪያን ክፈት",
                    "web_app": {"url": WEB_APP_URL}
                }]]}
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json={"chat_id": chat_id, "text": success_text,
                                    "parse_mode": "Markdown", "reply_markup": mini_app_keyboard}, timeout=10)
            except Exception as e:
                logger.error(f"Contact save: {e}")
            finally:
                if conn: release_db_connection(conn)
        return jsonify({"status": "ok"}), 200

    cb = data.get('callback_query')
    if cb:
        cb_id = cb.get('id')
        from_id = str(cb.get('from', {}).get('id', ''))
        raw = cb.get('data', '')

        if raw.startswith('lang_'):
            lang_names = {'am': 'አማርኛ', 'om': 'Afaan Oromoo', 'en': 'English'}
            lang_name = lang_names.get(raw.replace('lang_', ''), 'አማርኛ')
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                          json={"callback_query_id": cb_id, "text": f"✅ {lang_name}"}, timeout=5)
            msg = cb.get('message') or {}
            chat_id = msg.get('chat', {}).get('id') or cb.get('from', {}).get('id')
            if chat_id: _send_phone_request(chat_id)
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
                    cur.execute("""UPDATE orders SET order_status='approved', payment_status='paid',
                                   updated_at=CURRENT_TIMESTAMP WHERE order_id=%s""", (order_id,))
                    msg = f"✅ #{order_id} ፀድቋል"
                    push = f"🎉 *ትዕዛዝዎ ፀድቋል!*\n\n🆔 `{esc_md(order_id)}`\n🎟️ `{esc_md(','.join(map(str, nums)))}`\n\n🎊 መልካም ዕድል!"
                else:
                    cur.execute("""UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
                        user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
                        order_id=NULL, reserved_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE order_id=%s""", (order_id,))
                    cur.execute("""UPDATE orders SET order_status='rejected', updated_at=CURRENT_TIMESTAMP
                                   WHERE order_id=%s""", (order_id,))
                    msg = f"❌ #{order_id} ተሰርዟል"
                    push = f"❌ *ትዕዛዝዎ ተሰርዟል*\n\n🆔 `{esc_md(order_id)}`"
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

    return jsonify({"status": "ok"}), 200

# ==================== ADMIN APIs ====================
@app.route('/api/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if request.method == 'GET':
        s = get_all_settings()
        try: s['pricing_tiers'] = json.loads(s.get('pricing_tiers') or "[]")
        except Exception: s['pricing_tiers'] = PRICING_TIERS
        for k in ('base_price', 'total_tickets', 'max_per_order', 'reservation_minutes'):
            try: s[k] = int(s[k])
            except Exception: pass
        return jsonify({"success": True, "settings": s})

    data = request.get_json(silent=True) or {}
    allowed = ["product_name", "subtitle", "image_url", "draw_end_at", "draw_title",
               "base_price", "total_tickets", "max_per_order", "reservation_minutes",
               "pricing_tiers", "telebirr_number", "telebirr_name", "cbe_number", "cbe_name"]
    saved = []
    for key in allowed:
        if key in data:
            val = data[key]
            if key == "pricing_tiers" and isinstance(val, (list, dict)):
                val = json.dumps(val)
            if set_setting(key, val): saved.append(key)

    if "total_tickets" in data:
        try:
            new_total = int(data["total_tickets"])
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(number), 0) AS m FROM tickets")
            existing_max = cur.fetchone()[0] or 0
            if new_total > existing_max:
                data_insert = [(i, 'available') for i in range(existing_max + 1, new_total + 1)]
                cur.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT DO NOTHING", data_insert)
                conn.commit()
            cur.close(); release_db_connection(conn)
        except Exception as e:
            logger.error(f"Ticket expansion: {e}")

    log_audit_action('Admin', "UPDATE_SETTINGS", f"Keys: {saved}")
    return jsonify({"success": True, "saved": saved})

@app.route('/api/admin/orders', methods=['GET'])
def admin_orders_list():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    status_filter = request.args.get('status', 'pending')
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT o.order_id, o.user_name, o.user_phone, o.total_price, o.ticket_count,
                   o.order_status, o.payment_status, o.created_at,
                   ARRAY(SELECT number FROM tickets WHERE order_id = o.order_id ORDER BY number) AS numbers
            FROM orders o WHERE o.order_status = %s
            ORDER BY o.created_at DESC LIMIT 200
        """, (status_filter,))
        rows = cur.fetchall(); cur.close()
        out = [{
            "order_id": r['order_id'], "user_name": r['user_name'],
            "user_phone": r['user_phone'], "total_price": float(r['total_price'] or 0),
            "numbers": r['numbers'] or [],
            "created_at": r['created_at'].isoformat() if r['created_at'] else None,
            "status": r['order_status'], "payment_status": r['payment_status']
        } for r in rows]
        return jsonify({"success": True, "orders": out})
    except Exception as e:
        logger.error(f"admin orders: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/approve-order', methods=['POST'])
def admin_approve_order():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    order_id = data.get('order_id')
    if not order_id: return jsonify({"success": False, "error": "order_id required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id, status FROM tickets WHERE order_id = %s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets: return jsonify({"success": False, "error": "Order not found"}), 404
        if any(t['status'] != 'pending' for t in tickets):
            return jsonify({"success": False, "error": "Already processed"}), 409
        nums = [t['number'] for t in tickets]
        target = tickets[0]['user_id']
        cur.execute("UPDATE tickets SET status='sold', updated_at=CURRENT_TIMESTAMP WHERE order_id=%s", (order_id,))
        cur.execute("""UPDATE orders SET order_status='approved', payment_status='paid',
                       updated_at=CURRENT_TIMESTAMP WHERE order_id=%s""", (order_id,))
        conn.commit(); cur.close()
        send_telegram_push(target, f"🎉 ትዕዛዝዎ #{esc_md(order_id)} ፀድቋል!")
        log_audit_action('Admin', "APPROVE_ORDER", order_id)
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/reject-order', methods=['POST'])
def admin_reject_order():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    order_id = data.get('order_id')
    if not order_id: return jsonify({"success": False, "error": "order_id required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT number, user_id FROM tickets WHERE order_id = %s FOR UPDATE", (order_id,))
        tickets = cur.fetchall()
        if not tickets: return jsonify({"success": False, "error": "Order not found"}), 404
        target = tickets[0]['user_id']
        cur.execute("""UPDATE tickets SET status='available', user_id=NULL, user_name=NULL,
            user_phone=NULL, referrer=NULL, receipt_file_id=NULL, price_paid=NULL,
            order_id=NULL, reserved_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE order_id=%s""", (order_id,))
        cur.execute("""UPDATE orders SET order_status='rejected', updated_at=CURRENT_TIMESTAMP
                       WHERE order_id=%s""", (order_id,))
        conn.commit(); cur.close()
        send_telegram_push(target, f"❌ ትዕዛዝዎ #{esc_md(order_id)} ተሰርዟል")
        log_audit_action('Admin', "REJECT_ORDER", order_id)
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/broadcast', methods=['POST'])
def broadcast_message():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    msg = data.get('message')
    if not msg: return jsonify({"error": "Message required"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT user_id FROM tickets WHERE user_id IS NOT NULL")
        uids = [r['user_id'] for r in cur.fetchall() if r['user_id']]
        cur.close()
        for uid in uids:
            send_telegram_push(uid, f"📢 **ማሳወቂያ**\n\n{msg}")
        log_audit_action('Admin', "BROADCAST", f"Sent to {len(uids)} users")
        return jsonify({"success": True, "message": f"ለ {len(uids)} ተጠቃሚዎች ተልኳል"})
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
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""SELECT order_id, user_name, user_phone, ticket_count, total_price,
                              order_status, payment_status, created_at FROM orders
                       ORDER BY created_at DESC""")
        orders = cur.fetchall(); cur.close()
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(['Order_ID','Name','Phone','Tickets','Total','Status','Payment','Date'])
        for o in orders:
            w.writerow([o['order_id'], o['user_name'], o['user_phone'],
                        o['ticket_count'], o['total_price'], o['order_status'],
                        o['payment_status'], o['created_at']])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-disposition": "attachment; filename=orders.csv"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/update-ticket-status', methods=['POST'])
def update_ticket_status():
    if not is_authorized_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    nums = data.get('ticket_numbers', [])
    new_status = data.get('status')
    total = get_int_setting("total_tickets", TOTAL_TICKETS)
    if not nums or new_status not in ['sold','available','reserved','pending']:
        return jsonify({"error": "Invalid"}), 400
    nums = [n for n in nums if isinstance(n, int) and 1 <= n <= total]
    if not nums: return jsonify({"error": "No valid tickets"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT user_id FROM tickets WHERE number = ANY(%s)", (nums,))
        uids = [r['user_id'] for r in cur.fetchall() if r['user_id']]
        cur.execute("UPDATE tickets SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE number = ANY(%s)",
                    (new_status, nums))
        conn.commit(); cur.close()
        log_audit_action('Admin', "UPDATE_STATUS", f"{nums} → {new_status}")
        if new_status == 'sold':
            for uid in uids:
                send_telegram_push(uid, f"🎉 ትዕዛዝዎ ፀድቋል! `{esc_md(','.join(map(str, nums)))}`")
        return jsonify({"success": True, "message": f"{len(nums)} ቲኬቶች ተሻሻሉ"})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

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
        cur.execute("INSERT INTO winners (name, ticket_number, round, photo) VALUES (%s,%s,%s,%s)",
                    (name, tn, data.get('round', 'Round 1'), data.get('photo', '')))
        conn.commit(); cur.close()
        log_audit_action('Admin', "ADD_WINNER", f"{name} #{tn}")
        return jsonify({"success": True})
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

@app.route('/api/admin/audit-logs', methods=['GET'])
def admin_audit_logs():
    if not is_authorized_admin(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, admin_id, action, details, created_at FROM audit_logs ORDER BY created_at DESC LIMIT 100")
        rows = cur.fetchall(); cur.close()
        out = [{"id": r['id'], "admin_id": r['admin_id'], "action": r['action'],
                "details": r['details'],
                "created_at": r['created_at'].isoformat() if r['created_at'] else None} for r in rows]
        return jsonify({"success": True, "logs": out})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn: release_db_connection(conn)

# ==================== ERROR HANDLER ====================
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled: {e}", exc_info=True)
    if IS_PRODUCTION:
        return jsonify({"success": False, "error": "Internal server error"}), 500
    return jsonify({"success": False, "error": str(e)}), 500

# ==================== HELPERS ====================
@app.route('/api/admin/hash-password', methods=['POST'])
def generate_password_hash():
    """One-time helper: POST {password, setup_key} → returns hash. Remove after use."""
    data = request.get_json(silent=True) or {}
    if not CRON_SECRET or data.get('setup_key') != CRON_SECRET:
        return jsonify({"success": False}), 403
    pwd = data.get('password')
    if not pwd or len(pwd) < 8:
        return jsonify({"success": False, "error": "Password too short"}), 400
    return jsonify({"success": True, "hash": hash_password(pwd)})

# ==================== RUN ====================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
