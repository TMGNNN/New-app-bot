import os
import sqlite3
import json
import logging
import io
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# --- CONFIGURATION ---
BOT_TOKEN = "7969026648:AAEmYfxun6f_UtXxg2tVETcu_gAn0Bi010g"
ADMIN_CHAT_ID = "8982566651"
WEB_APP_URL = "https://tangerine-entremet-b361e6.netlify.app/"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- FLASK APP SETUP ---
app = Flask(__name__)
CORS(app)

# --- DATABASE SETUP ---
DB_NAME = "ekub_lottery.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            number INTEGER PRIMARY KEY,
            status TEXT DEFAULT 'available',
            user_id TEXT,
            user_name TEXT,
            user_phone TEXT,
            referrer TEXT,
            receipt_file_id TEXT
        )
    ''')
    
    cursor.execute("SELECT COUNT(*) FROM tickets")
    if cursor.fetchone()[0] == 0:
        tickets_data = [(i, 'available') for i in range(1, 2201)]
        cursor.executemany("INSERT INTO tickets (number, status) VALUES (?, ?)", tickets_data)
        
    conn.commit()
    conn.close()

init_db()

# --- API ENDPOINTS ---

@app.route('/', methods=['GET'])
def home():
    return "Server is Running Successfully!"

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT number, status FROM tickets")
    rows = cursor.fetchall()
    conn.close()
    
    tickets_status = {row['number']: row['status'] for row in rows}
    return jsonify(tickets_status)

@app.route('/api/submit-order', methods=['POST'])
def submit_order():
    data = request.json
    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_b64 = data.get('receipt_url')

    if not selected_numbers:
        return jsonify({"success": False, "message": "ምንም ቁጥር አልተመረጠም!"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    placeholders = ','.join(['?'] * len(selected_numbers))
    cursor.execute(f"SELECT number FROM tickets WHERE number IN ({placeholders}) AND status != 'available'", selected_numbers)
    taken = cursor.fetchall()

    if taken:
        conn.close()
        taken_nums = [t['number'] for t in taken]
        return jsonify({"success": False, "message": f"እነዚህ ቁጥሮች ቀደም ብለው ተይዘዋል፡ {taken_nums}"}), 400

    cursor.execute(f'''
        UPDATE tickets 
        SET status = 'pending', user_id = ?, user_name = ?, user_phone = ?, referrer = ?
        WHERE number IN ({placeholders})
    ''', [user_id, user_name, user_phone, referrer] + selected_numbers)

    conn.commit()
    conn.close()

    send_admin_verification(selected_numbers, user_name, user_phone, referrer, user_id, receipt_b64)

    return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል!"})

def send_admin_verification(numbers, user_name, user_phone, referrer, user_id, receipt_b64):
    nums_str = ", ".join(map(str, numbers))
    total_price = len(numbers) * 3000
    
    msg_text = (
        f"🚨 *አዲስ የቲኬት ደረሰኝ ደርሷል!*\n\n"
        f"👤 *ደንበኛ፡* {user_name}\n"
        f"📞 *ID፡* `{user_id}`\n"
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
            requests.post(f"{TELEGRAM_API}/sendPhoto", data=payload, files=files)
            return
        except Exception as e:
            print(f"Photo send error: {e}")

    payload = {
        'chat_id': ADMIN_CHAT_ID,
        'text': msg_text,
        'parse_mode': 'Markdown',
        'reply_markup': json.dumps(reply_markup)
    }
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)

# --- TELEGRAM WEBHOOK ROUTE ---
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        return 'OK', 200

    # /start Command
    if 'message' in update and 'text' in update['message']:
        chat_id = update['message']['chat']['id']
        text = update['message']['text']
        
        if text.startswith('/start'):
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "🚗 መኪና እቁብ/ሎተሪ ቁረጥ", "web_app": {"url": WEB_APP_URL}}
                ]]
            }
            payload = {
                'chat_id': chat_id,
                'text': "እንኳን ወደ Getachew Fikadu Car Ekub በደህና መጡ! ቁጥር ለመቁረጥ ከታች ያለውን ቁልፍ ይጫኑ፡",
                'reply_markup': json.dumps(reply_markup)
            }
            requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)

    # Admin Inline Button Clicks (Approve/Reject)
    if 'callback_query' in update:
        cb = update['callback_query']
        cb_id = cb['id']
        data = cb['data'].split('_')
        action = data[0]
        user_id = data[1]
        numbers = list(map(int, data[2].split('-')))
        placeholders = ','.join(['?'] * len(numbers))

        conn = get_db_connection()
        cursor = conn.cursor()

        if action == "approve":
            cursor.execute(f"UPDATE tickets SET status = 'sold' WHERE number IN ({placeholders})", numbers)
            conn.commit()
            msg = f"✅ ቲኬት ቁጥር {numbers} በስኬት ጸድቋል!"
            user_msg = f"🎉 እንኳን ደስ አለዎት! የቆረጧቸው ቲኬቶች (ቁጥር፡ {numbers}) በስኬት ጸድቀዋል።"
        else:
            cursor.execute(f"UPDATE tickets SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL WHERE number IN ({placeholders})", numbers)
            conn.commit()
            msg = f"❌ ቲኬት ቁጥር {numbers} ውድቅ ተደርጓል!"
            user_msg = f"⚠️ የላኩት የክፍያ ደረሰኝ ውድቅ ስለተደረገ የተያዙት ቁጥሮች ({numbers}) ነፃ ሆነዋል።"

        conn.close()

        # Answer Callback & Send Notification
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={'callback_query_id': cb_id, 'text': msg})
        requests.post(f"{TELEGRAM_API}/sendMessage", json={'chat_id': ADMIN_CHAT_ID, 'text': msg})
        requests.post(f"{TELEGRAM_API}/sendMessage", json={'chat_id': user_id, 'text': user_msg})

    return 'OK', 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
