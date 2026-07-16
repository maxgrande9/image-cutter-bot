import os
import io
import uuid
import zipfile
import asyncio
import qrcode
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

# Cloudinary НЕ импортируется здесь, чтобы не крашить старт!
CLOUDINARY_AVAILABLE = False

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
                user_id INTEGER, session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                combined_url TEXT, pieces_count INTEGER, format_type TEXT
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
                telegram_charge_id TEXT, provider_charge_id TEXT
            )
        """)
        await db.commit()

async def check_limit(user_id: int):
    today = datetime.now().date().isoformat()
    if await is_premium(user_id): return True, 999
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT count FROM usage WHERE user_id=? AND date=?", (user_id, today)) as cursor:
            row = await cursor.fetchone()
            return (row[0] if row else 0) < FREE_LIMIT, FREE_LIMIT - (row[0] if row else 0)

async def increment_usage(user_id: int):
    if await is_premium(user_id): return
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("INSERT INTO usage (user_id, date, count) VALUES (?, ?, 1) ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1", (user_id, today))
        await db.commit()

async def is_premium(user_id: int) -> bool:
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return False
            return datetime.fromisoformat(row[0]) > datetime.now()

async def activate_premium(user_id: int, charge_id: str, provider_id: str):
    expires = (datetime.now() + timedelta(days=30)).isoformat()
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?, ?)", (user_id, expires, charge_id, provider_id))
        await db.commit()

async def save_history(user_id, session_id, combined_url, pieces_count, format_type):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("INSERT INTO history VALUES (NULL, ?, ?, ?, ?, ?, ?)", (user_id, session_id, combined_url, pieces_count, format_type))
        await db.commit()

async def publish_to_gallery(user_id, session_id, preview_url, pieces_count, preset_name):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("INSERT OR REPLACE INTO gallery (user_id, session_id, preview_url, pieces_count, preset_name) VALUES (?, ?, ?, ?, ?)", (user_id, session_id, preview_url, pieces_count, preset_name))
        await db.commit()

async def get_gallery(page=0, per_page=12):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT id, session_id, preview_url, pieces_count, preset_name, likes, created_at FROM gallery WHERE is_active=1 ORDER BY likes DESC, created_at DESC LIMIT ? OFFSET ?", (per_page, page * per_page)) as cursor:
            return await cursor.fetchall()

async def toggle_like(user_id, gallery_id):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT 1 FROM gallery_likes WHERE user_id=? AND gallery_id=?", (user_id, gallery_id)) as cursor:
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
            return liked, (await cursor.fetchone())[0]

def generate_pdf(images_data, page_size_mm, dpi=300):
    pdf = FPDF(unit='mm', format=(page_size_mm[0], page_size_mm[1]))
    pdf.set_auto_page_break(False)
    px_per_mm = dpi / 25.4
    pdf.add_page()
    margin, gap = 5, 2
    usable_w = page_size_mm[0] - 2 * margin
    first_w, first_h, _ = images_data[0]
    item_w_mm, item_h_mm = first_w / px_per_mm, first_h / px_per_mm
    cols = max(1, int((usable_w + gap) / (item_w_mm + gap)))
    x, y, col_idx = margin, margin, 0
    for img_w, img_h, content in images_data:
        tmp_path = f"/tmp/pdf_{uuid.uuid4().hex}.png"
        with open(tmp_path, 'wb') as f: f.write(content)
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
    kb = {"inline_keyboard": [
        [{"text": "🚀 Открыть приложение", "web_app": {"url": MINI_APP_URL}}],
        [{"text": "⭐ Премиум (150★)", "callback_data": "buy_premium"}, {"text": "🖼️ Галерея", "web_app": {"url": f"{MINI_APP_URL}#gallery"}}],
        [{"text": "📜 История", "callback_data": "history"}, {"text": "ℹ️ Помощь", "callback_data": "help"}]
    ]}
    await message.answer(f"👋 Привет, {message.from_user.first_name}!\n\n🎯 Возможности:\n• ✂️ Нарезка картинок\n• 📄 PDF-экспорт\n• 🛍️ Шаблоны маркетплейсов\n\n📊 Статус: <b>{premium_status}</b>", reply_markup=kb)

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("🛠️ <b>Команды:</b>\n/start - Меню\n/premium - Подписка\n/history - История\n/stats - Статистика")

@router.message(Command("premium"))
async def cmd_premium(message: Message):
    if await is_premium(message.from_user.id):
        await message.answer("✅ У вас уже активна премиум-подписка!")
        return
    await message.answer_invoice(title="⭐ Премиум на 30 дней", description="Безлимит + все функции", prices=[LabeledPrice(label="Премиум", amount=PREMIUM_PRICE * 100)], provider_token="", payload=f"premium_{message.from_user.id}", currency="XTR")

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(pieces_count), 0) FROM history WHERE user_id=?", (message.from_user.id,)) as cursor:
            total, pieces = await cursor.fetchone()
    await message.answer(f"📊 <b>Статистика:</b>\n🎨 Обработок: <b>{total or 0}</b>\n✂️ Кусочков: <b>{pieces or 0}</b>")

@router.message(Command("history"))
async def cmd_history(message: Message):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT combined_url, pieces_count, format_type, created_at FROM history WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (message.from_user.id,)) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await message.answer("📭 История пуста.")
        return
    text = "📜 <b>История:</b>\n\n"
    kb = []
    for i, (url, count, fmt, date) in enumerate(rows, 1):
        text += f"{i}. {count} шт. ({fmt.upper()}) — {date[:10]}\n"
        if url: kb.append([{"text": f"📥 #{i}", "url": url}])
    await message.answer(text, reply_markup={"inline_keyboard": kb})

@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(callback: CallbackQuery):
    await callback.message.answer_invoice(title="⭐ Премиум", description="30 дней безлимит", prices=[LabeledPrice(label="Премиум", amount=PREMIUM_PRICE * 100)], provider_token="", payload=f"premium_{callback.from_user.id}", currency="XTR")
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
        await callback.message.edit_reply_markup(reply_markup={"inline_keyboard": [[{"text": f"{'❤️' if liked else '🤍'} {new_count}", "callback_data": f"like:{gallery_id}"}]]})
    except: pass

@router.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    if payment.invoice_payload.startswith("premium_"):
        await activate_premium(message.from_user.id, payment.telegram_payment_charge_id, payment.provider_payment_charge_id)
        await message.answer("🎉 <b>Премиум активирован!</b>\n✅ 30 дней безлимитного доступа.")

# ===== API: ЗАГРУЗКА (БЕЗОПАСНЫЙ ИМПОРТ CLOUDINARY) =====
async def handle_upload(request):
    global CLOUDINARY_AVAILABLE
    
    # 1. Проверяем и инициализируем Cloudinary ТОЛЬКО ЗДЕСЬ
    if not CLOUDINARY_AVAILABLE:
        cloudinary_url = os.getenv("CLOUDINARY_URL", "").strip().strip('"').strip("'")
        if cloudinary_url and cloudinary_url.startswith("cloudinary://"):
            try:
                import cloudinary
                import cloudinary.uploader
                cloudinary.config(cloudinary_url=cloudinary_url)
                CLOUDINARY_AVAILABLE = True
                print("✅ Cloudinary подключен при первом запросе")
            except Exception as e:
                print(f"⚠️ Ошибка Cloudinary: {e}")
                return web.json_response({"status": "error", "message": "Ошибка настройки Cloudinary"}, status=500)
        else:
            return web.json_response({
                "status": "error",
                "message": "CLOUDINARY_URL не задан в Railway. Добавьте переменную, начинающуюся с 'cloudinary://'"
            }, status=500)

    # 2. Чтение данных
    reader = await request.multipart()
    user_id = None
    format_type = 'png'
    quality = 85
    files = {}
    
    while True:
        part = await reader.next()
        if part is None: break
        name = part.name
        if name == "user_id": user_id = int((await part.read()).decode('utf-8'))
        elif name == "format": format_type = (await part.read()).decode('utf-8')
        elif name == "quality": quality = int((await part.read()).decode('utf-8'))
        elif name.startswith("file_"): files[name] = (part.filename, await part.read())

    if not user_id or not files:
        return web.json_response({"status": "error", "message": "Нет данных"}, status=400)

    session_id = str(uuid.uuid4())[:8]
    converted = {}
    
    # 3. Конвертация
    for key, (filename, content) in files.items():
        if format_type == 'jpg' and filename.endswith('.png'):
            img = Image.open(io.BytesIO(content))
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P': img = img.convert('RGBA')
                if 'A' in img.mode: bg.paste(img, mask=img.split()[-1])
                else: bg.paste(img)
                img = bg
            else:
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=True)
            converted[key] = (filename.replace('.png', '.jpg'), buf.getvalue())
        else:
            converted[key] = (filename, content)

    # 4. Загрузка в Cloudinary
    import cloudinary.uploader # Теперь это безопасно
    cloudinary_urls = {}
    for key, (filename, content) in converted.items():
        try:
            result = cloudinary.uploader.upload(io.BytesIO(content), public_id=f"user_{user_id}/{session_id}_{Path(filename).stem}", resource_type="image", overwrite=True)
            cloudinary_urls[key] = result['secure_url']
        except Exception as e:
            return web.json_response({"status": "error", "message": f"Cloudinary: {e}"}, status=500)

    # 5. ZIP и PDF (упрощенно)
    zip_buffer = io.BytesIO