# bot.py
import sqlite3
import os
import logging
from datetime import datetime, timedelta
import uuid
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters
)
import requests
from yookassa import Configuration, Payment

# ========================
# 🔑 СЕКРЕТЫ — ЗАМЕНИ НА СВОИ!
# ========================
TELEGRAM_BOT_TOKEN = "8329030593:AAEQrknENECxRTiPYj-Cgwc1dHUiZlaoS5I"
OPENROUTER_API_KEY = "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"
YOO_ACCOUNT_ID = 1080657
YOO_SECRET_KEY = "live__MaNSRR25nEEP4hBYOMsoq0zy7BftJxIeMyPk4K5958"

# Твой Telegram ID
ADMIN_TELEGRAM_ID = 8523454656

# ========================
# НАСТРОЙКА
# ========================
DB_PATH = "users.db"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Configuration.account_id = YOO_ACCOUNT_ID
Configuration.secret_key = YOO_SECRET_KEY


# ========================
# ПРОВЕРКА ПОДПИСКИ НА КАНАЛ
# ========================
async def is_subscribed_to_channel(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id == ADMIN_TELEGRAM_ID:
        return True  # Админ всегда проходит
    try:
        chat_member = await context.bot.get_chat_member(chat_id="@prognozor_novosti", user_id=user_id)
        return chat_member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Ошибка проверки подписки для {user_id}: {e}")
        return False


# ========================
# БАЗА ДАННЫХ
# ========================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                free_used INTEGER DEFAULT 0,
                last_free_reset DATE,
                premium_left INTEGER DEFAULT 0,
                premium_until TEXT,
                is_admin BOOLEAN DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                status TEXT,
                created_at TEXT,
                captured_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER,
                referred_id INTEGER PRIMARY KEY,
                used_at TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('premium_price', '500')
        """)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
        except:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0")
            conn.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (ADMIN_TELEGRAM_ID,))
        except:
            pass


# ========================
# РЕФЕРАЛЬНАЯ ПРОГРАММА
# ========================
def add_referral_bonus(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE users
            SET free_used = MAX(0, free_used - 3),
                last_free_reset = ?
            WHERE user_id = ?
        """, (datetime.utcnow().date().isoformat(), user_id))


# ========================
# УПРАВЛЕНИЕ PREMIUM
# ========================
def revoke_premium(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE users
            SET premium_left = 0,
                premium_until = NULL
            WHERE user_id = ?
        """, (user_id,))

def grant_premium(user_id, matches=50, days=30):
    until = datetime.utcnow() + timedelta(days=days)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (user_id, premium_left, premium_until)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                premium_left = ?,
                premium_until = ?
        """, (user_id, matches, until.isoformat(), matches, until.isoformat()))


# ========================
# РАБОТА С НАСТРОЙКАМИ
# ========================
def get_premium_price() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'premium_price'").fetchone()
        return int(row[0]) if row else 500

def set_premium_price(price: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'premium_price'", (str(price),))


# ========================
# ЛОГИКА ЛИМИТОВ
# ========================
def reset_daily_free():
    today = datetime.utcnow().date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET free_used = 0, last_free_reset = ? WHERE last_free_reset != ?", (today, today))
        conn.execute("UPDATE users SET last_free_reset = ? WHERE last_free_reset IS NULL", (today,))

def has_free_prediction(user_id):
    reset_daily_free()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT free_used FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row and row[0] < 3

def use_free_prediction(user_id):
    today = datetime.utcnow().date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (user_id, free_used, last_free_reset)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                free_used = free_used + 1,
                last_free_reset = ?
            WHERE free_used < 3
        """, (user_id, today, today))

def has_premium_prediction(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE users SET premium_left = 0, premium_until = NULL
            WHERE premium_until IS NOT NULL AND datetime(premium_until) < datetime('now')
        """)
        row = conn.execute("""
            SELECT premium_left FROM users
            WHERE user_id = ? AND premium_left > 0
        """, (user_id,)).fetchone()
        return bool(row)

def use_premium_prediction(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE users SET premium_left = premium_left - 1
            WHERE user_id = ? AND premium_left > 0
        """, (user_id,))


# ========================
# НЕЙРОСЕТЬ С УЧЁТОМ ДАТЫ — ИСПРАВЛЕНЫ URL
# ========================
def get_premium_prediction(team1: str, team2: str, match_date: str) -> str:
    prompt = f"""
Ты профессиональный капер с 10-летним опытом. Матч **{team1} vs {team2}** состоится **{match_date}**.
Анализ проводится **до матча**. Вы **не знаете результата**, так как матч ещё не сыгран.
Используйте только гипотетическую, но правдоподобную статистику по текущей форме, очным встречам, мотивации.

Сделайте прогноз, строго следуя структуре:

### 🔮 1. Анализ команд.

### 📊 2. Рекомендуемые ставки.

### ➕ 3. Ключевые статистические факты.
• Краткое обоснование.

### ⚽ 4. Вердикт капера

❗ ВАЖНО:
- Только на русском.
- Не выдумывайте реальные имена, даты, источники.
- Не пишите «как ИИ» — вы человек-аналитик.
- Не упоминайте, что матч в будущем — это и так ясно.
"""
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://t.me/prognozor_bot",
                "X-Title": "Football Predictor"
            },
            json={
                "model": "meta-llama/llama-3-8b-instruct",
                "messages": [{"role": "user", "content": prompt.strip()}],
                "max_tokens": 800,
                "temperature": 0.7
            },
            timeout=20
        )
        if r.status_code == 200:
            data = r.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"].strip()
        return "Не удалось сгенерировать прогноз."
    except Exception as e:
        return f"Ошибка генерации: {str(e)[:100]}"


# ========================
# ВАЛИДАЦИЯ ДАТЫ
# ========================
def validate_date(date_str: str) -> bool:
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", date_str):
        return False
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return dt.date() >= datetime.utcnow().date()
    except ValueError:
        return False


# ========================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: СТАТИСТИКА
# ========================
def get_stats_text() -> str:
    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        active_premium = conn.execute("""
            SELECT COUNT(*) FROM users
            WHERE premium_left > 0
              AND (premium_until IS NULL OR datetime(premium_until) > datetime('now'))
        """).fetchone()[0]

        today = datetime.utcnow().date().isoformat()
        result = conn.execute("SELECT SUM(free_used) FROM users WHERE last_free_reset = ?", (today,)).fetchone()
        free_used_today = result[0] if result[0] is not None else 0

        result2 = conn.execute("SELECT SUM(50 - premium_left) FROM users WHERE premium_left < 50").fetchone()
        premium_used_total = result2[0] if result2[0] is not None else 0

    return (
        f"📊 *Статистика*:\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💎 Активных Premium: {active_premium}\n"
        f"🆓 Бесплатных прогнозов сегодня: {free_used_today}\n"
        f"📈 Premium-прогнозов использовано: {premium_used_total}"
    )


# ========================
# ОБРАБОТЧИКИ
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 🔒 Проверка подписки
    if not await is_subscribed_to_channel(user_id, context):
        keyboard = [[InlineKeyboardButton("📣 Подписаться на новости", url="https://t.me/prognozor_novosti")]]
        await update.message.reply_text(
            "❗ Чтобы пользоваться ботом, подпишитесь на наш канал с новостями:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    username = update.effective_user.username
    is_admin = (user_id == ADMIN_TELEGRAM_ID)

    if context.args and context.args[0].startswith("ref"):
        try:
            referrer_id = int(context.args[0][3:])
            if referrer_id != user_id:
                with sqlite3.connect(DB_PATH) as conn:
                    existing = conn.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (user_id,)).fetchone()
                    if not existing:
                        conn.execute("""
                            INSERT OR IGNORE INTO referrals (referrer_id, referred_id, used_at)
                            VALUES (?, ?, ?)
                        """, (referrer_id, user_id, datetime.utcnow().isoformat()))
                        add_referral_bonus(referrer_id)
        except (ValueError, TypeError):
            pass

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, is_admin)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = ?
        """, (user_id, username, int(is_admin), username))

    free_left = 0
    if has_free_prediction(user_id):
        with sqlite3.connect(DB_PATH) as conn:
            used = conn.execute("SELECT free_used FROM users WHERE user_id = ?", (user_id,)).fetchone()
            free_left = 3 - (used[0] if used else 0)

    premium_info = ""
    if has_premium_prediction(user_id):
        with sqlite3.connect(DB_PATH) as conn:
            left = conn.execute("SELECT premium_left FROM users WHERE user_id = ?", (user_id,)).fetchone()
            premium_info = f"💎 Premium: {left[0]} прогнозов осталось"

    greeting = (
        "🔮 *Добро пожаловать в Прогнозор!* — ваш личный футбольный аналитик на основе искусственного интеллекта.\n\n"
        "⚡️ *Как это работает?*\n"
        "1. Введите названия команд (например: `Зенит - Спартак`)\n"
        "2. Укажите дату матча (`дд.мм.гггг`)\n"
        "3. Получите *профессиональный прогноз* **до начала игры**:\n"
        "   • Исход матча\n"
        "   • Тотал голов\n"
        "   • Фора\n"
        "   • Обе забьют?\n"
        "   • Точный счёт\n\n"
        "✨ *Ваши возможности:*\n"
        f"• 🆓 *Бесплатно*: {free_left} прогнозов сегодня\n"
        f"{('• 💎 *Premium*: ' + premium_info) if premium_info else '• 💎 *Premium*: 50 прогнозов в месяц'}\n\n"
        "🤝 *Привлекайте друзей!*\n"
        "Получайте **+3 бесплатных прогноза** за каждого друга по реферальной ссылке.\n\n"
        "📄 *Юридически безопасно:*\n"
        "• [Политика конфиденциальности](https://telegra.ph/Politika-konfidencialnosti-08-15-17)\n"
        "• [Пользовательское соглашение](https://telegra.ph/Polzovatelskoe-soglashenie-08-15-10)\n\n"
        "🚀 Готовы к точному прогнозу? Нажмите кнопку ниже!"
    )

    keyboard = [
        [InlineKeyboardButton("🔮 Сделать прогноз", callback_data="predict_start")],
        [InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")],
        [InlineKeyboardButton("🎁 Моя реферальная ссылка", callback_data="my_ref_link")]
    ]
    if is_admin:
        keyboard.append([InlineKeyboardButton("🛠 Админка", callback_data="admin_menu")])

    await update.message.reply_text(
        greeting,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 🔒 Проверка подписки
    if not await is_subscribed_to_channel(user_id, context):
        await update.callback_query.answer("Подпишитесь на канал, чтобы продолжить.", show_alert=True)
        keyboard = [[InlineKeyboardButton("📣 Подписаться на новости", url="https://t.me/prognozor_novosti")]]
        await update.callback_query.message.reply_text(
            "❗ Чтобы пользоваться ботом, подпишитесь на наш канал с новостями:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "predict_start":
        await query.edit_message_text(
            "Напишите названия команд через дефis:\n\n*Пример:*\n`Зенит - Спартак`",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_prediction"] = True
        context.user_data["awaiting_date"] = False

    elif data == "buy_premium":
        try:
            price = get_premium_price()
            payment = Payment.create({
                "amount": {"value": f"{price}.00", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": "https://t.me/prognozor_bot"},
                "description": f"Premium: 50 прогнозов / 30 дней ({price} ₽)",
                "metadata": {"user_id": str(user_id)}
            }, uuid.uuid4())

            created_at_iso = payment.created_at if payment.created_at else datetime.utcnow().isoformat()

            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    INSERT INTO payments (payment_id, user_id, amount, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    payment.id,
                    user_id,
                    float(price),
                    payment.status,
                    created_at_iso
                ))

            await query.message.reply_text(
                f"💳 Оплатите {price} ₽:\n{payment.confirmation.confirmation_url}\n\n"
                "После оплаты напишите: /check_payment"
            )
        except Exception as e:
            logger.error(f"Ошибка создания платежа: {e}")
            await query.message.reply_text("❌ Не удалось создать платёж.")

    elif data == "my_ref_link":
        ref_link = f"https://t.me/prognozor_bot?start=ref{user_id}"
        await query.message.reply_text(
            f"🔗 Ваша ссылка:\n`{ref_link}`\n\n"
            "Отправьте её другу — и получите **+3 бесплатных прогноза**!",
            parse_mode="Markdown"
        )

    elif data == "admin_menu" and user_id == ADMIN_TELEGRAM_ID:
        keyboard = [
            [InlineKeyboardButton("Выдать Premium", callback_data="admin_grant")],
            [InlineKeyboardButton("Забрать Premium", callback_data="admin_revoke")],
            [InlineKeyboardButton("Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("Изменить цену", callback_data="admin_set_price")],
            [InlineKeyboardButton("← Назад", callback_data="start")]
        ]
        await query.edit_message_text(
            "🛠 *Админка — выберите действие:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data == "admin_grant":
        await query.edit_message_text("Введите: `/grant_premium ID`", parse_mode="Markdown")
    elif data == "admin_revoke":
        await query.edit_message_text("Введите: `/revoke_premium ID`", parse_mode="Markdown")
    elif data == "admin_stats":
        await query.answer()
        text = get_stats_text()
        await query.message.reply_text(text, parse_mode="Markdown")
    elif data == "admin_set_price":
        await query.edit_message_text("Введите: `/set_price 600`")
    elif data == "start":
        await start(update, context)
    else:
        await start(update, context)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 🔒 Проверка подписки
    if not await is_subscribed_to_channel(user_id, context):
        keyboard = [[InlineKeyboardButton("📣 Подписаться на новости", url="https://t.me/prognozor_novosti")]]
        await update.message.reply_text(
            "❗ Чтобы пользоваться ботом, подпишитесь на наш канал с новостями:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    text = update.message.text.strip()

    if context.user_data.get("awaiting_prediction") and not context.user_data.get("awaiting_date"):
        if " - " not in text:
            await update.message.reply_text("❌ Формат: `Команда1 - Команда2`", parse_mode="Markdown")
            return

        team1, team2 = map(str.strip, text.split(" - ", 1))
        context.user_data["team1"] = team1
        context.user_data["team2"] = team2
        context.user_data["awaiting_date"] = True
        context.user_data["awaiting_prediction"] = False

        await update.message.reply_text(
            "📅 Когда состоится матч?\n\n*Формат: дд.мм.гггг*\nПример: `15.06.2025`",
            parse_mode="Markdown"
        )
        return

    if context.user_data.get("awaiting_date"):
        if not validate_date(text):
            await update.message.reply_text(
                "❌ Неверный формат или дата в прошлом.\nВведите в формате: `дд.мм.гггг`",
                parse_mode="Markdown"
            )
            return

        team1 = context.user_data["team1"]
        team2 = context.user_data["team2"]
        match_date = text

        has_free = has_free_prediction(user_id)
        has_prem = has_premium_prediction(user_id)

        if not has_free and not has_prem:
            await update.message.reply_text(
                "❌ Прогнозы исчерпаны!\n• Бесплатно: 3/день\n• Premium: 50/мес",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")
                ]])
            )
            return

        source = "Бесплатно"
        if has_free:
            use_free_prediction(user_id)
        elif has_prem:
            use_premium_prediction(user_id)
            source = "Premium"

        pred = get_premium_prediction(team1, team2, match_date)

        await update.message.reply_text(
            f"🔮 *{team1} vs {team2}*\n📅 *Дата матча: {match_date}*\n\n{pred}\n\n({source})",
            parse_mode="Markdown"
        )
        await start(update, context)
        return

    await update.message.reply_text("Нажмите /start")


# ========================
# КОМАНДЫ
# ========================
async def grant_premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("Используйте: /grant_premium 123456789")
        return
    try:
        user_id = int(context.args[0])
        grant_premium(user_id)
        await update.message.reply_text(f"✅ Premium выдан пользователю {user_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def revoke_premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("Используйте: /revoke_premium 123456789")
        return
    try:
        user_id = int(context.args[0])
        revoke_premium(user_id)
        await update.message.reply_text(f"🚫 Premium отозван у пользователя {user_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    text = get_stats_text()
    await update.message.reply_text(text, parse_mode="Markdown")

async def set_price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Используйте: /set_price 600")
        return
    new_price = int(context.args[0])
    set_premium_price(new_price)
    await update.message.reply_text(f"✅ Цена изменена на {new_price} ₽")

async def check_payment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 🔒 Проверка подписки (не обязательна, но для единообразия)
    if not await is_subscribed_to_channel(user_id, context):
        keyboard = [[InlineKeyboardButton("📣 Подписаться на новости", url="https://t.me/prognozor_novosti")]]
        await update.message.reply_text(
            "❗ Чтобы проверить платёж, подпишитесь на канал:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT payment_id FROM payments
            WHERE user_id = ? AND status != 'succeeded'
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,)).fetchone()
        if not row:
            await update.message.reply_text("❌ Нет активных платежей.")
            return
        payment_id = row[0]
        try:
            payment_info = Payment.find_one(payment_id)
            if payment_info.status == "succeeded":
                grant_premium(user_id)
                await update.message.reply_text("✅ Premium активирован на 30 дней.")
            else:
                await update.message.reply_text(f"⏳ Статус: *{payment_info.status}*", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка проверки платежа: {e}")
            await update.message.reply_text("❌ Не удалось проверить платёж.")


# ========================
# ЗАПУСК
# ========================
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("grant_premium", grant_premium_cmd))
    app.add_handler(CommandHandler("revoke_premium", revoke_premium_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("set_price", set_price_cmd))
    app.add_handler(CommandHandler("check_payment", check_payment_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("🚀 Прогнозор Bot (с обязательной подпиской) запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()