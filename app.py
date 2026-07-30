import os
import json
import base64
import hmac
import hashlib
import logging
import time
from io import BytesIO
from urllib.parse import parse_qsl
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# PDF and QR Code Libraries
import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIGURATION & ENVIRONMENT CHECK ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
ALLOWED_ORIGIN = os.environ.get("WEB_APP_URL", "*")

app = Flask(__name__)

# CORS Security
CORS(app, resources={r"/api/*": {"origins": [ALLOWED_ORIGIN, "https://telegram.org", "*"]}})

# Rate Limiting
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
        
        if count < 2200:
            tickets_data = [(i, 'available') for i in range(1, 2201)]
            cursor.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s) ON CONFLICT (number) DO NOTHING", tickets_data)
            conn.commit()
            logging.info("✅ 2200 ቁጥሮች በዳታቤዝ ውስጥ በስኬት ተፈጠሩ!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"❌ የዳታቤዝ ማስጀመሪያ ስህተት: {e}")

init_db()

# --- SECURITY: HMAC Validation ---
def verify_telegram_init_data(init_data: str) -> bool:
    if not BOT_TOKEN or not init_data:
        return True
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return False
        
        hash_to_check = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(calculated_hash, hash_to_check)
    except Exception as e:
        logging.error(f"InitData verification error: {e}")
        return False

# --- PDF GENERATOR FUNCTION ---
def generate_pdf_ticket(user_name, numbers, user_id):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    
    # Title & Header
    p.setFillColor(colors.HexColor("#1c1e21"))
    p.rect(0, 680, 612, 120, fill=True, stroke=False)
    
    p.setFillColor(colors.HexColor("#f1c40f"))
    p.setFont("Helvetica-Bold", 22)
    p.drawString(40, 740, "GETACHEW FIKADU CAR EKUB")
    
    p.setFillColor(colors.white)
    p.setFont("Helvetica", 12)
    p.drawString(40, 715, "OFFICIAL CAR RAFFLE TICKET (BYD YUAN UP)")
    
    # Ticket Details
    p.setFillColor(colors.black)
    p.setFont("Helvetica-Bold", 14)
    p.drawString(40, 640, f"Customer Name: {user_name}")
    p.drawString(40, 615, f"Telegram ID: {user_id}")
    p.drawString(40, 590, f"Ticket Numbers: {', '.join(map(str, numbers))}")
    p.drawString(40, 565, f"Total Paid: {len(numbers) * 3000:,} Birr")
    p.drawString(40, 540, f"Status: CONFIRMED & APPROVED")

    # Generate QR Code for Verification
    qr_data = f"CarEkub|User:{user_id}|Tickets:{'-'.join(map(str, numbers))}"
    qr = qrcode.make(qr_data)
    qr_buffer = BytesIO()
    qr.save(qr_buffer, format='PNG')
    qr_buffer.seek(0)
    
    # Draw QR code on PDF
    p.drawInlineImage(qr_buffer, 400, 520, width=150, height=150)

    # Footer
    p.setStrokeColor(colors.gray)
    p.line(40, 480, 570, 480)
    p.setFont("Helvetica-Oblique", 10)
    p.setFillColor(colors.gray)
    p.drawString(40, 460, "Thank you for participating! Please keep this ticket for the live winner draw.")

    p.showPage()
    p.save()
    buffer.seek(0)
    return buffer

def send_pdf_document(chat_id, pdf_buffer, filename="Car_Ekub_Ticket.pdf"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {'document': (filename, pdf_buffer, 'application/pdf')}
    payload = {'chat_id': chat_id, 'caption': "🎉 **እንኳን ደስ አለዎት!**\nየተገዙት ቲኬቶች ኦፊሴላዊ PDF ደረሰኝ ከታች ተያይዟል።"}
    requests.post(url, data=payload, files=files)

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
        return jsonify({}), 200

@app.route('/api/stream-tickets')
def stream_tickets():
    def event_stream():
        while True:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT number, status FROM tickets")
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                
                tickets_status = {row['number']: row['status'] for row in rows}
                yield f"data: {json.dumps(tickets_status)}\n\n"
            except Exception as e:
                logging.error(f"SSE stream error: {e}")
            time.sleep(3)

    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/api/submit-order', methods=['POST'])
@limiter.limit("5 per minute")
def submit_order():
    data = request.json or {}
    init_data = data.get('initData', '')

    if init_data and not verify_telegram_init_data(init_data):
        return jsonify({"success": False, "message": "የቴሌግራም ጥያቄ ማረጋገጫ አልተሳካም!"}), 403

    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_b64 = data.get('receipt_url')

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
                    cursor.execute(f"SELECT user_name FROM tickets WHERE number = %s", (numbers[0],))
                    u_row = cursor.fetchone()
                    user_name = u_row['user_name'] if u_row else "Customer"

                    cursor.execute(f"UPDATE tickets SET status = 'sold' WHERE number IN ({placeholders})", numbers)
                    conn.commit()
                    
                    # Generate and send PDF Ticket
                    pdf_buf = generate_pdf_ticket(user_name, numbers, user_id)
                    send_pdf_document(user_id, pdf_buf)
                    
                    admin_status_text = f"\n\n✅ **Approved & PDF Ticket Sent**"

                elif action == "reject":
                    cursor.execute(f"UPDATE tickets SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL WHERE number IN ({placeholders})", numbers)
                    conn.commit()
                    
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                        "chat_id": user_id,
                        "text": f"⚠️ **ማሳወቂያ፡**\n\nየላኩት የክፍያ ደረሰኝ ውድቅ ስለተደረገ የተያዙት ቁጥሮች ({numbers}) ተመልሰው ነፃ ሆነዋል።",
                        "parse_mode": "Markdown"
                    })
                    admin_status_text = f"\n\n❌ **Rejected by Admin**"

                cursor.close()
                conn.close()

                current_caption = message.get("caption") or message.get("text") or ""
                updated_text = current_caption + admin_status_text

                if "caption" in message:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption", json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "caption": updated_text,
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": []}
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
