import logging
import sqlite3
import asyncio
import os
import json
from datetime import datetime, timedelta
from io import BytesIO
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from PIL import Image
import requests
import pytz
from telegram.ext import JobQueue

# Logging sozlamalari
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Konfiguratsiya
API_KEYS = [
    "AIzaSyAMkhZR1foHJk_y2wVg2F5wrOEjj590BJc",
    "AIzaSyD23AJ0fZiN6ELHKHJTUbiL8EqLswzPWmA",
    "AIzaSyB8QL_c9GzAXRRL4ZS_BafuW74mjceBzUg", 
    "AIzaSyDQPaUa-wIX4xpoiXwfHD2P1h5CTt6c4qA",
    "AIzaSyBa2SKZ9e7BPCImOgDfHvsVRb4J6hqLRGM"
]
TELEGRAM_TOKEN = "8386018951:AAFxK6zUhZjNvlnMSJICk81WRVi2FmIX1vU"
ADMIN_ID = 7445142075

# Majburiy kanal
REQUIRED_CHANNEL = "@sayfiddinov22"  # Bu yerga o'z kanalingiz ID sini kiriting
CHANNEL_ENABLED = False  # True qilib qo'ysangiz kanal majburiy bo'ladi

# Gemini konfiguratsiyasi
current_api_index = 0
genai.configure(api_key=API_KEYS[current_api_index])

# JSON fayl nomlari
CHATS_FILE = "user_chats.json"
BLOCKS_FILE = "blocked_users.json"

# JSON fayllarni yuklash va saqlash
def load_json_file(filename):
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_json_file(filename, data):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"JSON saqlashda xato: {e}")

# Chat va block ma'lumotlarini yuklash
user_chats = load_json_file(CHATS_FILE)
blocked_users = load_json_file(BLOCKS_FILE)

# Ma'lumotlar bazasi sozlamalari
def init_database():
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    
    # Foydalanuvchilar jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_pro BOOLEAN DEFAULT 0,
            pro_expiry DATE,
            daily_questions INTEGER DEFAULT 0,
            last_reset DATE,
            is_blocked BOOLEAN DEFAULT 0,
            block_expiry DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Savollar tarixi jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            question TEXT,
            answer TEXT,
            has_image BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Kanal sozlamalari jadvali
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_settings (
            id INTEGER PRIMARY KEY,
            channel_id TEXT,
            channel_name TEXT,
            is_enabled BOOLEAN DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()

def get_user_info(user_id):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def add_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    
    # Foydalanuvchi mavjudligini tekshirish
    cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    existing_user = cursor.fetchone()
    
    if existing_user:
        # Mavjud foydalanuvchi ma'lumotlarini yangilash
        cursor.execute('''
            UPDATE users SET username = ?, first_name = ?, last_name = ?
            WHERE user_id = ?
        ''', (username, first_name, last_name, user_id))
    else:
        # Yangi foydalanuvchi qo'shish
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, last_reset, daily_questions)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name, datetime.now().date(), 0))
    
    conn.commit()
    conn.close()

def is_user_blocked(user_id):
    """Foydalanuvchi bloklangan yoki yo'qligini tekshirish"""
    if str(user_id) in blocked_users:
        block_info = blocked_users[str(user_id)]
        if block_info.get('permanent', False):
            return True, "permanent"
        
        expiry_date = datetime.strptime(block_info['expiry'], '%Y-%m-%d').date()
        if expiry_date >= datetime.now().date():
            return True, expiry_date
        else:
            # Muddati tugagan blokni o'chirish
            del blocked_users[str(user_id)]
            save_json_file(BLOCKS_FILE, blocked_users)
    return False, None

def block_user(user_id, days=None):
    """Foydalanuvchini bloklash"""
    if days is None:
        blocked_users[str(user_id)] = {"permanent": True, "blocked_at": datetime.now().isoformat()}
    else:
        expiry_date = datetime.now() + timedelta(days=days)
        blocked_users[str(user_id)] = {
            "expiry": expiry_date.date().isoformat(),
            "blocked_at": datetime.now().isoformat(),
            "permanent": False
        }
    save_json_file(BLOCKS_FILE, blocked_users)

def unblock_user(user_id):
    """Foydalanuvchini blokdan chiqarish"""
    if str(user_id) in blocked_users:
        del blocked_users[str(user_id)]
        save_json_file(BLOCKS_FILE, blocked_users)
        return True
    return False

async def check_channel_membership(bot, user_id):
    """Kanalga a'zolikni tekshirish"""
    global CHANNEL_ENABLED, REQUIRED_CHANNEL
    
    if not CHANNEL_ENABLED or not REQUIRED_CHANNEL:
        return True
    
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def update_daily_questions(user_id):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    
    today = datetime.now().date()
    cursor.execute('SELECT daily_questions, last_reset FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        daily_questions, last_reset = result
        if last_reset:
            if isinstance(last_reset, str):
                last_reset_date = datetime.strptime(last_reset, '%Y-%m-%d').date()
            else:
                last_reset_date = last_reset
        else:
            last_reset_date = today
        
        if last_reset_date < today:
            # Yangi kun - hisoblagichni 1 ga o'rnatish
            cursor.execute('UPDATE users SET daily_questions = 1, last_reset = ? WHERE user_id = ?', 
                         (today, user_id))
        else:
            # Bir xil kun - hisoblagichni oshirish
            cursor.execute('UPDATE users SET daily_questions = daily_questions + 1 WHERE user_id = ?', 
                         (user_id,))
    else:
        # Foydalanuvchi topilmasa, uni qo'shish
        cursor.execute('''
            INSERT INTO users (user_id, daily_questions, last_reset)
            VALUES (?, ?, ?)
        ''', (user_id, 1, today))
    
    conn.commit()
    conn.close()

def get_daily_questions_count(user_id):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    
    today = datetime.now().date()
    cursor.execute('SELECT daily_questions, last_reset FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        daily_questions, last_reset = result
        if last_reset:
            if isinstance(last_reset, str):
                last_reset_date = datetime.strptime(last_reset, '%Y-%m-%d').date()
            else:
                last_reset_date = last_reset
        else:
            last_reset_date = today
        
        if last_reset_date < today:
            # Yangi kun boshlanganda hisoblagichni 0 ga qaytarish
            cursor.execute('UPDATE users SET daily_questions = 0, last_reset = ? WHERE user_id = ?', 
                         (today, user_id))
            conn.commit()
            conn.close()
            return 0
        
        conn.close()
        return daily_questions if daily_questions else 0
    
    conn.close()
    return 0

def is_pro_user(user_id):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    cursor.execute('SELECT is_pro, pro_expiry FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    
    if result:
        is_pro, pro_expiry = result
        if is_pro and pro_expiry:
            if isinstance(pro_expiry, str):
                expiry_date = datetime.strptime(pro_expiry, '%Y-%m-%d').date()
            else:
                expiry_date = pro_expiry
            
            if expiry_date >= datetime.now().date():
                conn.close()
                return True
            else:
                # Pro muddati tugagan - o'chirish
                cursor.execute('UPDATE users SET is_pro = 0, pro_expiry = NULL WHERE user_id = ?', (user_id,))
                conn.commit()
    
    conn.close()
    return False

def give_pro_access(user_id, days=30):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    
    expiry_date = datetime.now() + timedelta(days=days)
    cursor.execute('UPDATE users SET is_pro = 1, pro_expiry = ? WHERE user_id = ?', 
                   (expiry_date.date(), user_id))
    conn.commit()
    conn.close()

def remove_pro_access(user_id):
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_pro = 0, pro_expiry = NULL WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

async def check_and_notify_expiries(context: ContextTypes.DEFAULT_TYPE):
    """Pro obuna va blok muddatlarini tekshirish va xabar berish"""
    try:
        current_date = datetime.now().date()
        current_time, _ = get_current_time()
        
        # Pro obuna muddatlarini tekshirish
        conn = sqlite3.connect('dilshod_ai.db')
        cursor = conn.cursor()
        
        # Muddati tugagan pro foydalanuvchilarni topish
        cursor.execute('''
            SELECT user_id, first_name, pro_expiry 
            FROM users 
            WHERE is_pro = 1 AND pro_expiry IS NOT NULL AND pro_expiry <= ?
        ''', (current_date,))
        
        expired_pro_users = cursor.fetchall()
        
        for user_id, first_name, pro_expiry in expired_pro_users:
            # Pro obunani o'chirish
            cursor.execute('UPDATE users SET is_pro = 0, pro_expiry = NULL WHERE user_id = ?', (user_id,))
            
            # Foydalanuvchiga xabar yuborish
            try:
                pro_tugadi_xabari = f"""⏰ Pro obuna muddati tugadi!

Hurmatli {first_name or 'foydalanuvchi'}, sizning Pro obunangiz muddati tugadi.

Endi siz:
❌ Kuniga faqat 1 ta savol bera olasiz
❌ Rasmlarni yuklay olmaysiz

🔄 Qayta Pro obuna olish uchun admin bilan bog'laning:
👨‍💻 @dilshod_sayfiddinov

🕐 Tugagan vaqt: {current_time}"""
                
                await context.bot.send_message(user_id, pro_tugadi_xabari)
                logger.info(f"Pro muddati tugagan foydalanuvchiga xabar yuborildi: {user_id}")
            except Exception as e:
                logger.error(f"Pro tugash xabarini yuborishda xato {user_id}: {e}")
        
        conn.commit()
        conn.close()
        
        # Blok muddatlarini tekshirish
        expired_blocks = []
        for user_id_str, block_info in list(blocked_users.items()):
            if not block_info.get('permanent', False):
                expiry_date = datetime.strptime(block_info['expiry'], '%Y-%m-%d').date()
                if expiry_date <= current_date:
                    expired_blocks.append(user_id_str)
        
        # Muddati tugagan bloklarni o'chirish va xabar yuborish
        for user_id_str in expired_blocks:
            user_id = int(user_id_str)
            del blocked_users[user_id_str]
            
            # Foydalanuvchiga xabar yuborish
            try:
                # Foydalanuvchi ismini olish
                conn = sqlite3.connect('dilshod_ai.db')
                cursor = conn.cursor()
                cursor.execute('SELECT first_name FROM users WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                first_name = result[0] if result else 'foydalanuvchi'
                conn.close()
                
                blok_tugadi_xabari = f"""✅ Blok muddati tugadi!

Hurmatli {first_name}, sizning blok muddatingiz tugadi.

Endi botdan erkin foydalanishingiz mumkin! 🎉

🕐 Tugagan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                
                await context.bot.send_message(user_id, blok_tugadi_xabari)
                logger.info(f"Blok muddati tugagan foydalanuvchiga xabar yuborildi: {user_id}")
            except Exception as e:
                logger.error(f"Blok tugash xabarini yuborishda xato {user_id}: {e}")
        
        # Yangilangan blok ma'lumotlarini saqlash
        if expired_blocks:
            save_json_file(BLOCKS_FILE, blocked_users)
            logger.info(f"{len(expired_blocks)} ta blok muddati tugadi va o'chirildi")
        
        if expired_pro_users:
            logger.info(f"{len(expired_pro_users)} ta pro obuna muddati tugadi")
            
    except Exception as e:
        logger.error(f"Muddatlarni tekshirishda xato: {e}")
def save_question(user_id, question, answer, has_image=False):
    """Savolni ma'lumotlar bazasi va JSON faylga saqlash"""
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO questions (user_id, question, answer, has_image)
        VALUES (?, ?, ?, ?)
    ''', (user_id, question, answer, has_image))
    conn.commit()
    conn.close()
    
    # JSON faylga saqlash
    if str(user_id) not in user_chats:
        user_chats[str(user_id)] = []
    
    user_chats[str(user_id)].append({
        "question": question,
        "answer": answer,
        "has_image": has_image,
        "timestamp": datetime.now().isoformat()
    })
    
    # Faqat oxirgi 50 ta suhbatni saqlash (xotirani tejash uchun)
    if len(user_chats[str(user_id)]) > 50:
        user_chats[str(user_id)] = user_chats[str(user_id)][-50:]
    
    save_json_file(CHATS_FILE, user_chats)

def get_conversation_history(user_id, limit=10):
    """Foydalanuvchi bilan oxirgi suhbat tarixini olish"""
    if str(user_id) in user_chats:
        return user_chats[str(user_id)][-limit:]
    return []

def get_all_users():
    conn = sqlite3.connect('dilshod_ai.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, first_name, last_name, is_pro FROM users')
    users = cursor.fetchall()
    conn.close()
    return users

def get_current_time():
    """Toshkent vaqti bo'yicha hozirgi vaqt va kunni qaytarish"""
    tashkent_tz = pytz.timezone('Asia/Tashkent')
    now = datetime.now(tashkent_tz)
    
    days_uz = {
        0: "Dushanba", 1: "Seshanba", 2: "Chorshanba",
        3: "Payshanba", 4: "Juma", 5: "Shanba", 6: "Yakshanba"
    }
    
    months_uz = {
        1: "Yanvar", 2: "Fevral", 3: "Mart", 4: "Aprel",
        5: "May", 6: "Iyun", 7: "Iyul", 8: "Avgust",
        9: "Sentabr", 10: "Oktabr", 11: "Noyabr", 12: "Dekabr"
    }
    
    day_name = days_uz[now.weekday()]
    month_name = months_uz[now.month]
    
    formatted_time = f"{day_name}, {now.day} {month_name} {now.year} yil, soat {now.strftime('%H:%M')}"
    return formatted_time, now

async def get_gemini_response(text, image=None, is_pro=False, user_id=None):
    global current_api_index
    
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        # Suhbat tarixini olish
        conversation_context = ""
        if user_id:
            history = get_conversation_history(user_id, 10)
            if history:
                conversation_context = "\n\nOxirgi suhbatlarimiz:\n"
                for i, chat in enumerate(history, 1):
                    conversation_context += f"{i}. Savol: {chat['question']}\n   Javob: {chat['answer']}\n\n"
        
        # Vaqt so'ralgan bo'lsa, hozirgi vaqtni qo'shish
        time_info = ""
        if any(word in text.lower() for word in ['vaqt', 'soat', 'kun', 'sana', 'bugun', 'hozir']):
            current_time, _ = get_current_time()
            time_info = f"\n\nHozirgi vaqt: {current_time}"
        
        # Uy vazifasi uchun maxsus prompt
        homework_keywords = ['uy vazifa', 'homework', 'vazifa', 'mashq', 'topshiriq', 'test', 'imtihon']
        is_homework = any(keyword in text.lower() for keyword in homework_keywords)
        
        yaratuvchi_text = "Yaratuvchi: Dilshod Sayfiddinov"
        
        if is_homework:
            system_prompt = f"""Siz Dilshod AI - eng zo'r ta'lim yordamchisisiz. Siz o'quvchilarga uy vazifalari va darslik savollarda yordam berasiz. 

UY VAZIFASI YORDAMI QOIDALARI:
1. Javobni to'liq va tushuntirish bilan bering
2. Qadamma-qadamlik yechimni ko'rsating
3. Formulalar va misollar bering
4. Tushuntirishni oddiy va tushunarli qiling
5. Agar kerak bo'lsa qo'shimcha ma'lumot bering

Har doim o'zbek tilida (lotin alifbosida) javob bering. 
{yaratuvchi_text}

{conversation_context}"""
        elif is_pro:
            system_prompt = f"""Siz Dilshod AI - eng zo'r va aqlli yordamchi botsiz. Siz Pro foydalanuvchi bilan gaplashyapsiz, shuning uchun eng sifatli, batafsil va foydali javob bering. Har doim o'zbek tilida (lotin alifbosida) javob bering. Javoblaringiz professional, tushunarli va qiziqarli bo'lsin.

Yaratuvchi haqida so'ralsa "Dilshod Sayfiddinov" deb javob bering.
Agar foydalanuvchi oldingi suhbatlarimizga murojaat qilsa, oxirgi suhbatlarimizdan foydalaning.
O'zingizni faqat Dilshod AI deb tanishtiring, Gemini yoki Google haqida gapirmang.

{conversation_context}"""
        else:
            system_prompt = f"""Siz Dilshod AI - yordamchi botsiz. Har doim o'zbek tilida (lotin alifbosida) javob bering. Tushunarli va foydali javob bering.

{yaratuvchi_text}
O'zingizni faqat Dilshod AI deb tanishtiring.

{conversation_context}"""
        
        full_prompt = system_prompt + "\n\nHozirgi savol: " + text + time_info
        
        if image:
            response = model.generate_content([full_prompt, image])
        else:
            response = model.generate_content(full_prompt)
        
        return response.text
        
    except Exception as e:
        current_api_index = (current_api_index + 1) % len(API_KEYS)
        genai.configure(api_key=API_KEYS[current_api_index])
        
        try:
            model = genai.GenerativeModel('gemini-2.0-flash-exp')
            full_prompt = system_prompt + "\n\nHozirgi savol: " + text + time_info
            
            if image:
                response = model.generate_content([full_prompt, image])
            else:
                response = model.generate_content(full_prompt)
            return response.text
        except Exception as e2:
            logger.error(f"Gemini API xatosi: {e2}")
            return "Uzr, javob berishda biroz kechikish bo'ldi. Qaytadan so'rang."

def get_admin_keyboard():
    """Admin uchun asosiy klaviatura"""
    keyboard = [
        [KeyboardButton("👥 Foydalanuvchilar"), KeyboardButton("📊 Statistika")],
        [KeyboardButton("🎁 Pro berish"), KeyboardButton("❌ Pro o'chirish")],
        [KeyboardButton("🚫 Bloklash"), KeyboardButton("✅ Blokdan chiqarish")],
        [KeyboardButton("📢 Xabar yuborish"), KeyboardButton("📺 Kanal sozlamalari")],
        [KeyboardButton("🔙 Oddiy foydalanuvchi")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_user_keyboard():
    """Oddiy foydalanuvchi uchun klaviatura"""
    keyboard = [
        [KeyboardButton("ℹ️ Ma'lumot"), KeyboardButton("⭐ Pro bo'lish")],
        [KeyboardButton("📞 Aloqa")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Foydalanuvchini bazaga qo'shish (har safar tekshirish)
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    # Bloklangan foydalanuvchini tekshirish
    is_blocked, block_info = is_user_blocked(user.id)
    if is_blocked:
        if block_info == "permanent":
            await update.message.reply_text("🚫 Siz botdan doimiy bloklangansiz!")
        else:
            await update.message.reply_text(f"🚫 Siz {block_info} gacha bloklangansiz!")
        return
    
    # Kanal a'zoligini tekshirish
    if not await check_channel_membership(context.bot, user.id):
        keyboard = [[InlineKeyboardButton("✅ Tekshirish", callback_data="check_channel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📺 Botdan foydalanish uchun avval kanalimizga a'zo bo'ling:\n\n"
            f"👉 {REQUIRED_CHANNEL}\n\n"
            f"A'zo bo'lgach \"✅ Tekshirish\" tugmasini bosing.",
            reply_markup=reply_markup
        )
        return
    
    current_time, _ = get_current_time()
    
    welcome_message = f"""🤖 Salom {user.first_name}! Men Dilshod AI man!

Men sizga har qanday savollaringizga javob berishga tayyorman. Rasmlarni ham tahlil qila olaman va uy vazifalaringizda yordam bera olaman.

🧠 **Muhim:** Men sizning barcha suhbatlaringizni eslab qolaman!

📊 Sizning holatingiz:
{'🌟 Pro foydalanuvchi' if is_pro_user(user.id) else '👤 Oddiy foydalanuvchi (kuniga 1 ta savol)'}

🕐 Hozirgi vaqt: {current_time}
👨‍💻 Yaratuvchi: Dilshod Sayfiddinov

Menga savolingizni yuboring!
"""
    
    if user.id == ADMIN_ID:
        reply_markup = get_admin_keyboard()
    else:
        reply_markup = get_user_keyboard()
    
    await update.message.reply_text(welcome_message, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    # Foydalanuvchini bazaga qo'shish (har safar tekshirish)
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    # Bloklangan foydalanuvchini tekshirish
    is_blocked, block_info = is_user_blocked(user.id)
    if is_blocked:
        if block_info == "permanent":
            await update.message.reply_text("🚫 Siz botdan doimiy bloklangansiz!")
        else:
            await update.message.reply_text(f"🚫 Siz {block_info} gacha bloklangansiz!")
        return
    
    # Kanal a'zoligini tekshirish
    if not await check_channel_membership(context.bot, user.id):
        keyboard = [[InlineKeyboardButton("✅ Tekshirish", callback_data="check_channel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📺 Kanalimizga a'zo bo'ling: {REQUIRED_CHANNEL}",
            reply_markup=reply_markup
        )
        return
    
    # Admin tugmalari
    if user.id == ADMIN_ID:
        if text == "👥 Foydalanuvchilar":
            await show_users_list(update, context, page=1)
            return
        elif text == "📊 Statistika":
            await show_statistics(update, context)
            return
        elif text == "🎁 Pro berish":
            await update.message.reply_text(
                "🎁 Pro berish:\n\n"
                "Format: /gift user_id days\n"
                "Misol: /gift 123456789 30\n\n"
                "Doimiy pro uchun: /gift 123456789 permanent"
            )
            return
        elif text == "❌ Pro o'chirish":
            await update.message.reply_text(
                "❌ Pro o'chirish:\n\n"
                "Format: /removepro user_id\n"
                "Misol: /removepro 123456789"
            )
            return
        elif text == "🚫 Bloklash":
            await update.message.reply_text(
                "🚫 Foydalanuvchini bloklash:\n\n"
                "Format: /block user_id days\n"
                "Misol: /block 123456789 7\n\n"
                "Doimiy bloklash uchun: /block 123456789 permanent"
            )
            return
        elif text == "✅ Blokdan chiqarish":
            await update.message.reply_text(
                "✅ Blokdan chiqarish:\n\n"
                "Format: /unblock user_id\n"
                "Misol: /unblock 123456789"
            )
            return
        elif text == "📢 Xabar yuborish":
            await update.message.reply_text(
                "📢 Xabar yuborish:\n\n"
                "Format: /broadcast type message\n\n"
                "Turlar:\n"
                "- all: hammaga\n"
                "- pro: pro foydalanuvchilarga\n"
                "- regular: oddiy foydalanuvchilarga\n\n"
                "Misol: /broadcast all Salom hammaga!"
            )
            return
        elif text == "📺 Kanal sozlamalari":
            await show_channel_settings(update, context)
            return
        elif text == "🔙 Oddiy foydalanuvchi":
            reply_markup = get_user_keyboard()
            await update.message.reply_text("Oddiy foydalanuvchi rejimiga o'tdingiz", reply_markup=reply_markup)
            return
    
    # Oddiy foydalanuvchi tugmalari
    if text == "ℹ️ Ma'lumot":
        current_time, _ = get_current_time()
        
        # Pro foydalanuvchi ekanligini tekshirish
        is_pro = is_pro_user(user.id)
        
        # Matnlarni tayyorlash
        pro_questions_text = '✅ Cheksiz savollar (Pro)' if is_pro else '❌ Cheksiz savollar (Pro)'
        pro_images_text = '✅ Rasmlarni tahlil qilish (Pro)' if is_pro else '❌ Rasmlarni tahlil qilish (Pro)'
        
        if is_pro:
            status_text = '🌟 Pro foydalanuvchi'
        else:
            daily_count = get_daily_questions_count(user.id)
            status_text = f'👤 Oddiy foydalanuvchi\n📝 Bugungi savollar: {daily_count}/1'
        
        info_text = f"""ℹ️ **Bot haqida ma'lumot:**

🤖 **Bot nomi:** Uy ishichi 
👨‍💻 **Yaratuvchi:** Dilshod Sayfiddinov
📅 **Yaratilgan sana:** 2025 yil
🔄 **Oxirgi yangilanish:** {current_time}

📊 **Sizning holatingiz:**
{status_text}

🎯 **Imkoniyatlar:**
✅ Savollar va javoblar
✅ Uy vazifalari yechimi
✅ Suhbat tarixini eslab qolish
{pro_images_text}
{pro_questions_text}"""
        
        await update.message.reply_text(info_text)
        return
        
    elif text == "⭐ Pro bo'lish":
        await update.message.reply_text(
            "⭐ Pro versiya imkoniyatlari:\n\n"
            "✅ Cheksiz savollar\n"
            "✅ Rasmlarni yuklash va tahlil qilish\n"
            "✅ Tezroq va sifatliroq javoblar\n"
            "✅ Uy vazifalari uchun maxsus yordam\n"
            "✅ Suhbat tarixini to'liq eslab qolish\n\n"
            "👨‍💻 Yaratuvchi: Dilshod Sayfiddinov\n"
            "Pro versiyani olish uchun admin bilan bog'laning: @dilshod_sayfiddinov"
        )
        return
        
    elif text == "📞 Aloqa":
        await update.message.reply_text(
            "📞 **Aloqa ma'lumotlari:**\n\n"
            "👨‍💻 **Yaratuvchi:** Dilshod Sayfiddinov\n"
            "📱 **Telegram:** @dilshod_sayfiddinov\n"
            "📧 **Email:** sayfiddinovd25@gmail.com\n"
            "🌐 **Telefon raqam:** +998990953018\n\n"
            "💡 Takliflar, shikoyatlar yoki yordam uchun murojaat qiling!"
        )
        return
    
    # Foydalanuvchi limitini tekshirish
    if not is_pro_user(user.id) and user.id != ADMIN_ID:
        daily_count = get_daily_questions_count(user.id)
        if daily_count >= 1:
            await update.message.reply_text(
                "❌ Siz bugun 1 ta savolni allaqachon so'ragansiz.\n\n"
                "Pro versiyaga o'tib, cheksiz savol berish imkoniyatiga ega bo'ling!\n\n"
                "👨‍💻 Aloqa: @dilshod_sayfiddinov"
            )
            return
    
    # Savolni qayta ishlash
    await update.message.reply_text("🤔 O'ylayapman...")
    
    try:
        is_pro = is_pro_user(user.id) or user.id == ADMIN_ID
        response = await get_gemini_response(text, is_pro=is_pro, user_id=user.id)
        
        # Javobni saqlash
        save_question(user.id, text, response, False)
        
        # Savol hisoblagichini yangilash
        if not is_pro and user.id != ADMIN_ID:
            update_daily_questions(user.id)
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Xabar qayta ishlashda xato: {e}")
        await update.message.reply_text("Savolingizni qaytadan yuboring.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Foydalanuvchini bazaga qo'shish (har safar tekshirish)
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    # Bloklangan foydalanuvchini tekshirish
    is_blocked, block_info = is_user_blocked(user.id)
    if is_blocked:
        if block_info == "permanent":
            await update.message.reply_text("🚫 Siz botdan doimiy bloklangansiz!")
        else:
            await update.message.reply_text(f"🚫 Siz {block_info} gacha bloklangansiz!")
        return
    
    # Kanal a'zoligini tekshirish
    if not await check_channel_membership(context.bot, user.id):
        keyboard = [[InlineKeyboardButton("✅ Tekshirish", callback_data="check_channel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📺 Kanalimizga a'zo bo'ling: {REQUIRED_CHANNEL}",
            reply_markup=reply_markup
        )
        return
    
    # Pro emas va admin emas bo'lsa, rasm yuklashga ruxsat bermaslik
    if not is_pro_user(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text(
            "📸 Rasmlarni yuklash faqat Pro foydalanuvchilar uchun mavjud!\n\n"
            "👨‍💻 Pro bo'lish uchun: @dilshod_sayfiddinov"
        )
        return
    
    # Limit tekshiruvi
    if not is_pro_user(user.id) and user.id != ADMIN_ID:
        daily_count = get_daily_questions_count(user.id)
        if daily_count >= 1:
            await update.message.reply_text("❌ Siz bugun 1 ta savolni allaqachon so'ragansiz.")
            return
    
    await update.message.reply_text("📷 Rasmni tahlil qilayapman...")
    
    try:
        # Rasmni yuklab olish
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        # Rasmni BytesIO orqali yuklab olish
        image_bytes = BytesIO()
        await file.download_to_memory(image_bytes)
        image_bytes.seek(0)
        
        # PIL Image yaratish
        image = Image.open(image_bytes)
        
        # Matn olish
        caption = update.message.caption or "Bu rasmda nima bor?"
        
        is_pro = is_pro_user(user.id) or user.id == ADMIN_ID
        response = await get_gemini_response(caption, image, is_pro=is_pro, user_id=user.id)
        
        # Javobni saqlash
        save_question(user.id, f"[RASM] {caption}", response, True)
        
        # Savol hisoblagichini yangilash
        if not is_pro and user.id != ADMIN_ID:
            update_daily_questions(user.id)
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Rasm qayta ishlashda xato: {e}")
        await update.message.reply_text("Rasmni qaytadan yuboring.")

async def show_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    """Foydalanuvchilar ro'yxatini sahifalash bilan ko'rsatish"""
    users = get_all_users()
    total_users = len(users)
    users_per_page = 20
    total_pages = (total_users + users_per_page - 1) // users_per_page
    
    start_index = (page - 1) * users_per_page
    end_index = min(start_index + users_per_page, total_users)
    page_users = users[start_index:end_index]
    
    current_time, _ = get_current_time()
    user_text = f"👥 Foydalanuvchilar ro'yxati (Sahifa {page}/{total_pages}):\n\n"
    
    for i, user in enumerate(page_users, start_index + 1):
        user_id, username, first_name, last_name, is_pro = user
        name = f"{first_name or ''} {last_name or ''}".strip()
        if not name:
            name = username or "Noma'lum"
        pro_status = "⭐" if is_pro else "👤"
        
        # Bloklangan yoki yo'qligini tekshirish
        is_blocked, _ = is_user_blocked(user_id)
        block_status = " 🚫" if is_blocked else ""
        
        user_text += f"{i}. {pro_status} {name} - {user_id}{block_status}\n"
    
    jami_text = f"\n📊 Jami: {total_users} ta foydalanuvchi\n"
    vaqt_text = f"🕐 Ma'lumot vaqti: {current_time}"
    user_text += jami_text + vaqt_text
    
    # Sahifalash tugmalari
    keyboard = []
    nav_buttons = []
    
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Orqaga", callback_data=f"users_page_{page-1}"))
    
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("➡️ Oldinga", callback_data=f"users_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🔄 Yangilash", callback_data=f"users_page_{page}")])
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(user_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(user_text, reply_markup=reply_markup)

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot statistikasini ko'rsatish"""
    users = get_all_users()
    total_users = len(users)
    pro_users = len([u for u in users if u[4]])
    regular_users = total_users - pro_users
    
    # Bloklangan foydalanuvchilar soni
    blocked_count = len(blocked_users)
    
    current_time, _ = get_current_time()
    
    kanal_holati = 'Majburiy kanal yoqilgan' if CHANNEL_ENABLED else 'Majburiy kanal o\'chirilgan'
    kanal_nomi = REQUIRED_CHANNEL if REQUIRED_CHANNEL else 'Belgilanmagan'
    
    stats_text = f"""📊 Bot statistikasi:

👥 Jami foydalanuvchilar: {total_users}
⭐ Pro foydalanuvchilar: {pro_users}
👤 Oddiy foydalanuvchilar: {regular_users}
🚫 Bloklangan foydalanuvchilar: {blocked_count}

📺 Kanal sozlamalari:
{'✅' if CHANNEL_ENABLED else '❌'} {kanal_holati}
📢 Kanal: {kanal_nomi}

🕐 Ma'lumot vaqti: {current_time}
👨‍💻 Yaratuvchi: Dilshod Sayfiddinov"""
    
    if update.callback_query:
        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(stats_text)

async def show_channel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kanal sozlamalarini ko'rsatish"""
    global CHANNEL_ENABLED, REQUIRED_CHANNEL
    
    kanal_nomi = REQUIRED_CHANNEL if REQUIRED_CHANNEL else 'Belgilanmagan'
    kanal_holati = 'Yoqilgan' if CHANNEL_ENABLED else 'O\'chirilgan'
    
    settings_text = f"""📺 Kanal sozlamalari:

📢 **Majburiy kanal:** {kanal_nomi}
🔘 **Holati:** {'✅' if CHANNEL_ENABLED else '❌'} {kanal_holati}

📝 **Sozlash buyruqlari:**
• Kanalni o'rnatish: /setchannel @kanal_nomi
• Kanalni yoqish: /enablechannel
• Kanalni o'chirish: /disablechannel

⚠️ **Diqqat:** Majburiy kanal yoqilganda, foydalanuvchilar avval kanalga a'zo bo'lishi kerak."""
    
    if update.callback_query:
        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(settings_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(settings_text)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Foydalanuvchini bazaga qo'shish
    add_user(query.from_user.id, query.from_user.username, query.from_user.first_name, query.from_user.last_name)
    
    if query.data == "check_channel":
        if await check_channel_membership(context.bot, query.from_user.id):
            await query.edit_message_text("✅ A'zolik tasdiqlandi! Botdan foydalanishingiz mumkin.\n\nMenga savolingizni yuboring!")
        else:
            keyboard = [[InlineKeyboardButton("✅ Tekshirish", callback_data="check_channel")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            kanal_xabari = f"❌ Siz hali kanalga a'zo emassiz!\n\n👉 {REQUIRED_CHANNEL}\n\nA'zo bo'lgach qaytadan \"✅ Tekshirish\" tugmasini bosing."
            await query.edit_message_text(kanal_xabari, reply_markup=reply_markup)
    
    elif query.data == "admin_panel" and query.from_user.id == ADMIN_ID:
        await query.edit_message_text(
            "👨‍💼 Admin Panel\n\nKerakli tugmani tanlang:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="users_page_1")],
                [InlineKeyboardButton("📊 Statistika", callback_data="statistics")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back")]
            ])
        )
    
    elif query.data.startswith("users_page_"):
        page = int(query.data.split("_")[-1])
        await show_users_list(update, context, page)
    
    elif query.data == "statistics":
        await show_statistics(update, context)
    
    elif query.data == "back":
        await start(update, context)

# Admin buyruqlari
async def gift_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pro berish buyrug'i"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Noto'g'ri format!\n\n"
                "To'g'ri format: /gift user_id days\n"
                "Misol: /gift 123456789 30\n"
                "Doimiy pro: /gift 123456789 permanent"
            )
            return
        
        user_id = int(context.args[0])
        days_arg = context.args[1]
        
        current_time, _ = get_current_time()
        
        if days_arg.lower() == "permanent":
            # Doimiy pro (999 yil)
            give_pro_access(user_id, 365000)
            await update.message.reply_text(f"🎁 {user_id} foydalanuvchiga doimiy Pro berildi!")
            
            try:
                pro_xabari = f"""🎉 Tabriklaymiz! Sizga doimiy Pro versiya berildi!

Endi siz:
✅ Cheksiz savol bera olasiz
✅ Rasmlarni yuklash va tahlil qila olasiz  
✅ Uy vazifalari uchun maxsus yordam olasiz

🕐 Berilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                await context.bot.send_message(user_id, pro_xabari)
            except:
                pass
        else:
            days = int(days_arg)
            give_pro_access(user_id, days)
            await update.message.reply_text(f"🎁 {user_id} foydalanuvchiga {days} kunlik Pro berildi!")
            
            try:
                pro_xabari = f"""🎉 Tabriklaymiz! Sizga {days} kunlik Pro versiya berildi!

Endi siz:
✅ Cheksiz savol bera olasiz
✅ Rasmlarni yuklash va tahlil qila olasiz
✅ Uy vazifalari uchun maxsus yordam olasiz

🕐 Berilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                await context.bot.send_message(user_id, pro_xabari)
            except:
                pass
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /gift 123456789 30")

async def remove_pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pro o'chirish buyrug'i"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    try:
        user_id = int(context.args[0])
        remove_pro_access(user_id)
        current_time, _ = get_current_time()
        await update.message.reply_text(f"❌ {user_id} foydalanuvchidan Pro o'chirildi!")
        
        try:
            pro_ochirildi_xabari = f"""⚠️ Sizning Pro obunangiz bekor qilindi.

Endi siz kuniga 1 ta savol bera olasiz va rasmlarni yuklay olmaysiz.

🕐 O'chirilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
            await context.bot.send_message(user_id, pro_ochirildi_xabari)
        except:
            pass
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /removepro 123456789")

async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchini bloklash buyrug'i"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Noto'g'ri format!\n\n"
                "To'g'ri format: /block user_id days\n"
                "Misol: /block 123456789 7\n"
                "Doimiy bloklash: /block 123456789 permanent"
            )
            return
        
        user_id = int(context.args[0])
        days_arg = context.args[1]
        
        current_time, _ = get_current_time()
        
        if days_arg.lower() == "permanent":
            block_user(user_id)
            await update.message.reply_text(f"🚫 {user_id} foydalanuvchi doimiy bloklandi!")
            
            try:
                blok_xabari = f"""🚫 Siz botdan doimiy bloklangansiz!

🕐 Bloklangan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                await context.bot.send_message(user_id, blok_xabari)
            except:
                pass
        else:
            days = int(days_arg)
            block_user(user_id, days)
            expiry_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
            await update.message.reply_text(f"🚫 {user_id} foydalanuvchi {days} kunga bloklandi! ({expiry_date} gacha)")
            
            try:
                blok_xabari = f"""🚫 Siz botdan {days} kunga bloklangansiz!

📅 Blok tugashi: {expiry_date}
🕐 Bloklangan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                await context.bot.send_message(user_id, blok_xabari)
            except:
                pass
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /block 123456789 7")

async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchini blokdan chiqarish buyrug'i"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    try:
        user_id = int(context.args[0])
        
        if unblock_user(user_id):
            current_time, _ = get_current_time()
            await update.message.reply_text(f"✅ {user_id} foydalanuvchi blokdan chiqarildi!")
            
            try:
                blokdan_chiqarildi_xabari = f"""✅ Siz botdan blokdan chiqarildingiz!

Endi botdan erkin foydalanishingiz mumkin.

🕐 Chiqarilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                await context.bot.send_message(user_id, blokdan_chiqarildi_xabari)
            except:
                pass
        else:
            await update.message.reply_text(f"❌ {user_id} foydalanuvchi bloklanmagan!")
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /unblock 123456789")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xabar yuborish buyrug'i"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Noto'g'ri format!\n\n"
                "To'g'ri format: /broadcast type message\n\n"
                "Turlar:\n"
                "- all: hammaga\n"
                "- pro: pro foydalanuvchilarga\n"
                "- regular: oddiy foydalanuvchilarga\n\n"
                "Misol: /broadcast all Salom hammaga!"
            )
            return
        
        broadcast_type = context.args[0].lower()
        message_text = " ".join(context.args[1:])
        
        if broadcast_type not in ['all', 'pro', 'regular']:
            await update.message.reply_text("❌ Noto'g'ri tur! Foydalaning: all, pro, regular")
            return
        
        users = get_all_users()
        current_time, _ = get_current_time()
        
        # Foydalanuvchilarni filtrlash
        if broadcast_type == "pro":
            target_users = [u for u in users if u[4]]  # is_pro = True
        elif broadcast_type == "regular":
            target_users = [u for u in users if not u[4]]  # is_pro = False
        else:  # all
            target_users = users
        
        # Xabarni tayyorlash
        full_message = f"""📢 Admin xabari:

{message_text}

🕐 Yuborilgan vaqt: {current_time}
👨‍💻 Yuboruvchi: Dilshod Sayfiddinov (Admin)"""
        
        # Xabarni yuborish
        sent_count = 0
        failed_count = 0
        
        await update.message.reply_text(f"📤 {len(target_users)} ta foydalanuvchiga xabar yuborilmoqda...")
        
        for user in target_users:
            user_id = user[0]
            try:
                await context.bot.send_message(user_id, full_message)
                sent_count += 1
                await asyncio.sleep(0.1)  # Spam oldini olish uchun
            except Exception as e:
                failed_count += 1
                logger.error(f"Foydalanuvchi {user_id} ga xabar yuborishda xato: {e}")
        
        # Natijani ko'rsatish
        yakunlanish_vaqti = get_current_time()[0]
        result_message = f"""✅ Xabar yuborish yakunlandi!

📊 Natijalar:
✅ Muvaffaqiyatli yuborildi: {sent_count}
❌ Yuborilmadi: {failed_count}
📈 Jami: {len(target_users)}

🕐 Yakunlangan vaqt: {yakunlanish_vaqti}"""
        
        await update.message.reply_text(result_message)
        
    except Exception as e:
        logger.error(f"Broadcast xatosi: {e}")
        await update.message.reply_text("❌ Xabar yuborishda xato yuz berdi!")

# Kanal sozlamalari buyruqlari
async def set_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kanalni o'rnatish buyrug'i"""
    global REQUIRED_CHANNEL
    
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    try:
        if not context.args:
            await update.message.reply_text(
                "❌ Noto'g'ri format!\n\n"
                "To'g'ri format: /setchannel @kanal_nomi\n"
                "Misol: /setchannel @dilshod_ai_channel"
            )
            return
        
        channel = context.args[0]
        if not channel.startswith('@'):
            channel = '@' + channel
        
        REQUIRED_CHANNEL = channel
        await update.message.reply_text(f"✅ Majburiy kanal o'rnatildi: {channel}")
        
    except Exception as e:
        logger.error(f"Kanal o'rnatishda xato: {e}")
        await update.message.reply_text("❌ Kanal o'rnatishda xato!")

async def enable_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Majburiy kanalni yoqish"""
    global CHANNEL_ENABLED
    
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    if not REQUIRED_CHANNEL:
        await update.message.reply_text("❌ Avval kanalni o'rnating: /setchannel @kanal_nomi")
        return
    
    CHANNEL_ENABLED = True
    await update.message.reply_text(f"✅ Majburiy kanal yoqildi: {REQUIRED_CHANNEL}")

async def disable_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Majburiy kanalni o'chirish"""
    global CHANNEL_ENABLED
    
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
        return
    
    CHANNEL_ENABLED = False
    await update.message.reply_text("❌ Majburiy kanal o'chirildi!")

def main():
    # Ma'lumotlar bazasini ishga tushirish
    init_database()
    
    # Bot ilovasi
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Muddatlarni tekshirish uchun job queue
    job_queue = application.job_queue
    
    # Har daqiqada muddatlarni tekshirish
    job_queue.run_repeating(check_and_notify_expiries, interval=60, first=10)
    
    # Handlerlar
    application.add_handler(CommandHandler("start", start))
    
    # Admin buyruqlari
    application.add_handler(CommandHandler("gift", gift_pro))
    application.add_handler(CommandHandler("removepro", remove_pro_command))
    application.add_handler(CommandHandler("block", block_user_command))
    application.add_handler(CommandHandler("unblock", unblock_user_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("setchannel", set_channel_command))
    application.add_handler(CommandHandler("enablechannel", enable_channel_command))
    application.add_handler(CommandHandler("disablechannel", disable_channel_command))
    
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Botni ishga tushirish
    print("🤖 Dilshod AI bot ishga tushdi!")
    kanal_sozlamasi = "Yoqilgan" if CHANNEL_ENABLED else "O'chirilgan"
    print(f"📺 Kanal sozlamalari: {kanal_sozlamasi}")
    kanal_nomi = REQUIRED_CHANNEL if REQUIRED_CHANNEL else "Belgilanmagan"
    print(f"📢 Majburiy kanal: {kanal_nomi}")
    application.run_polling()

if __name__ == '__main__':
    main()
