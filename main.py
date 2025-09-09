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
 
]
TELEGRAM_TOKEN = ""
ADMIN_ID = 7445142075

# Majburiy kanal
REQUIRED_CHANNEL = "@sayfiddinov22"
CHANNEL_ENABLED = False  # True qilib qo'ysangiz kanal majburiy bo'ladi

# Gemini konfiguratsiyasi
current_api_index = 0

def configure_gemini():
    """Gemini API ni sozlash"""
    try:
        genai.configure(api_key=API_KEYS[current_api_index])
        return True
    except Exception as e:
        logger.error(f"Gemini API sozlashda xato: {e}")
        return False

# Boshlangich API sozlash
configure_gemini()

# JSON fayl nomlari
CHATS_FILE = "user_chats.json"
BLOCKS_FILE = "blocked_users.json"

# JSON fayllarni yuklash va saqlash
def load_json_file(filename):
    """JSON faylni xavfsiz yuklash"""
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"{filename} fayli buzuq, yangi fayl yaratilmoqda")
            return {}
        except Exception as e:
            logger.error(f"{filename} faylini yuklashda xato: {e}")
            return {}
    return {}

def save_json_file(filename, data):
    """JSON faylni xavfsiz saqlash"""
    try:
        # Faylni temp fayl sifatida yozish va keyin ko'chirish
        temp_filename = filename + '.tmp'
        with open(temp_filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # Temp faylni asosiy faylga ko'chirish
        if os.path.exists(temp_filename):
            os.replace(temp_filename, filename)
    except Exception as e:
        logger.error(f"JSON saqlashda xato {filename}: {e}")
        # Temp faylni o'chirish
        if os.path.exists(filename + '.tmp'):
            try:
                os.remove(filename + '.tmp')
            except:
                pass

# Chat va block ma'lumotlarini yuklash
user_chats = load_json_file(CHATS_FILE)
blocked_users = load_json_file(BLOCKS_FILE)

# Ma'lumotlar bazasi bilan ishlash
def get_db_connection():
    """Ma'lumotlar bazasiga ulanish"""
    try:
        conn = sqlite3.connect('dilshod_ai.db', timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"Ma'lumotlar bazasiga ulanishda xato: {e}")
        return None

def init_database():
    """Ma'lumotlar bazasini boshlangich sozlash"""
    conn = get_db_connection()
    if not conn:
        logger.error("Ma'lumotlar bazasini yaratib bo'lmadi!")
        return False
    
    try:
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
                daily_images INTEGER DEFAULT 0,
                last_reset DATE DEFAULT CURRENT_DATE,
                is_blocked BOOLEAN DEFAULT 0,
                block_expiry DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                id INTEGER PRIMARY KEY DEFAULT 1,
                channel_id TEXT,
                channel_name TEXT,
                is_enabled BOOLEAN DEFAULT 0
            )
        ''')
        
        conn.commit()
        logger.info("Ma'lumotlar bazasi muvaffaqiyatli yaratildi/yangilandi")
        return True
        
    except Exception as e:
        logger.error(f"Ma'lumotlar bazasini yaratishda xato: {e}")
        return False
    finally:
        conn.close()

def get_user_info(user_id):
    """Foydalanuvchi ma'lumotlarini olish"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        return dict(user) if user else None
    except Exception as e:
        logger.error(f"Foydalanuvchi ma'lumotlarini olishda xato {user_id}: {e}")
        return None
    finally:
        conn.close()

def add_user(user_id, username, first_name, last_name):
    """Foydalanuvchini bazaga qo'shish yoki yangilash"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        current_date = datetime.now().date().isoformat()
        
        # Foydalanuvchi mavjudligini tekshirish
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        existing_user = cursor.fetchone()
        
        if existing_user:
            # Mavjud foydalanuvchi ma'lumotlarini yangilash
            cursor.execute('''
                UPDATE users SET 
                    username = ?, 
                    first_name = ?, 
                    last_name = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (username, first_name, last_name, user_id))
        else:
            # Yangi foydalanuvchi qo'shish
            cursor.execute('''
                INSERT INTO users (
                    user_id, username, first_name, last_name, 
                    last_reset, daily_questions, daily_images,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ''', (user_id, username, first_name, last_name, current_date))
        
        conn.commit()
        logger.info(f"Foydalanuvchi {'yangilandi' if existing_user else 'qoshildi'}: {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Foydalanuvchini qo'shishda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

def is_user_blocked(user_id):
    """Foydalanuvchi bloklangan yoki yo'qligini tekshirish"""
    user_id_str = str(user_id)
    
    if user_id_str in blocked_users:
        block_info = blocked_users[user_id_str]
        
        if block_info.get('permanent', False):
            return True, "permanent"
        
        try:
            expiry_date = datetime.strptime(block_info['expiry'], '%Y-%m-%d').date()
            if expiry_date >= datetime.now().date():
                return True, expiry_date
            else:
                # Muddati tugagan blokni o'chirish
                del blocked_users[user_id_str]
                save_json_file(BLOCKS_FILE, blocked_users)
                return False, None
        except (KeyError, ValueError) as e:
            logger.error(f"Blok ma'lumotlarini o'qishda xato {user_id}: {e}")
            # Noto'g'ri ma'lumotni o'chirish
            del blocked_users[user_id_str]
            save_json_file(BLOCKS_FILE, blocked_users)
            return False, None
    
    return False, None

def block_user(user_id, days=None):
    """Foydalanuvchini bloklash"""
    user_id_str = str(user_id)
    current_datetime = datetime.now()
    
    if days is None:
        blocked_users[user_id_str] = {
            "permanent": True, 
            "blocked_at": current_datetime.isoformat()
        }
    else:
        expiry_date = current_datetime + timedelta(days=days)
        blocked_users[user_id_str] = {
            "expiry": expiry_date.date().isoformat(),
            "blocked_at": current_datetime.isoformat(),
            "permanent": False
        }
    
    save_json_file(BLOCKS_FILE, blocked_users)
    logger.info(f"Foydalanuvchi bloklandi {user_id}: {'permanent' if days is None else f'{days} kun'}")

def unblock_user(user_id):
    """Foydalanuvchini blokdan chiqarish"""
    user_id_str = str(user_id)
    if user_id_str in blocked_users:
        del blocked_users[user_id_str]
        save_json_file(BLOCKS_FILE, blocked_users)
        logger.info(f"Foydalanuvchi blokdan chiqarildi: {user_id}")
        return True
    return False

async def check_channel_membership(bot, user_id):
    """Kanalga a'zolikni tekshirish"""
    global CHANNEL_ENABLED, REQUIRED_CHANNEL
    
    if not CHANNEL_ENABLED or not REQUIRED_CHANNEL:
        return True
    
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        is_member = member.status in ['member', 'administrator', 'creator']
        logger.info(f"Kanal a'zoligi tekshirildi {user_id}: {is_member}")
        return is_member
    except Exception as e:
        logger.error(f"Kanal a'zoligini tekshirishda xato {user_id}: {e}")
        return True  # Xato bo'lsa, kirish ruxsati berish

def reset_daily_counters_if_needed(user_id):
    """Agar yangi kun boshlangan bo'lsa, kunlik hisoblagichlarni qayta o'rnatish"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        today = datetime.now().date().isoformat()
        
        cursor.execute('SELECT last_reset FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            last_reset = result[0]
            if last_reset != today:
                cursor.execute('''
                    UPDATE users SET 
                        daily_questions = 0,
                        daily_images = 0,
                        last_reset = ?
                    WHERE user_id = ?
                ''', (today, user_id))
                conn.commit()
                logger.info(f"Kunlik hisoblagichlar qayta o'rnatildi: {user_id}")
        
        return True
        
    except Exception as e:
        logger.error(f"Kunlik hisoblagichlarni qayta o'rnatishda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

def update_daily_questions(user_id):
    """Kunlik savol hisoblagichini yangilash"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        reset_daily_counters_if_needed(user_id)
        
        cursor.execute('''
            UPDATE users SET 
                daily_questions = daily_questions + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (user_id,))
        
        conn.commit()
        return True
        
    except Exception as e:
        logger.error(f"Kunlik savollarni yangilashda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

def update_daily_images(user_id):
    """Kunlik rasm hisoblagichini yangilash"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        reset_daily_counters_if_needed(user_id)
        
        cursor.execute('''
            UPDATE users SET 
                daily_images = daily_images + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (user_id,))
        
        conn.commit()
        return True
        
    except Exception as e:
        logger.error(f"Kunlik rasmlarni yangilashda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

def get_daily_questions_count(user_id):
    """Kunlik savol sonini olish"""
    conn = get_db_connection()
    if not conn:
        return 0
    
    try:
        cursor = conn.cursor()
        reset_daily_counters_if_needed(user_id)
        
        cursor.execute('SELECT daily_questions FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        return result[0] if result else 0
        
    except Exception as e:
        logger.error(f"Kunlik savollar sonini olishda xato {user_id}: {e}")
        return 0
    finally:
        conn.close()

def get_daily_images_count(user_id):
    """Kunlik rasm sonini olish"""
    conn = get_db_connection()
    if not conn:
        return 0
    
    try:
        cursor = conn.cursor()
        reset_daily_counters_if_needed(user_id)
        
        cursor.execute('SELECT daily_images FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        return result[0] if result else 0
        
    except Exception as e:
        logger.error(f"Kunlik rasmlar sonini olishda xato {user_id}: {e}")
        return 0
    finally:
        conn.close()

def is_pro_user(user_id):
    """Pro foydalanuvchi ekanligini tekshirish"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT is_pro, pro_expiry FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result and result[0]:  # is_pro = True
            pro_expiry = result[1]
            if pro_expiry:
                try:
                    if isinstance(pro_expiry, str):
                        expiry_date = datetime.strptime(pro_expiry, '%Y-%m-%d').date()
                    else:
                        expiry_date = pro_expiry
                    
                    if expiry_date >= datetime.now().date():
                        return True
                    else:
                        # Pro muddati tugagan - o'chirish
                        cursor.execute('''
                            UPDATE users SET 
                                is_pro = 0, 
                                pro_expiry = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = ?
                        ''', (user_id,))
                        conn.commit()
                        return False
                except (ValueError, TypeError):
                    return True  # Agar sana noto'g'ri bo'lsa, pro deb hisoblash
            else:
                return True  # Muddatsiz pro
        
        return False
        
    except Exception as e:
        logger.error(f"Pro holatini tekshirishda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

def give_pro_access(user_id, days=30):
    """Pro ruxsat berish"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        
        if days >= 365000:  # Doimiy pro
            cursor.execute('''
                UPDATE users SET 
                    is_pro = 1, 
                    pro_expiry = '2099-12-31',
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (user_id,))
        else:
            expiry_date = datetime.now() + timedelta(days=days)
            cursor.execute('''
                UPDATE users SET 
                    is_pro = 1, 
                    pro_expiry = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (expiry_date.date().isoformat(), user_id))
        
        conn.commit()
        logger.info(f"Pro ruxsat berildi {user_id}: {days} kun")
        return True
        
    except Exception as e:
        logger.error(f"Pro ruxsat berishda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

def remove_pro_access(user_id):
    """Pro ruxsatni olib tashlash"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET 
                is_pro = 0, 
                pro_expiry = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (user_id,))
        
        conn.commit()
        logger.info(f"Pro ruxsat olib tashlandi: {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Pro ruxsatni olib tashashda xato {user_id}: {e}")
        return False
    finally:
        conn.close()

async def check_and_notify_expiries(context: ContextTypes.DEFAULT_TYPE):
    """Pro obuna va blok muddatlarini tekshirish"""
    try:
        current_date = datetime.now().date()
        current_time, _ = get_current_time()
        
        # Pro obuna muddatlarini tekshirish
        conn = get_db_connection()
        if not conn:
            return
        
        try:
            cursor = conn.cursor()
            
            # Muddati tugagan pro foydalanuvchilarni topish
            cursor.execute('''
                SELECT user_id, first_name, pro_expiry 
                FROM users 
                WHERE is_pro = 1 AND pro_expiry IS NOT NULL AND pro_expiry <= ? AND pro_expiry != '2099-12-31'
            ''', (current_date.isoformat(),))
            
            expired_pro_users = cursor.fetchall()
            
            for user in expired_pro_users:
                user_id, first_name, pro_expiry = user[0], user[1], user[2]
                
                # Pro obunani o'chirish
                cursor.execute('''
                    UPDATE users SET 
                        is_pro = 0, 
                        pro_expiry = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                ''', (user_id,))
                
                # Foydalanuvchiga xabar yuborish
                try:
                    pro_tugadi_xabari = f"""⏰ Pro obuna muddati tugadi!

Hurmatli {first_name or 'foydalanuvchi'}, sizning Pro obunangiz muddati tugadi.

Endi siz:
❌ Kuniga faqat 3 ta savol bera olasiz
❌ Kuniga faqat 1 ta rasm yuklay olasiz

🔄 Qayta Pro obuna olish uchun admin bilan bog'laning:
👨‍💻 @dilshod_sayfiddinov

🕐 Tugagan vaqt: {current_time}"""
                    
                    await context.bot.send_message(user_id, pro_tugadi_xabari)
                    logger.info(f"Pro muddati tugagan foydalanuvchiga xabar yuborildi: {user_id}")
                except Exception as e:
                    logger.error(f"Pro tugash xabarini yuborishda xato {user_id}: {e}")
            
            conn.commit()
            
            if expired_pro_users:
                logger.info(f"{len(expired_pro_users)} ta pro obuna muddati tugadi")
                
        finally:
            conn.close()
        
        # Blok muddatlarini tekshirish
        expired_blocks = []
        for user_id_str, block_info in list(blocked_users.items()):
            if not block_info.get('permanent', False):
                try:
                    expiry_date = datetime.strptime(block_info['expiry'], '%Y-%m-%d').date()
                    if expiry_date <= current_date:
                        expired_blocks.append(user_id_str)
                except (KeyError, ValueError):
                    expired_blocks.append(user_id_str)  # Noto'g'ri ma'lumotni o'chirish
        
        # Muddati tugagan bloklarni o'chirish
        for user_id_str in expired_blocks:
            user_id = int(user_id_str)
            del blocked_users[user_id_str]
            
            # Foydalanuvchiga xabar yuborish
            try:
                user_info = get_user_info(user_id)
                first_name = user_info['first_name'] if user_info else 'foydalanuvchi'
                
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
            
    except Exception as e:
        logger.error(f"Muddatlarni tekshirishda xato: {e}")

def save_question(user_id, question, answer, has_image=False):
    """Savolni saqlash"""
    # Ma'lumotlar bazasiga saqlash
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO questions (user_id, question, answer, has_image)
                VALUES (?, ?, ?, ?)
            ''', (user_id, question, answer, has_image))
            conn.commit()
        except Exception as e:
            logger.error(f"Savolni ma'lumotlar bazasiga saqlashda xato {user_id}: {e}")
        finally:
            conn.close()
    
    # JSON faylga saqlash
    try:
        user_id_str = str(user_id)
        if user_id_str not in user_chats:
            user_chats[user_id_str] = []
        
        user_chats[user_id_str].append({
            "question": question,
            "answer": answer,
            "has_image": has_image,
            "timestamp": datetime.now().isoformat()
        })
        
        # Faqat oxirgi 50 ta suhbatni saqlash
        if len(user_chats[user_id_str]) > 50:
            user_chats[user_id_str] = user_chats[user_id_str][-50:]
        
        save_json_file(CHATS_FILE, user_chats)
        
    except Exception as e:
        logger.error(f"Savolni JSON ga saqlashda xato {user_id}: {e}")

def get_conversation_history(user_id, limit=10):
    """Suhbat tarixini olish"""
    try:
        user_id_str = str(user_id)
        if user_id_str in user_chats:
            return user_chats[user_id_str][-limit:]
        return []
    except Exception as e:
        logger.error(f"Suhbat tarixini olishda xato {user_id}: {e}")
        return []

def get_all_users():
    """Barcha foydalanuvchilarni olish"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, username, first_name, last_name, is_pro FROM users ORDER BY created_at DESC')
        users = cursor.fetchall()
        return [dict(user) for user in users]
    except Exception as e:
        logger.error(f"Foydalanuvchilar ro'yxatini olishda xato: {e}")
        return []
    finally:
        conn.close()

def get_current_time():
    """Toshkent vaqtini olish"""
    try:
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
    except Exception as e:
        logger.error(f"Vaqt olishda xato: {e}")
        return "Noma'lum vaqt", datetime.now()

async def get_gemini_response(text, image=None, is_pro=False, user_id=None):
    """Gemini dan javob olish"""
    global current_api_index
    
    max_retries = len(API_KEYS)
    
    for retry in range(max_retries):
        try:
            if not configure_gemini():
                current_api_index = (current_api_index + 1) % len(API_KEYS)
                continue
            
            model = genai.GenerativeModel('gemini-2.0-flash-exp')
            
            # Suhbat tarixini olish
            conversation_context = ""
            if user_id:
                try:
                    history = get_conversation_history(user_id, 10)
                    if history:
                        conversation_context = "\n\nOxirgi suhbatlarimiz:\n"
                        for i, chat in enumerate(history, 1):
                            conversation_context += f"{i}. Savol: {chat['question']}\n   Javob: {chat['answer']}\n\n"
                except Exception as e:
                    logger.error(f"Suhbat tarixini olishda xato: {e}")
            
            # Vaqt ma'lumotini qo'shish
            time_info = ""
            if any(word in text.lower() for word in ['vaqt', 'soat', 'kun', 'sana', 'bugun', 'hozir']):
                try:
                    current_time, _ = get_current_time()
                    time_info = f"\n\nHozirgi vaqt: {current_time}"
                except Exception as e:
                    logger.error(f"Vaqt ma'lumotini olishda xato: {e}")
            
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
6. Agar  foydalanuvchi sizga rasm tashlasa va unda matematik masalallar yoki misolar bo'lssa yechib bering yoki shunchaki uy vazifasi bo'lsa ham uni bajaring shunchaki savol bo'lsa javob yozing rasmda savol misol masala bo'lsa birdaniga qilib ber qaysini yechay yo qaysisi deb o'tirma birdaniga yech rasmdagi misolni yoki masalani va senga matn tashlansa va inglizcha bo'lsa yoki ruscha uni tarjima qilish aytilsa o'zbekcha qilib aar unda uy ishi borligini sezsang u ishini misollarni yechib ber foydalanuvchi rasm tashlasa savol berma undagi vazifani qilib ber
Har doim o'zbek tilida (lotin alifbosida) javob bering. 
{yaratuvchi_text}

{conversation_context}"""
            elif is_pro:
                system_prompt = f"""Siz Dilshod AI - eng zo'r va aqlli yordamchi botsiz. Siz Pro foydalanuvchi bilan gaplashyapsiz, shuning uchun eng sifatli, batafsil va foydali javob bering. Har doim o'zbek tilida (lotin alifbosida) javob bering. Javoblaringiz professional, tushunarli va qiziqarli bo'lsin.

Yaratuvchi haqida so'ralsa "Dilshod Sayfiddinov" deb javob bering.
Agar foydalanuvchi oldingi suhbatlarimizga murojaat qilsa, oxirgi suhbatlarimizdan foydalaning.
O'zingizni faqat Dilshod AI deb tanishtiring va sizdi Dishod Sayfiddinov yasagan , Gemini yoki Google haqida gapirmang.
Agar  foydalanuvchi sizga rasm tashlasa va unda matematik masalallar yoki misolar bo'lssa yechib bering yoki shunchaki uy vazifasi bo'lsa ham uni bajaring shunchaki savol bo'lsa javob yozing rasmda savol misol masala bo'lsa birdaniga qilib ber qaysini yechay yo qaysisi deb o'tirma birdaniga yech rasmdagi misolni yoki masalani va senga matn tashlansa va inglizcha bo'lsa yoki ruscha uni tarjima qilish aytilsa o'zbekcha qilib aar unda uy ishi borligini sezsang u ishini misollarni yechib ber foydalanuvchi rasm tashlasa savol berma undagi vazifani qilib ber

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
            
            if response.text:
                return response.text
            else:
                raise Exception("Bo'sh javob qaytdi")
            
        except Exception as e:
            logger.error(f"Gemini API xatosi (urinish {retry + 1}/{max_retries}): {e}")
            current_api_index = (current_api_index + 1) % len(API_KEYS)
            
            if retry == max_retries - 1:
                return "Uzr, hozir javob berishda qiyinchilik bor. Iltimos, keyinroq qayta urinib ko'ring."
            
            await asyncio.sleep(1)  # Qisqa kutish
    
    return "Xizmat vaqtincha ishlamayapti. Keyinroq urinib ko'ring."

def get_admin_keyboard():
    """Admin klaviaturasi"""
    keyboard = [
        [KeyboardButton("👥 Foydalanuvchilar"), KeyboardButton("📊 Statistika")],
        [KeyboardButton("🎁 Pro berish"), KeyboardButton("❌ Pro o'chirish")],
        [KeyboardButton("🚫 Bloklash"), KeyboardButton("✅ Blokdan chiqarish")],
        [KeyboardButton("📢 Xabar yuborish"), KeyboardButton("📺 Kanal sozlamalari")],
        [KeyboardButton("🔙 Oddiy foydalanuvchi")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_user_keyboard():
    """Foydalanuvchi klaviaturasi"""
    keyboard = [
        [KeyboardButton("ℹ️ Ma'lumot"), KeyboardButton("⭐ Pro bo'lish")],
        [KeyboardButton("📞 Aloqa")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start komandasi"""
    try:
        user = update.effective_user
        
        # Foydalanuvchini bazaga qo'shish
        success = add_user(user.id, user.username, user.first_name, user.last_name)
        if not success:
            await update.message.reply_text("Xizmat vaqtincha ishlamayapti. Keyinroq urinib ko'ring.")
            return
        
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
{'🌟 Pro foydalanuvchi (cheksiz)' if is_pro_user(user.id) else '👤 Oddiy foydalanuvchi (kuniga 3 ta savol, 1 ta rasm)'}

🕐 Hozirgi vaqt: {current_time}
👨‍💻 Yaratuvchi: Dilshod Sayfiddinov

Menga savolingizni yuboring!"""
        
        if user.id == ADMIN_ID:
            reply_markup = get_admin_keyboard()
        else:
            reply_markup = get_user_keyboard()
        
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Start komandasi xatosi: {e}")
        await update.message.reply_text("Xizmat ishga tushirishda xatolik. Keyinroq urinib ko'ring.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xabarlarni qayta ishlash"""
    try:
        user = update.effective_user
        text = update.message.text
        
        # Foydalanuvchini bazaga qo'shish
        success = add_user(user.id, user.username, user.first_name, user.last_name)
        if not success:
            await update.message.reply_text("Xizmat vaqtincha ishlamayapti. Keyinroq urinib ko'ring.")
            return
        
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
            try:
                current_time, _ = get_current_time()
                
                # Foydalanuvchi ma'lumotlarini olish
                user_info = get_user_info(user.id)
                if not user_info:
                    await update.message.reply_text("Ma'lumotlaringizni yuklab bo'lmadi. Qaytadan /start bosing.")
                    return
                
                # Pro foydalanuvchi ekanligini tekshirish
                is_pro = is_pro_user(user.id)
                
                if is_pro:
                    status_text = '🌟 Pro foydalanuvchi'
                    questions_text = '✅ Cheksiz savollar (Pro)'
                    images_text = '✅ Cheksiz rasmlar (Pro)'
                else:
                    daily_questions = get_daily_questions_count(user.id)
                    daily_images = get_daily_images_count(user.id)
                    status_text = f'👤 Oddiy foydalanuvchi\n📝 Bugungi savollar: {daily_questions}/3\n📷 Bugungi rasmlar: {daily_images}/1'
                    questions_text = '✅ Kuniga 3 ta savol'
                    images_text = '✅ Kuniga 1 ta rasm'
                
                info_text = f"""ℹ️ **Bot haqida ma'lumot:**

🤖 **Bot nomi:** Dilshod AI
👨‍💻 **Yaratuvchi:** Dilshod Sayfiddinov
📅 **Yaratilgan sana:** 2025 yil
🔄 **Oxirgi yangilanish:** {current_time}

📊 **Sizning holatingiz:**
{status_text}

🎯 **Imkoniyatlar:**
{questions_text}
{images_text}
✅ Uy vazifalari yechimi
✅ Suhbat tarixini eslab qolish"""
                
                await update.message.reply_text(info_text)
                
            except Exception as e:
                logger.error(f"Ma'lumot tugmasida xato {user.id}: {e}")
                await update.message.reply_text("Ma'lumotlarni yuklashda xatolik yuz berdi. Qaytadan urinib ko'ring.")
            return
            
        elif text == "⭐ Pro bo'lish":
            await update.message.reply_text(
                "⭐ Pro versiya imkoniyatlari:\n\n"
                "✅ Cheksiz savollar\n"
                "✅ Cheksiz rasmlarni yuklash va tahlil qilish\n"
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
            if daily_count >= 3:
                await update.message.reply_text(
                    "❌ Siz bugun 3 ta savolni allaqachon so'ragansiz.\n\n"
                    "Pro versiyaga o'tib, cheksiz savol berish imkoniyatiga ega bo'ling!\n\n"
                    "👨‍💻 Aloqa: @dilshod_sayfiddinov"
                )
                return
        
        # Savolni qayta ishlash
        await update.message.reply_text("🤔 O'ylayapman...")
        
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
    """Rasmlarni qayta ishlash"""
    try:
        user = update.effective_user
        
        # Foydalanuvchini bazaga qo'shish
        success = add_user(user.id, user.username, user.first_name, user.last_name)
        if not success:
            await update.message.reply_text("Xizmat vaqtincha ishlamayapti. Keyinroq urinib ko'ring.")
            return
        
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
        
        # Rasm limitini tekshirish (Pro emas va admin emas bo'lsa)
        if not is_pro_user(user.id) and user.id != ADMIN_ID:
            daily_images = get_daily_images_count(user.id)
            if daily_images >= 1:
                await update.message.reply_text(
                    "📸 Siz bugun 1 ta rasmni allaqachon yuklabsiz!\n\n"
                    "Pro versiyaga o'tib, cheksiz rasm yuklash imkoniyatiga ega bo'ling!\n\n"
                    "👨‍💻 Pro bo'lish uchun: @dilshod_sayfiddinov"
                )
                return
        
        await update.message.reply_text("📷 Rasmni tahlil qilayapman...")
        
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
        
        # Rasm hisoblagichini yangilash
        if not is_pro and user.id != ADMIN_ID:
            update_daily_images(user.id)
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Rasm qayta ishlashda xato: {e}")
        await update.message.reply_text("Rasmni qaytadan yuboring.")

async def show_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    """Foydalanuvchilar ro'yxati"""
    try:
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
            user_id = user['user_id']
            username = user['username'] 
            first_name = user['first_name']
            last_name = user['last_name']
            is_pro = user['is_pro']
            
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
            
    except Exception as e:
        logger.error(f"Foydalanuvchilar ro'yxatini ko'rsatishda xato: {e}")
        error_text = "Foydalanuvchilar ro'yxatini yuklashda xatolik."
        if update.callback_query:
            await update.callback_query.edit_message_text(error_text)
        else:
            await update.message.reply_text(error_text)

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistika"""
    try:
        users = get_all_users()
        total_users = len(users)
        pro_users = len([u for u in users if u['is_pro']])
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

🎯 Oddiy foydalanuvchi limitlari:
📝 Kunlik savollar: 3 ta
📷 Kunlik rasmlar: 1 ta

🕐 Ma'lumot vaqti: {current_time}
👨‍💻 Yaratuvchi: Dilshod Sayfiddinov"""
        
        if update.callback_query:
            keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.callback_query.edit_message_text(stats_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(stats_text)
            
    except Exception as e:
        logger.error(f"Statistika ko'rsatishda xato: {e}")
        error_text = "Statistikani yuklashda xatolik."
        if update.callback_query:
            await update.callback_query.edit_message_text(error_text)
        else:
            await update.message.reply_text(error_text)

async def show_channel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kanal sozlamalari"""
    try:
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
            
    except Exception as e:
        logger.error(f"Kanal sozlamalarini ko'rsatishda xato: {e}")
        error_text = "Kanal sozlamalarini yuklashda xatolik."
        if update.callback_query:
            await update.callback_query.edit_message_text(error_text)
        else:
            await update.message.reply_text(error_text)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback tugmalarini qayta ishlash"""
    try:
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
            
    except Exception as e:
        logger.error(f"Callback qayta ishlashda xato: {e}")
        try:
            await query.answer("Xatolik yuz berdi. Qaytadan urinib ko'ring.")
        except:
            pass

# Admin buyruqlari
async def gift_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pro berish buyrug'i"""
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
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
            success = give_pro_access(user_id, 365000)
            if success:
                await update.message.reply_text(f"🎁 {user_id} foydalanuvchiga doimiy Pro berildi!")
                
                try:
                    pro_xabari = f"""🎉 Tabriklaymiz! Sizga doimiy Pro versiya berildi!

Endi siz:
✅ Cheksiz savol bera olasiz
✅ Cheksiz rasmlarni yuklash va tahlil qila olasiz  
✅ Uy vazifalari uchun maxsus yordam olasiz

🕐 Berilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                    await context.bot.send_message(user_id, pro_xabari)
                except Exception as e:
                    logger.error(f"Pro berish xabarini yuborishda xato {user_id}: {e}")
            else:
                await update.message.reply_text("❌ Pro berishda xatolik yuz berdi!")
        else:
            days = int(days_arg)
            success = give_pro_access(user_id, days)
            if success:
                await update.message.reply_text(f"🎁 {user_id} foydalanuvchiga {days} kunlik Pro berildi!")
                
                try:
                    pro_xabari = f"""🎉 Tabriklaymiz! Sizga {days} kunlik Pro versiya berildi!

Endi siz:
✅ Cheksiz savol bera olasiz
✅ Cheksiz rasmlarni yuklash va tahlil qila olasiz
✅ Uy vazifalari uchun maxsus yordam olasiz

🕐 Berilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                    await context.bot.send_message(user_id, pro_xabari)
                except Exception as e:
                    logger.error(f"Pro berish xabarini yuborishda xato {user_id}: {e}")
            else:
                await update.message.reply_text("❌ Pro berishda xatolik yuz berdi!")
            
    except (IndexError, ValueError) as e:
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /gift 123456789 30")
    except Exception as e:
        logger.error(f"Gift pro buyrug'ida xato: {e}")
        await update.message.reply_text("❌ Buyruqni bajarishda xatolik!")

async def remove_pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pro o'chirish buyrug'i"""
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
        user_id = int(context.args[0])
        success = remove_pro_access(user_id)
        
        if success:
            current_time, _ = get_current_time()
            await update.message.reply_text(f"❌ {user_id} foydalanuvchidan Pro o'chirildi!")
            
            try:
                pro_ochirildi_xabari = f"""⚠️ Sizning Pro obunangiz bekor qilindi.

Endi siz kuniga 3 ta savol va 1 ta rasm yuklay olasiz.

🕐 O'chirilgan vaqt: {current_time}
👨‍💻 Admin: Dilshod Sayfiddinov"""
                await context.bot.send_message(user_id, pro_ochirildi_xabari)
            except Exception as e:
                logger.error(f"Pro o'chirish xabarini yuborishda xato {user_id}: {e}")
        else:
            await update.message.reply_text("❌ Pro o'chirishda xatolik yuz berdi!")
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /removepro 123456789")
    except Exception as e:
        logger.error(f"Remove pro buyrug'ida xato: {e}")
        await update.message.reply_text("❌ Buyruqni bajarishda xatolik!")

async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchini bloklash buyrug'i"""
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
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
            except Exception as e:
                logger.error(f"Blok xabarini yuborishda xato {user_id}: {e}")
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
            except Exception as e:
                logger.error(f"Blok xabarini yuborishda xato {user_id}: {e}")
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /block 123456789 7")
    except Exception as e:
        logger.error(f"Block buyrug'ida xato: {e}")
        await update.message.reply_text("❌ Buyruqni bajarishda xatolik!")

async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchini blokdan chiqarish buyrug'i"""
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
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
            except Exception as e:
                logger.error(f"Blokdan chiqarish xabarini yuborishda xato {user_id}: {e}")
        else:
            await update.message.reply_text(f"❌ {user_id} foydalanuvchi bloklanmagan!")
            
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Noto'g'ri format. Misol: /unblock 123456789")
    except Exception as e:
        logger.error(f"Unblock buyrug'ida xato: {e}")
        await update.message.reply_text("❌ Buyruqni bajarishda xatolik!")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xabar yuborish buyrug'i"""
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
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
            target_users = [u for u in users if u['is_pro']]  # is_pro = True
        elif broadcast_type == "regular":
            target_users = [u for u in users if not u['is_pro']]  # is_pro = False
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
            user_id = user['user_id']
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
    try:
        global REQUIRED_CHANNEL
        
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
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
    try:
        global CHANNEL_ENABLED
        
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
        if not REQUIRED_CHANNEL:
            await update.message.reply_text("❌ Avval kanalni o'rnating: /setchannel @kanal_nomi")
            return
        
        CHANNEL_ENABLED = True
        await update.message.reply_text(f"✅ Majburiy kanal yoqildi: {REQUIRED_CHANNEL}")
        
    except Exception as e:
        logger.error(f"Kanalni yoqishda xato: {e}")
        await update.message.reply_text("❌ Kanalni yoqishda xato!")

async def disable_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Majburiy kanalni o'chirish"""
    try:
        global CHANNEL_ENABLED
        
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Bu buyruq faqat admin uchun!")
            return
        
        CHANNEL_ENABLED = False
        await update.message.reply_text("❌ Majburiy kanal o'chirildi!")
        
    except Exception as e:
        logger.error(f"Kanalni o'chirishda xato: {e}")
        await update.message.reply_text("❌ Kanalni o'chirishda xato!")

def main():
    """Asosiy funksiya"""
    try:
        # Ma'lumotlar bazasini ishga tushirish
        if not init_database():
            logger.error("Ma'lumotlar bazasini yaratib bo'lmadi. Dastur to'xtatilmoqda.")
            return
        
        # Bot ilovasi
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        
        # Muddatlarni tekshirish uchun job queue
        job_queue = application.job_queue
        
        # Har daqiqada muddatlarni tekshirish
        if job_queue:
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
        print("📊 Yangi limitlar:")
        print("   👤 Oddiy foydalanuvchilar: 3 ta savol, 1 ta rasm")
        print("   ⭐ Pro foydalanuvchilar: Cheksiz")
        print("✅ Bot muvaffaqiyatli ishga tushdi!")
        
        application.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Botni ishga tushirishda fatal xato: {e}")
        print(f"❌ Botni ishga tushirishda xato: {e}")

if __name__ == '__main__':
    main()
