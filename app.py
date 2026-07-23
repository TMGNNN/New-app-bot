import os
import sqlite3
import json
import logging
import io
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
BOT_TOKEN = "7969026648:AAEmYfxun6f_UtXxg2tVETcu_gAn0Bi010g" [cite: 1]
ADMIN_CHAT_ID = "8982566651"   [cite: 1]
WEB_APP_URL = "https://tangerine-entremet-b361e6.netlify.app/"[cite: 1]

# --- FLASK APP SETUP ---
app = Flask(__name__)[cite: 1]
CORS(app)[cite: 1]

# --- DATABASE SETUP ---
DB_NAME = "ekub_lottery.db"[cite: 1]

def get_db_connection():
    # Multi-threading ችግር እንዳይፈጠር check_same_thread=False ተጨምሯል
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
    ''')[cite: 1]
    
    cursor.execute("SELECT COUNT(*) FROM tickets")
    if cursor.fetchone()[0] == 0:
        tickets_data = [(i, 'available') for i in range(1, 2201)][cite: 1]
        cursor.executemany("INSERT INTO tickets (number, status) VALUES (?, ?)", tickets_data)[cite: 1]
        
    conn.commit()
    conn.close()

init_db()

# --- API ENDPOINTS FOR WEBSITE ---

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT number, status FROM tickets")[cite: 1]
    rows = cursor.fetchall()
    conn.close()
    
    tickets_status = {row['number']: row['status'] for row in rows}
    return jsonify(tickets_status)

@app.route('/api/submit-order', methods=['POST'])
def submit_order():
    data = request.json[cite: 1]
    selected_numbers = data.get('numbers', [])[cite: 1]
    user_id = data.get('user_id')[cite: 1]
    user_name = data.get('user_name')[cite: 1]
    user_phone = data.get('user_phone')[cite: 1]
    referrer = data.get('referrer', 'የለም')[cite: 1]
    receipt_b64 = data.get('receipt_url') # Base64 Image

    if not selected_numbers:
        return jsonify({"success": False, "message": "ምንም ቁጥር አልተመረጠም!"}), 400[cite: 1]

    conn = get_db_connection()
    cursor = conn.cursor()

    placeholders = ','.join(['?'] * len(selected_numbers))[cite: 1]
    cursor.execute(f"SELECT number FROM tickets WHERE number IN ({placeholders}) AND status != 'available'", selected_numbers)[cite: 1]
    taken = cursor.fetchall()

    if taken:
        conn.close()
        taken_nums = [t['number'] for t in taken]
        return jsonify({"success": False, "message": f"እነዚህ ቁጥሮች ቀደም ብለው ተይዘዋል፡ {taken_nums}"}), 400[cite: 1]

    cursor.execute(f'''
        UPDATE tickets 
        SET status = 'pending', user_id = ?, user_name = ?, user_phone = ?, referrer = ?
        WHERE number IN ({placeholders})
    ''', [user_id, user_name, user_phone, referrer] + selected_numbers)[cite: 1]

    conn.commit()
    conn.close()

    # ደረሰኙን ጨምሮ ለአድሚን መላክ
    send_admin_verification(selected_numbers, user_name, user_phone, referrer, user_id, receipt_b64)

    return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል! በአድሚን በማረጋገጥ ላይ ይገኛል።"})[cite: 1]

# --- TELEGRAM BOT LOGIC ---

def send_admin_verification(numbers, user_name, user_phone, referrer, user_id, receipt_b64):
    bot = Bot(token=BOT_TOKEN)[cite: 1]
    
    nums_str = ", ".join(map(str, numbers))[cite: 1]
    total_price = len(numbers) * 3000[cite: 1]
    
    msg_text = (
        f"🚨 **አዲስ የቲኬት ደረሰኝ ደርሷል!**\n\n"[cite: 1]
        f"👤 **ደንበኛ፡** {user_name}\n"[cite: 1]
        f"📞 **ስልክ/ID፡** `{user_id}`\n"
        f"🎟️ **የተመረጡ ቁጥሮች፡** `{nums_str}`\n"[cite: 1]
        f"💰 **ጠቅላላ ክፍያ፡** {total_price:,} Birr\n"[cite: 1]
        f"✍️ **ቆራጭ/አስገባጭ፡** {referrer}\n\n"[cite: 1]
        f"እባክዎን ደረሰኙን አጣርተው ያጽድቁ ወይም ውድቅ ያድርጉ።"[cite: 1]
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve (አጽድቅ)", callback_data=f"approve_{user_id}_{'-'.join(map(str, numbers))}"),[cite: 1]
            InlineKeyboardButton("❌ Reject (ሰርዝ)", callback_data=f"reject_{user_id}_{'-'.join(map(str, numbers))}")[cite: 1]
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)[cite: 1]

    # የደረሰኝ ምስል ካለ በፎቶ መልክ ይላካል
    if receipt_b64 and "," in receipt_b64:
        try:
            image_data = base64.b64decode(receipt_b64.split(",")[1])
            photo_bytes = io.BytesIO(image_data)
            photo_bytes.name = 'receipt.jpg'
            
            bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=photo_bytes,
                caption=msg_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            print(f"Photo send failed, sending text only: {e}")

    # ምስል ከሌለ ወይም ካልተላከ በጽሁፍ ብቻ ይላካል
    bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg_text, parse_mode="Markdown", reply_markup=reply_markup)[cite: 1]

async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query[cite: 1]
    await query.answer()[cite: 1]

    data = query.data.split('_')[cite: 1]
    action = data[0][cite: 1]
    user_id = data[1][cite: 1]
    numbers = list(map(int, data[2].split('-')))[cite: 1]
    placeholders = ','.join(['?'] * len(numbers))[cite: 1]

    conn = get_db_connection()
    cursor = conn.cursor()

    if action == "approve":
        cursor.execute(f"UPDATE tickets SET status = 'sold' WHERE number IN ({placeholders})", numbers)[cite: 1]
        conn.commit()
        conn.close()

        await query.edit_message_caption(caption=f"✅ **ቲኬት ቁጥር {numbers} በስኬት ጸድቋል (Sold)!**") if query.message.photo else await query.edit_message_text(text=f"✅ **ቲኬት ቁጥር {numbers} በስኬት ጸድቋል (Sold)!**")
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 **እንኳን ደስ አለዎት!**\n\nየቆረጧቸው ቲኬቶች (ቁጥር፡ {numbers}) በስኬት ጸድቀዋል። መልካም ዕድል!"[cite: 1]
            )
        except Exception as e:
            print(f"ለደንበኛው መልእክት መላክ አልተቻለም: {e}")[cite: 1]

    elif action == "reject":
        cursor.execute(f"UPDATE tickets SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL WHERE number IN ({placeholders})", numbers)[cite: 1]
        conn.commit()
        conn.close()

        await query.edit_message_caption(caption=f"❌ **ቲኬት ቁጥር {numbers} ውድቅ ተደርጓል (ከስርዓቱ ተሰርዟል)!**") if query.message.photo else await query.edit_message_text(text=f"❌ **ቲኬት ቁጥር {numbers} ውድቅ ተደርጓል (ከስርዓቱ ተሰርዟል)!**")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ **ማሳወቂያ፡**\n\nየላኩት የክፍያ ደረሰኝ ውድቅ ስለተደረገ የተያዙት ቁጥሮች ({numbers}) ተመልሰው ነፃ ሆነዋል። እባክዎን እንደገና ይሞክሩ።"[cite: 1]
            )
        except Exception as e:
            print(f"ለደንበኛው መልእክት መላክ አልተቻለም: {e}")[cite: 1]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🚗 መኪና እቁብ/ሎተሪ ቁረጥ", web_app={"url": WEB_APP_URL})]][cite: 1]
    reply_markup = InlineKeyboardMarkup(keyboard)[cite: 1]
    await update.message.reply_text("እንኳን ወደ Getachew Fikadu Car Ekub በደህና መጡ! ቁጥር ለመቁረጥ ከታች ያለውን ቁልፍ ይጫኑ፡", reply_markup=reply_markup)[cite: 1]

# --- MAIN RUNNER ---
if __name__ == '__main__':
    from threading import Thread
    def run_flask():
        app.run(host='0.0.0.0', port=5000)[cite: 1]
    
    Thread(target=run_flask).start()[cite: 1]

    application = Application.builder().token(BOT_TOKEN).build()[cite: 1]
    application.add_handler(CommandHandler("start", start_command))[cite: 1]
    application.add_handler(CallbackQueryHandler(handle_admin_action))[cite: 1]
    
    print("🤖 Bot and Backend are running...")[cite: 1]
    application.run_polling()[cite: 1]
