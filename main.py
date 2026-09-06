import logging
import time
import asyncio
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, InlineKeyboardMarkup,
    InlineKeyboardButton, InputMediaPhoto,
    KeyboardButton, ReplyKeyboardRemove
)

# ================= НАСТРОЙКИ =================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан! Добавьте его в Environment Variables на Render")

CHANNEL_USERNAME = "@blackrussia_85"
CHANNEL_LINK = "https://t.me/blackrussia_85"
BOT_USERNAME = "blackrussia85_bot"

OWNER_ID = 724545647  # Ваш ID

MODERATORS = [
    724545647,  # Только вы — владелец и модератор
]

MAX_PHOTOS = 5

# Текст подписи в конце каждого объявления (кликабельный)
SUBSCRIPTION_TEXT = f"\n\n📢 <b>Подпишись на канал:</b> <a href='{CHANNEL_LINK}'>Б/У рынок IZHEVSK</a>"

# ===== ЕДИНОЕ КД ДЛЯ ВСЕХ =====
COOLDOWN_SECONDS = 2 * 60 * 60  # 2 часа (7200 секунд)

# ================= ПУТИ К БАЗЕ ДАННЫХ (ИСПРАВЛЕНО) =================

DATA_DIR = os.path.join(os.getcwd(), "data")
DB_PATH = os.path.join(DATA_DIR, "database.db")
os.makedirs(DATA_DIR, exist_ok=True)

# ================= ПИНГ-СЕРВЕР =================

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ping':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

def start_ping_server():
    server = HTTPServer(('0.0.0.0', 8080), PingHandler)
    print("✅ Пинг-сервер запущен на порту 8080")
    server.serve_forever()

threading.Thread(target=start_ping_server, daemon=True).start()

# ================= INIT =================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ================= DATABASE =================

def init_db():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                last_ad_time INTEGER DEFAULT 0
            )
            """)
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
            """)
            
            conn.commit()
            logging.info(f"База данных инициализирована: {DB_PATH}")
    except Exception as e:
        logging.error(f"Ошибка инициализации БД: {e}")

init_db()

# ================= FSM =================

class AdForm(StatesGroup):
    text = State()
    ask_photo = State()
    photos = State()
    confirm = State()

# ================= STORAGE =================

pending_ads = {}
processed_ads = set()

# ================= КЛАВИАТУРЫ =================

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Опубликовать объявление")],
            [KeyboardButton(text="📖 Помощь"), KeyboardButton(text="📞 Связь с владельцем")],
            [KeyboardButton(text="👮 Модераторы")]
        ],
        resize_keyboard=True
    )
    return keyboard

main_kb = get_main_keyboard()

def get_subscribe_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    ])
    return keyboard

subscribe_kb = get_subscribe_keyboard()

ask_photo_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить фото"), KeyboardButton(text="➡️ Без фото")]
    ],
    resize_keyboard=True
)

photo_done_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Готово")]
    ],
    resize_keyboard=True
)

def get_confirm_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")
        ]
    ])
    return keyboard

confirm_kb = get_confirm_keyboard()

def get_moderation_keyboard(ad_id):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{ad_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{ad_id}")
        ]
    ])
    return keyboard

# ================= УТИЛИТЫ =================

def get_cursor():
    conn = sqlite3.connect(DB_PATH)
    return conn, conn.cursor()

def format_time(seconds):
    if seconds < 60:
        return f"{seconds} сек"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} мин"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}ч {minutes}м" if minutes > 0 else f"{hours}ч"

def can_post(user_id, last_ad_time):
    """Проверка, может ли пользователь опубликовать объявление"""
    now = int(time.time())
    
    if last_ad_time == 0:
        return True, 0
    
    time_passed = now - last_ad_time
    
    if time_passed >= COOLDOWN_SECONDS:
        return True, 0
    else:
        remaining = COOLDOWN_SECONDS - time_passed
        return False, remaining

async def check_subscription(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        return False

def add_subscription_text(text):
    return text + SUBSCRIPTION_TEXT

# ================= START =================

@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
    
    conn.close()
    
    if not await check_subscription(user_id):
        await message.answer(
            "❌ Для использования бота подпишитесь на канал:",
            reply_markup=subscribe_kb
        )
        return
    
    await message.answer(
        "👋 Добро пожаловать в бот Б/У рынка IZHEVSK!\n\n"
        "Здесь вы можете публиковать свои объявления.\n"
        f"⏱ КД между публикациями: {format_time(COOLDOWN_SECONDS)}",
        reply_markup=main_kb
    )

@dp.callback_query(lambda c: c.data == "check_sub")
async def check_sub_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    
    if not await check_subscription(user_id):
        await call.answer("❌ Вы ещё не подписались на канал!", show_alert=True)
        return
    
    await call.message.delete()
    await call.message.answer(
        "✅ Подписка подтверждена! Теперь вы можете пользоваться ботом.",
        reply_markup=main_kb
    )

# ================= ИНФО =================

@dp.message(lambda m: m.text == "📖 Помощь")
async def help_command(message: types.Message):
    await message.answer(
        "📌 <b>Как подать объявление</b>\n\n"
        "1️⃣ Нажмите кнопку «Опубликовать объявление»\n"
        "2️⃣ Отправьте текст объявления\n"
        "3️⃣ Добавьте фото (до 5 штук, по желанию)\n"
        "4️⃣ Подтвердите отправку\n\n"
        "⚠️ <b>Важно:</b>\n"
        "• В тексте обязательно должен быть указан ваш @username\n"
        f"• КД между публикациями: {format_time(COOLDOWN_SECONDS)}\n"
        "• Объявления проходят модерацию\n\n"
        "❓ Дополнительные вопросы: @onesever",
        reply_markup=main_kb
    )

@dp.message(lambda m: m.text == "📞 Связь с владельцем")
async def owner_contact(message: types.Message):
    await message.answer(
        "👑 <b>Владелец бота:</b> @onesever\n\n"
        "По всем вопросам обращайтесь к нему.",
        reply_markup=main_kb
    )

@dp.message(lambda m: m.text == "👮 Модераторы")
async def moderators_list(message: types.Message):
    await message.answer(
        "👮 <b>Текущий модератор:</b>\n\n"
        "👑 @onesever - Владелец и модератор\n\n"
        "По всем вопросам обращайтесь к нему.",
        reply_markup=main_kb
    )

# ================= ПОДАЧА ОБЪЯВЛЕНИЯ =================

@dp.message(lambda m: m.text == "📢 Опубликовать объявление")
async def create_ad(message: types.Message):
    user_id = message.from_user.id
    
    if not await check_subscription(user_id):
        await message.answer(
            "❌ Для публикации объявлений нужно быть подписанным на канал.",
            reply_markup=subscribe_kb
        )
        return
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT last_ad_time FROM users WHERE user_id=?", (user_id,))
    result = cursor.fetchone()
    
    if not result:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        last_ad_time = 0
    else:
        last_ad_time = result[0]
    
    conn.close()
    
    can_post_now, remaining = can_post(user_id, last_ad_time)
    
    if not can_post_now:
        await message.answer(
            f"⏳ <b>КД активен!</b>\n\n"
            f"Осталось подождать: {format_time(remaining)}\n\n"
            f"Следующая публикация будет доступна через {format_time(remaining)}"
        )
        return
    
    await message.answer(
        f"✍️ <b>Введите текст объявления</b>\n\n"
        f"⏱ <b>КД после публикации:</b> {format_time(COOLDOWN_SECONDS)}\n\n"
        f"📌 <b>Пример оформления:</b>\n"
        f"Продам дом в Бусаево\n"
        f"Цена: 17кк\n"
        f"Связь: @username\n\n"
        f"⚠️ <b>Обязательно укажите ваш @username в тексте!</b>",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdForm.text)

@dp.message(AdForm.text)
async def process_ad_text(message: types.Message, state: FSMContext):
    if not message.from_user.username:
        await message.answer(
            "❌ У вас не установлен username в Telegram.\n"
            "Пожалуйста, установите его в настройках и попробуйте снова.",
            reply_markup=main_kb
        )
        await state.clear()
        return
    
    user_mention = f"@{message.from_user.username}"
    if user_mention.lower() not in message.text.lower():
        await message.answer(
            f"❌ В тексте обязательно должен быть указан ваш username: {user_mention}\n"
            f"Пожалуйста, добавьте его и отправьте текст снова."
        )
        return
    
    await state.update_data(text=message.text, photos=[])
    await message.answer(
        "Хотите добавить фото к объявлению?",
        reply_markup=ask_photo_kb
    )
    await state.set_state(AdForm.ask_photo)

@dp.message(AdForm.ask_photo, lambda m: m.text == "➕ Добавить фото")
async def add_photo_start(message: types.Message, state: FSMContext):
    await message.answer(
        f"📸 Отправьте до {MAX_PHOTOS} фото.\nПосле отправки всех фото нажмите «Готово».",
        reply_markup=photo_done_kb
    )
    await state.set_state(AdForm.photos)

@dp.message(AdForm.ask_photo, lambda m: m.text == "➡️ Без фото")
async def no_photo_confirm(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await show_preview(message, data, state)

@dp.message(AdForm.photos, lambda m: m.text == "✅ Готово")
async def photos_done(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await show_preview(message, data, state)

@dp.message(AdForm.photos, lambda m: m.photo is not None)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    
    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"❌ Нельзя добавить больше {MAX_PHOTOS} фото.")
        return
    
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    
    remaining = MAX_PHOTOS - len(photos)
    
    response = (
        f"✅ Фото добавлено! ({len(photos)}/{MAX_PHOTOS})\n"
        f"📸 Осталось мест: {remaining}\n\n"
        f"➡️ <b>Что делать дальше?</b>\n"
        f"• Отправьте ещё фото, если нужно\n"
        f"• Или нажмите кнопку <b>«✅ Готово»</b> внизу, чтобы закончить"
    )
    
    if remaining == 0:
        response = (
            f"✅ Все {MAX_PHOTOS} фото добавлены!\n\n"
            f"➡️ <b>Нажмите кнопку «✅ Готово»</b> внизу, чтобы перейти к предпросмотру объявления."
        )
    
    await message.answer(response)

async def show_preview(message: types.Message, data: dict, state: FSMContext):
    preview_text = f"🔍 <b>Предпросмотр объявления</b>\n\n{data['text']}"
    if data.get("photos"):
        preview_text += f"\n\n📸 Фото: {len(data['photos'])} шт."
    
    await message.answer(
        preview_text,
        reply_markup=confirm_kb
    )
    await state.set_state(AdForm.confirm)

@dp.callback_query(AdForm.confirm, lambda c: c.data == "cancel")
async def cancel_ad(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Подача объявления отменена.")
    await call.message.answer("Главное меню:", reply_markup=main_kb)
    await call.answer()

@dp.callback_query(AdForm.confirm, lambda c: c.data == "confirm")
async def confirm_ad(call: types.CallbackQuery, state: FSMContext):
    user = call.from_user
    data = await state.get_data()
    
    conn, cursor = get_cursor()
    
    cursor.execute("INSERT INTO ads (user_id) VALUES (?)", (user.id,))
    ad_id = cursor.lastrowid
    
    current_time = int(time.time())
    cursor.execute("UPDATE users SET last_ad_time = ? WHERE user_id = ?", (current_time, user.id))
    
    conn.commit()
    conn.close()
    
    pending_ads[ad_id] = data
    await state.clear()
    
    mod_text = (
        f"📢 <b>Новое объявление №{ad_id}</b>\n\n"
        f"👤 Автор: @{user.username}\n"
        f"🆔 ID: {user.id}\n"
        f"⏱ Время подачи: {datetime.fromtimestamp(current_time).strftime('%d.%m.%Y %H:%M')}\n\n"
        f"📝 Текст:\n{data['text']}"
    )
    
    if data.get("photos"):
        mod_text += f"\n\n📸 Фото: {len(data['photos'])} шт."
    
    sent_count = 0
    for mod_id in MODERATORS:
        try:
            if data.get("photos"):
                media_group = []
                for i, photo_id in enumerate(data["photos"]):
                    if i == 0:
                        media_group.append(InputMediaPhoto(photo_id, caption=mod_text))
                    else:
                        media_group.append(InputMediaPhoto(photo_id))
                
                await bot.send_media_group(mod_id, media_group)
                await bot.send_message(mod_id, "Действия:", reply_markup=get_moderation_keyboard(ad_id))
            else:
                await bot.send_message(mod_id, mod_text, reply_markup=get_moderation_keyboard(ad_id))
            
            sent_count += 1
        except Exception as e:
            logging.error(f"Не удалось отправить модератору {mod_id}: {e}")
    
    await call.message.edit_text(
        f"✅ <b>Объявление №{ad_id} отправлено на модерацию!</b>\n\n"
        f"⏱ Следующая публикация будет доступна через: {format_time(COOLDOWN_SECONDS)}\n"
        f"(отсчет пошел с момента подачи этого объявления)\n\n"
        f"Ожидайте проверки (обычно до 24 часов)."
    )
    await call.message.answer("Главное меню:", reply_markup=main_kb)
    await call.answer()
    
    logging.info(f"Объявление {ad_id} отправлено {sent_count} модераторам. КД для {user.id} обновлен.")

# ================= МОДЕРАЦИЯ =================

@dp.callback_query(lambda c: c.data and c.data.startswith("approve:"))
async def approve_ad(call: types.CallbackQuery):
    ad_id = int(call.data.split(":")[1])
    
    if ad_id in processed_ads:
        await call.answer("❌ Это объявление уже обработано!", show_alert=True)
        return
    
    processed_ads.add(ad_id)
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT user_id FROM ads WHERE id=?", (ad_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        await call.answer("❌ Объявление не найдено!", show_alert=True)
        return
    
    user_id = row[0]
    data = pending_ads.get(ad_id)
    
    if not data:
        conn.close()
        await call.answer("❌ Данные объявления утеряны!", show_alert=True)
        return
    
    final_text_with_sub = add_subscription_text(data['text'])
    
    try:
        if data.get("photos"):
            media_group = []
            for i, photo_id in enumerate(data["photos"]):
                if i == 0:
                    media_group.append(InputMediaPhoto(photo_id, caption=final_text_with_sub))
                else:
                    media_group.append(InputMediaPhoto(photo_id))
            
            await bot.send_media_group(CHANNEL_USERNAME, media_group)
        else:
            await bot.send_message(CHANNEL_USERNAME, final_text_with_sub)
        
        cursor.execute("UPDATE ads SET status='approved' WHERE id=?", (ad_id,))
        conn.commit()
        
        await bot.send_message(user_id, f"✅ Ваше объявление №{ad_id} одобрено и опубликовано в канале!")
        
        for mod_id in MODERATORS:
            try:
                await bot.send_message(mod_id, f"📌 <b>Объявление №{ad_id} ОДОБРЕНО</b>\n👮 Модератор: @{call.from_user.username}")
            except:
                pass
        
        await call.message.edit_reply_markup()
        await call.answer("✅ Объявление одобрено и опубликовано!")
        
    except Exception as e:
        logging.error(f"Ошибка при публикации объявления {ad_id}: {e}")
        await call.answer("❌ Ошибка при публикации!", show_alert=True)
    
    conn.close()

@dp.callback_query(lambda c: c.data and c.data.startswith("reject:"))
async def reject_ad(call: types.CallbackQuery):
    ad_id = int(call.data.split(":")[1])
    
    if ad_id in processed_ads:
        await call.answer("❌ Это объявление уже обработано!", show_alert=True)
        return
    
    processed_ads.add(ad_id)
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT user_id FROM ads WHERE id=?", (ad_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        await call.answer("❌ Объявление не найдено!", show_alert=True)
        return
    
    user_id = row[0]
    
    cursor.execute("UPDATE ads SET status='rejected' WHERE id=?", (ad_id,))
    conn.commit()
    conn.close()
    
    await bot.send_message(user_id, f"❌ Ваше объявление №{ad_id} отклонено модератором.\nПричина: не указана (свяжитесь с модератором для уточнения).")
    
    for mod_id in MODERATORS:
        try:
            await bot.send_message(mod_id, f"📌 <b>Объявление №{ad_id} ОТКЛОНЕНО</b>\n👮 Модератор: @{call.from_user.username}")
        except:
            pass
    
    await call.message.edit_reply_markup()
    await call.answer("❌ Объявление отклонено!")

# ================= АДМИН-КОМАНДЫ =================

@dp.message(Command("users"))
async def admin_users_count(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE last_ad_time > 0")
    active_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM ads WHERE status='pending'")
    pending_ads_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM ads WHERE status='approved'")
    approved_ads = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM ads WHERE status='rejected'")
    rejected_ads = cursor.fetchone()[0]
    
    conn.close()
    
    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"📝 Активных (с постами): {active_users}\n"
        f"⏳ Ожидают модерации: {pending_ads_count}\n"
        f"✅ Одобрено: {approved_ads}\n"
        f"❌ Отклонено: {rejected_ads}\n"
        f"🔄 В обработке сейчас: {len(processed_ads)}"
    )

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    
    text = message.get_args()
    if not text:
        await message.answer("❌ Укажите текст рассылки: /broadcast Текст")
        return
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()
    
    sent = 0
    failed = 0
    
    status_msg = await message.answer(f"📨 Начинаю рассылку {len(users)} пользователям...")
    
    for i, (user_id,) in enumerate(users):
        try:
            await bot.send_message(user_id, f"📢 <b>Рассылка</b>\n\n{text}")
            sent += 1
        except Exception as e:
            failed += 1
        
        if i % 10 == 0:
            await status_msg.edit_text(f"📨 Прогресс: {i}/{len(users)}\n✅ Отправлено: {sent}\n❌ Ошибок: {failed}")
    
    await status_msg.edit_text(f"✅ Рассылка завершена!\nОтправлено: {sent}\nОшибок: {failed}")

@dp.message(Command("clear_ads"))
async def admin_clear_ads(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    
    global pending_ads, processed_ads
    pending_ads.clear()
    processed_ads.clear()
    
    await message.answer("✅ Кэш объявлений очищен!")

@dp.message(Command("check_cooldown"))
async def check_cooldown(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    
    args = message.get_args()
    if not args:
        await message.answer("Укажите ID пользователя: /check_cooldown 123456789")
        return
    
    try:
        check_user_id = int(args)
    except:
        await message.answer("Некорректный ID")
        return
    
    conn, cursor = get_cursor()
    cursor.execute("SELECT last_ad_time FROM users WHERE user_id=?", (check_user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        await message.answer(f"Пользователь {check_user_id} не найден в БД")
        return
    
    last_ad_time = result[0]
    can_post_now, remaining = can_post(check_user_id, last_ad_time)
    
    last_ad_str = datetime.fromtimestamp(last_ad_time).strftime('%d.%m.%Y %H:%M:%S') if last_ad_time > 0 else "никогда"
    
    await message.answer(
        f"📊 <b>Информация о пользователе {check_user_id}</b>\n\n"
        f"КД: {format_time(COOLDOWN_SECONDS)}\n"
        f"Последняя подача: {last_ad_str}\n"
        f"Может подать сейчас: {'✅' if can_post_now else '❌'}\n"
        f"Осталось: {format_time(remaining) if not can_post_now else '0'}"
    )

# ================= ЗАПУСК =================

async def main():
    logging.info("🚀 Бот запущен на Render!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
