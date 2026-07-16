import os
import io
import uuid
import zipfile
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from aiohttp import web
import aiosqlite
from PIL import Image
from fpdf import FPDF
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InputMediaPhoto,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

# ===== КОНФИГУРАЦИЯ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL")
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
PREMIUM_PRICE = 150

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ===== БАЗА ДАННЫХ =====
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                combined_url TEXT,
                pieces_count INTEGER,
                format_type TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER, date TEXT, count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gallery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, session_id TEXT UNIQUE,
                preview_url TEXT, pieces_count INTEGER,
                preset_name TEXT, likes INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gallery_likes (
                user_id INTEGER, gallery_id INTEGER,
                PRIMARY KEY (user_id, gallery_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TIMESTAMP,
                telegram_charge_id TEXT,
                provider_charge_id TEXT
            )
        """)
        await db.commit()

async def check_limit(user_id: int):
    today = datetime.now().date().isoformat()
    if await is_premium(user_id):
        return True, 999
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT count FROM usage WHERE user_id=? AND date=?",
            (user_id, today)
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
            return count < FREE_LIMIT, FREE_LIMIT - count

async def increment_usage(user_id: int):
    if await is_premium(user_id):
        return
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO usage (user_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1",
            (user_id, today)
        )
        await db.commit()

async def is_premium(user_id: int) -> bool:
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row: return False
            return datetime.fromisoformat(row[0]) > datetime.now()

async def activate_premium(user_id: int, charge_id: str, provider_id: str):
    expires = (datetime.now() + timedelta(days=30)).isoformat()
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?, ?)",
            (user_id, expires, charge_id, provider_id)
        )
        await db.commit()

async def save_history(user_id, session_id, combined_url, pieces_count, format_type):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO history VALUES (NULL, ?, ?, ?, ?, ?, ?)",
            (user_id, session_id, combined_url, pieces_count, format_type)
        )
        await db.commit()

async def publish_to_gallery(user_id, session_id, preview_url, pieces_count, preset_name):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO gallery (user_id, session_id, preview_url, pieces_count, preset_name) VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, preview_url, pieces_count, preset_name)
        )
        await db.commit()

async def get_gallery(page=0, per_page=12):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT id, session_id, preview_url, pieces_count, preset_name, likes, created_at "
            "FROM gallery WHERE is_active=1 ORDER BY likes DESC, created_at DESC LIMIT ? OFFSET ?",
            (per_page, page * per_page)
        ) as cursor:
            return await cursor.fetchall()

async def toggle_like(user_id, gallery_id):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT 1 FROM gallery_likes WHERE user_id=? AND gallery_id=?",
            (user_id, gallery_id)
        ) as cursor:
            already = await cursor.fetchone()
        if already:
            await db.execute("DELETE FROM gallery_likes WHERE user_id=? AND gallery_id=?", (user_id, gallery_id))
            await db.execute("UPDATE gallery SET likes = MAX(0, likes - 1) WHERE id=?", (gallery_id,))
            liked = False
        else:
            await db.execute("INSERT INTO gallery_likes VALUES (?, ?)", (user_id, gallery_id))
            await db.execute("UPDATE gallery SET likes = likes + 1 WHERE id=?", (gallery_id,))
            liked = True
        await db.commit()
        async with db.execute("SELECT likes FROM gallery WHERE id=?", (gallery_id,)) as cursor:
            row = await cursor.fetchone()
            return liked, row[0]

# ===== PDF-ГЕНЕРАЦИЯ =====
def generate_pdf(images_data, page_size_mm, dpi=300):
    pdf = FPDF(unit='mm', format=(page_size_mm[0], page_size_mm[1]))
    pdf.set_auto_page_break(False)
    px_per_mm = dpi / 25.4
    pdf.add_page()
    margin, gap = 5, 2
    usable_w = page_size_mm[0] - 2 * margin
    first_w, _, _ = images_data[0]
    item_w_mm = first_w / px_per_mm
    item_h_mm = images_data[0][1] / px_per_mm
    cols = max(1, int((usable_w + gap) / (item_w_mm + gap)))
    x, y, col_idx = margin, margin, 0
    for img_w, img_h, content in images_data:
        tmp_path = f"/tmp/pdf_{uuid.uuid4().hex}.png"
        with open(tmp_path, 'wb') as f:
            f.write(content)
        pdf.image(tmp_path, x=x, y=y, w=img_w/px_per_mm, h=img_h/px_per_mm)
        os.unlink(tmp_path)
        col_idx += 1
        if col_idx >= cols:
            col_idx, x = 0, margin
            y += item_h_mm + gap
            if y + item_h_mm > page_size_mm[1] - margin:
                pdf.add_page()
                y = margin
        else:
            x += item_w_mm + gap
    return pdf.output()

# ===== КОМАНДЫ БОТА =====
@router.message(Command("start"))
async def cmd_start(message: Message):
    allowed, remaining = await check_limit(message.from_user.id)
    premium_status = "⭐ Премиум" if await is_premium(message.from_user.id) else f"🆓 {remaining}/{FREE_LIMIT}"
    kb = {
        "inline_keyboard": [
            [{"text": " Открыть приложение", "web_app": {"url": MINI_APP_URL}}],
            [{"text": "⭐ Премиум (150★)", "callback_data": "buy_premium"},
             {"text": "🖼️ Галерея", "web_app": {"url": f"{MINI_APP_URL}#gallery"}}],
            [{"text": "📜 История", "callback_data": "history"},
             {"text": "ℹ️ Помощь", "callback_data": "help"}]
        ]
    }
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"🎯 Возможности:\n"
        f"• ✂️ Нарезка картинок (px/мм)\n"
        f"• 📄 PDF-экспорт для печати\n"
        f"• 🛍️ Шаблоны WB/Ozon/Amazon\n"
        f"• 🖼️ Публичная галерея\n\n"
        f"📊 Статус: <b>{premium_status}</b>",
        reply_markup=kb
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🛠️ <b>Команды:</b>\n"
        "/start - Главное меню\n"
        "/premium - Купить подписку\n"
        "/history - История обработок\n"
        "/stats - Ваша статистика\n\n"
        "💡 <b>Советы:</b>\n"
        "• Используйте пресеты для быстрого старта\n"
        "• Включите JPG для экономии трафика\n"
        "• Отмечайте «В галерею» чтобы поделиться работой"
    )

@router.message(Command("premium"))
async def cmd_premium(message: Message):
    if await is_premium(message.from_user.id):
        await message.answer("✅ У вас уже активна премиум-подписка!")
        return
    await message.answer_invoice(
        title="⭐ Премиум-подписка на 30 дней",
        description=(
            "🚀 Безлимитные обработки\n"
            "📦 Пакетная обработка\n"
            "🎨 Без водяных знаков"
        ),
        prices=[LabeledPrice(label="Премиум", amount=PREMIUM_PRICE * 100)],
        provider_token="",
        payload=f"premium_{message.from_user.id}",
        currency="XTR"
    )

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pieces_count), 0) FROM history WHERE user_id=?",
            (message.from_user.id,)
        ) as cursor:
            total, pieces = await cursor.fetchone()
    premium = "⭐ ДА" if await is_premium(message.from_user.id) else "Нет"
    await message.answer(
        f"📊 <b>Ваша статистика:</b>\n\n"
        f"🎨 Обработок: <b>{total or 0}</b>\n"
        f"✂️ Кусочков: <b>{pieces or 0}</b>\n"
        f"⭐ Премиум: <b>{premium}</b>"
    )

@router.message(Command("history"))
async def cmd_history(message: Message):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT combined_url, pieces_count, format_type, created_at "
            "FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (message.from_user.id,)
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await message.answer("📭 История пуста.")
        return
    text = "📜 <b>Последние обработки:</b>\n\n"
    kb = []
    for i, (url, count, fmt, date) in enumerate(rows, 1):
        text += f"{i}. {count} кусочков ({fmt.upper()}) — {date[:10]}\n"
        if url:
            kb.append([{"text": f"📥 #{i}", "url": url}])
    await message.answer(text, reply_markup={"inline_keyboard": kb})

# ===== CALLBACK =====
@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(callback: CallbackQuery):
    await callback.message.answer_invoice(
        title="⭐ Премиум на 30 дней",
        description="Безлимит + AI-функции",
        prices=[LabeledPrice(label="Премиум", amount=PREMIUM_PRICE * 100)],
        provider_token="",
        payload=f"premium_{callback.from_user.id}",
        currency="XTR"
    )
    await callback.answer()

@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery):
    await cmd_history(callback.message)
    await callback.answer()

@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await cmd_help(callback.message)
    await callback.answer()

@router.callback_query(F.data.startswith("like:"))
async def cb_like(callback: CallbackQuery):
    gallery_id = int(callback.data.split(":")[1])
    liked, new_count = await toggle_like(callback.from_user.id, gallery_id)
    await callback.answer(f"{'❤️' if liked else '💔'} {new_count}")
    try:
        await callback.message.edit_reply_markup(
            reply_markup={"inline_keyboard": [[
                {"text": f"{'❤️' if liked else '🤍'} {new_count}", "callback_data": f"like:{gallery_id}"}
            ]]}
        )
    except:
        pass

@router.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    if payment.invoice_payload.startswith("premium_"):
        await activate_premium(
            message.from_user.id,
            payment.telegram_payment_charge_id,
            payment.provider_payment_charge_id
        )
        await message.answer(
            "🎉 <b>Добро пожаловать в премиум!</b>\n\n"
            "✅ Подписка активна на 30 дней\n"
            "✅ Все лимиты сняты\n"
            "✅ AI-функции разблокированы"
        )

# ===== API: ЗАГРУЗКА =====
async def handle_upload(request):
    return web.json_response({
        "status": "ok",
        "message": "API работает! Cloudinary будет добавлен позже."
    })

# ===== API: ГАЛЕРЕЯ =====
async def handle_gallery(request):
    page = int(request.query.get('page', 0))
    rows = await get_gallery(page, 12)
    items = [{
        "id": r[0], "session_id": r[1], "preview_url": r[2],
        "pieces_count": r[3], "preset_name": r[4],
        "likes": r[5], "created_at": r[6]
    } for r in rows]
    return web.json_response({"items": items, "page": page})

# ===== ПУЛЕНЕПРОБИВАЕМЫЙ ЗАПУСК =====
async def health_handler(request):
    return web.json_response({"status": "ok", "message": "Сервер работает!"})

async def on_startup(app):
    await init_db()
    print("=" * 50)
    print("✅ База данных инициализирована")
    print(f"🌐 Mini App URL: {MINI_APP_URL}")
    try:
        me = await bot.get_me()
        print(f"🤖 Бот @{me.username} успешно подключен к Telegram!")
    except Exception as e:
        print(f"⚠️ Ошибка подключения бота (проверьте BOT_TOKEN): {e}")
    print("=" * 50)

async def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_post("/upload", handle_upload)
    app.router.add_get("/gallery", handle_gallery)
    app.router.add_get("/health", health_handler)

    port = int(os.environ.get("PORT", 8080))
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f" Веб-сервер запущен на порту {port}")

    print("🔄 Запускаем polling бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())