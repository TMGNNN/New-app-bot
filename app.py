import os
import json
import base64
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from flask_cors import CORS

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://tangerine-entremet-b361e6.netlify.app/")[cite: 2]
DATABASE_URL = os.environ.get("DATABASE_URL")

app = Flask(__name__)
CORS(app)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL አልተገኘም! እባክዎን Render Environment Variables ላይ ያረጋግጡ።")
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
        result = cursor.fetchone()
        count = result['count'] if result else 0
        
        if count == 0:
            tickets_data = [(i, 'available') for i in range(1, 2201)]
            cursor.executemany("INSERT INTO tickets (number, status) VALUES (%s, %s)", tickets_data)
            conn.commit()
            print("✅ 2200 ቁጥሮች በዳታቤዝ ውስጥ በስኬት ተፈጠሩ!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ የዳታቤዝ ስህተት: {e}")

init_db()

def send_admin_notification_via_api(numbers, user_name, user_phone, referrer, user_id, receipt_b64):
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
            print(f"Photo send failed via API: {e}")

    payload = {
        'chat_id': ADMIN_CHAT_ID,
        'text': msg_text,
        'parse_mode': 'Markdown',
        'reply_markup': json.dumps(reply_markup)
    }
    requests.post(url_send_msg, json=payload)

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
        return jsonify({"error": str(e)}), 500

# ለተጠቃሚው የያዛቸውን ትኬቶች የሚያመጣ አዲስ API Endpoint
@app.route('/api/my-tickets/<user_id>', methods=['GET'])
def get_my_tickets(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT number, status, referrer FROM tickets WHERE user_id = %s", (str(user_id),))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/submit-order', methods=['POST'])
def submit_order():
    data = request.json or {}
    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_b64 = data.get('receipt_url')

    if not selected_numbers:
        return jsonify({"success": False, "message": "ምንም ቁጥር አልተመረጠም!"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        placeholders = ','.join(['%s'] * len(selected_numbers))
        
        # Atomic Update - ነፃ የሆኑትን ብቻ ወደ pending ይቀይራል
        cursor.execute(f'''
            UPDATE tickets 
            SET status = 'pending', user_id = %s, user_name = %s, user_phone = %s, referrer = %s
            WHERE number IN ({placeholders}) AND status = 'available'
            RETURNING number;
        ''', [user_id, user_name, user_phone, referrer] + selected_numbers)

        updated_rows = cursor.fetchall()
        conn.commit()

        # የትኞቹ ቁጥሮች በስኬት እንደተያዙ ማረጋገጥ
        if len(updated_rows) != len(selected_numbers):
            cursor.close()
            conn.close()
            return jsonify({
                "success": False, 
                "message": "ከተመረጡት ቁጥሮች መካከል የተወሰኑት አሁን ባለው ሰዓት በሌላ ሰው ተይዘዋል! እባክዎን ገፁን Refresh አድርገው በድጋሚ ይሞክሩ።"
            }), 400

        cursor.close()
        conn.close()

        send_admin_notification_via_api(selected_numbers, user_name, user_phone, referrer, user_id, receipt_b64)

        return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል! በአድሚን በማረጋገጥ ላይ ይገኛል።"})
    except Exception as e:
        return jsonify({"success": False, "message": f"የሰርቨር ስህተት: {str(e)}"}), 500

@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json() or {}
    if "callback_query" in update:
        query = update["callback_query"]
        callback_id = query.get("id")
        callback_data = query.get("data", "").split('_')
        
        if len(callback_data) >= 3:
            action = callback_data[0]
            user_id = callback_data[1]
            numbers = list(map(int, callback_data[2].split('-')))
            
            conn = get_db_connection()
            cursor = conn.cursor()
            placeholders = ','.join(['%s'] * len(numbers))

            if action == "approve":
                cursor.execute(f"UPDATE tickets SET status = 'sold' WHERE number IN ({placeholders})", numbers)
                conn.commit()
                msg = f"🎉 **እንኳን ደስ አለዎት!**\n\nየቆረጧቸው ቲኬቶች (ቁጥር፡ {numbers}) በስኬት ጸድቀዋል። መልካም ዕድል!"
                ans_text = "በስኬት ጸድቋል!"
            elif action == "reject":
                cursor.execute(f"UPDATE tickets SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL WHERE number IN ({placeholders})", numbers)
                conn.commit()
                msg = f"⚠️ **ማሳወቂያ፡**\n\nየላኩት የክፍያ ደረሰኝ ውድቅ ስለተደረገ የተያዙት ቁጥሮች ({numbers}) ተመልሰው ነፃ ሆነዋል።"
                ans_text = "ውድቅ ተደርጓል!"

            cursor.close()
            conn.close()

            # 1. ለቴሌግራም Callback አጭር ምላሽ መስጠት (Loading Spinner እንዲጠፋ)
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={
                "callback_query_id": callback_id,
                "text": ans_text
            })

            # 2. ለተጠቃሚው ማሳወቂያ መላክ
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": user_id,
                "text": msg,
                "parse_mode": "Markdown"
            })

    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
