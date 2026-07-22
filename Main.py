import os
import sqlite3
import json
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# --- CONFIGURATION ---
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"  # ከ BotFather ያገኙትን Token እዚህ ያስገቡ
ADMIN_CHAT_ID = "YOUR_ADMIN_CHAT_ID_HERE"    # የርስዎ/የአድሚኑ የቴሌግራም Chat ID
WEB_APP_URL = "https://your-app.netlify.app" # የዌብሳይትዎ (Netlify/Render) ሊንክ

# --- FLASK APP SETUP ---
app = Flask(__name__)
CORS(app)

# --- DATABASE SETUP ---
DB_NAME = "ekub_lottery.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # የቁጥሮች እና የቲኬቶች ሰንጠረዥ
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            number INTEGER PRIMARY KEY,
            status TEXT DEFAULT 'available', -- available, pending, sold
            user_id TEXT,
            user_name TEXT,
            user_phone TEXT,
            referrer TEXT,
            receipt_file_id TEXT
        )
    ''')
    
    # 1 - 2200 ያሉ ቁጥሮችን በመጀመሪያ ጊዜ መሙላት
    cursor.execute("SELECT COUNT(*) FROM tickets")
    if cursor.fetchone()[0] == 0:
        tickets_data = [(i, 'available') for i in range(1, 2201)]
        cursor.executemany("INSERT INTO tickets (number, status) VALUES (?, ?)", tickets_data)
        
    conn.commit()
    conn.close()

init_db()

# --- API ENDPOINTS FOR WEBSITE ---

@app.route('/api/get-tickets', methods=['GET'])
def get_tickets():
    """ በዌብሳይቱ ላይ የቁጥሮችን ሁኔታ (Available/Pending/Sold) ለማሳየት """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT number, status FROM tickets")
    rows = cursor.fetchall()
    conn.close()
    
    tickets_status = {row[0]: row[1] for row in rows}
    return jsonify(tickets_status)


@app.route('/api/submit-order', methods=['POST'])
def submit_order():
    """ ከዌብሳይቱ አዲስ ትዕዛዝ እና ደረሰኝ ሲላክ የሚቀበል """
    data = request.json
    selected_numbers = data.get('numbers', [])
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    user_phone = data.get('user_phone')
    referrer = data.get('referrer', 'የለም')
    receipt_image_url = data.get('receipt_url')

    if not selected_numbers:
        return jsonify({"success": False, "message": "ምንም ቁጥር አልተመረጠም!"}), 400

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # ቁጥሮቹ ነፃ መሆናቸውን ማረጋገጥ
    placeholders = ','.join(['?'] * len(selected_numbers))
    cursor.execute(f"SELECT number FROM tickets WHERE number IN ({placeholders}) AND status != 'available'", selected_numbers)
    taken = cursor.fetchall()

    if taken:
        conn.close()
        taken_nums = [t[0] for t in taken]
        return jsonify({"success": False, "message": f"እነዚህ ቁጥሮች ቀደም ብለው ተይዘዋል፡ {taken_nums}"}), 400

    # ቁጥሮቹን ወደ 'pending' መቀየር
    cursor.execute(f'''
        UPDATE tickets 
        SET status = 'pending', user_id = ?, user_name = ?, user_phone = ?, referrer = ?
        WHERE number IN ({placeholders})
    ''', [user_id, user_name, user_phone, referrer] + selected_numbers)

    conn.commit()
    conn.close()

    # ለአድሚን በቴሌግራም የማረጋገጫ መልእክት መላክ
    send_admin_verification(selected_numbers, user_name, user_phone, referrer, user_id)

    return jsonify({"success": True, "message": "ትዕዛዝዎ በስኬት ተልኳል! በአድሚን በማረጋገጥ ላይ ይገኛል።"})


# --- TELEGRAM BOT LOGIC ---

def send_admin_verification(numbers, user_name, user_phone, referrer, user_id):
    """ ለአድሚኑ የጽሁፍ መልእክት እና Approve/Reject ቁልፎችን ይልካል """
    bot = Bot(token=BOT_TOKEN)
    
    nums_str = ", ".join(map(str, numbers))
    total_price = len(numbers) * 3000
    
    msg_text = (
        f"🚨 **አዲስ የቲኬት ደረሰኝ ደርሷል!**\n\n"
        f"👤 **ደንበኛ፡** {user_name}\n"
        f"📞 **ስልክ፡** {user_phone}\n"
        f"🎟️ **የተመረጡ ቁጥሮች፡** `{nums_str}`\n"
        f"💰 **ጠቅላላ ክፍያ፡** {total_price:,} Birr\n"
        f"✍️ **ቆራጭ/አስገባጭ፡** {referrer}\n\n"
        f"እባክዎን ደረሰኙን አጣርተው ያጽድቁ ወይም ውድቅ ያድርጉ።"
    )

    # Approve እና Reject ቁልፎች (Callback Data ይይዛሉ)
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve (አጽድቅ)", callback_data=f"approve_{user_id}_{'-'.join(map(str, numbers))}"),
            InlineKeyboardButton("❌ Reject (ሰርዝ)", callback_data=f"reject_{user_id}_{'-'.join(map(str, numbers))}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg_text, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ አድሚኑ Approve ወይም Reject ሲጫን የሚሰራ """
    query = update.callback_query
    await query.answer()

    data = query.data.split('_')
    action = data[0]
    user_id = data[1]
    numbers = list(map(int, data[2].split('-')))
    placeholders = ','.join(['?'] * len(numbers))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if action == "approve":
        # ቁጥሮቹን ወደ 'sold' መቀየር
        cursor.execute(f"UPDATE tickets SET status = 'sold' WHERE number IN ({placeholders})", numbers)
        conn.commit()
        conn.close()

        await query.edit_message_text(text=f"✅ **ቲኬት ቁጥር {numbers} በስኬት ጸድቋል (Sold)!**")
        
        # ለደንበኛው የማረጋገጫ መልእክት መላክ
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 **እንኳን ደስ አለዎት!**\n\nየቆረጧቸው ቲኬቶች (ቁጥር፡ {numbers}) በስኬት ጸድቀዋል። መልካም ዕድል!"
            )
        except Exception as e:
            print(f"ለደንበኛው መልእክት መላክ አልተቻለም: {e}")

    elif action == "reject":
        # ቁጥሮቹን ተመልሰው 'available' ማድረግ
        cursor.execute(f"UPDATE tickets SET status = 'available', user_id=NULL, user_name=NULL, user_phone=NULL, referrer=NULL WHERE number IN ({placeholders})", numbers)
        conn.commit()
        conn.close()

        await query.edit_message_text(text=f"❌ **ቲኬት ቁጥር {numbers} ውድቅ ተደርጓል (ከስርዓቱ ተሰርዟል)!**")

        # ለደንበኛው የማሳወቂያ መልእክት መላክ
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ **ማሳወቂያ፡**\n\nየላኩት የክፍያ ደረሰኝ ውድቅ ስለተደረገ የተያዙት ቁጥሮች ({numbers}) ተመልሰው ነፃ ሆነዋል። እባክዎን እንደገና ይሞክሩ።"
            )
        except Exception as e:
            print(f"ለደንበኛው መልእክት መላክ አልተቻለም: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ ደንበኛው /start ሲል የሚላክ Web App Button """
    keyboard = [[InlineKeyboardButton("🚗 መኪና እቁብ/ሎተሪ ቁረጥ", web_app={"url": WEB_APP_URL})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("እንኳን ወደ Getachew Fikadu Car Ekub በደህና መጡ! ቁጥር ለመቁረጥ ከታች ያለውን ቁልፍ ይጫኑ፡", reply_markup=reply_markup)


# --- MAIN RUNNER ---
if __name__ == '__main__':
    # Flask backend ን ማስነሳት
    from threading import Thread
    def run_flask():
        app.run(host='0.0.0.0', port=5000)
    
    Thread(target=run_flask).start()

    # Telegram Bot ን ማስነሳት
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_admin_action))
    
    print("🤖 Bot and Backend are running...")
    application.run_polling()
