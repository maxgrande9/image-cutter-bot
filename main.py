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
# Безопасный импорт cloudinary — не крашит, если переменная не задана
CLOUDINARY_AVAILABLE = False
try:
    import cloudinary
    import cloudinary.uploader
    
    # Проверяем, задана ли переменная
    cloudinary_url = os.getenv("CLOUDINARY_URL")
    if cloudinary_url and cloudinary_url.strip().startswith("cloudinary://"):
              CLOUDINARY_AVAILABLE = True
        print("✅ Cloudinary подключен")
    else:
        print("️ CLOUDINARY_URL не задан — загрузка файлов отключена")
except Exception as e:
    CLOUDINARY_AVAILABLE = False
    print(f"⚠️ Cloudinary не инициализирован: {e}")
from PIL import Image
from fpdf import FPDF
from rembg import remove, new_session
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InputMediaPhoto,
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment
)
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

# ===== КОНФИГУРАЦИЯ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL")
MINI_APP_URL = os.getenv("MINI_APP_URL")
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5"))
PREMIUM_PRICE = 150  # Telegram Stars

# ===== БЕЗОПАСНАЯ ИНИЦИАЛИЗАЦИЯ CLOUDINARY =====
CLOUDINARY_AVAILABLE = False
raw_cloudinary_url = os.getenv("CLOUDINARY_URL", "").strip().strip('"').strip("'")

if raw_cloudinary_url.startswith("cloudinary://"):
    try:
        import cloudinary
        cloudinary.config(cloudinary_url=raw_cloudinary_url)
        CLOUDINARY_AVAILABLE = True
        print("✅ Cloudinary успешно подключен")
    except Exception as e:
        print(f"⚠️ Ошибка инициализации Cloudinary: {e}")
else:
    print("⚠️ CLOUDINARY_URL не задан или имеет неверный формат. Бот запустится, но загрузка файлов будет недоступна.")
# ================================================

# Загружаем rembg лениво (экономим RAM на старте)
rembg_session = None
def get_rembg():
    global rembg_session
    if rembg_session is None:
        print("⏳ Загрузка модели rembg...")
        rembg_session = new_session("u2net")
        print("✅ rembg загружена")
    return rembg_session

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
            [{"text": "🚀 Открыть приложение", "web_app": {"url": MINI_APP_URL}}],
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
        f"• 🪄 AI-удаление фона\n"
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
            "🪄 AI-удаление фона\n"
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
    reader = await request.multipart()
    user_id = None
    format_type = 'png'
    quality = 85
    add_qr = False
    remove_bg = False
    export_pdf = False
    pdf_page = 'a4'
    publish_gallery = False
    preset_name = 'custom'
    files = {}

    while True:
        part = await reader.next()
        if part is None: break
        name = part.name
        if name == "user_id":
            user_id = int((await part.read()).decode('utf-8'))
        elif name == "format":
            format_type = (await part.read()).decode('utf-8')
        elif name == "quality":
            quality = int((await part.read()).decode('utf-8'))
        elif name == "add_qr":
            add_qr = (await part.read()).decode('utf-8') == 'true'
        elif name == "remove_bg":
            remove_bg = (await part.read()).decode('utf-8') == 'true'
        elif name == "export_pdf":
            export_pdf = (await part.read()).decode('utf-8') == 'true'
        elif name == "pdf_page":
            pdf_page = (await part.read()).decode('utf-8')
        elif name == "publish_gallery":
            publish_gallery = (await part.read()).decode('utf-8') == 'true'
        elif name == "preset_name":
            preset_name = (await part.read()).decode('utf-8')
        elif name.startswith("file_"):
            files[name] = (part.filename, await part.read())

    if not user_id or not files:
        return web.json_response({"status": "error", "message": "Нет данных"}, status=400)

    allowed, remaining = await check_limit(user_id)
    if not allowed:
        return web.json_response({
            "status": "limit",
            "message": f"Достигнут лимит ({FREE_LIMIT}/день). Купите премиум!"
        }, status=429)

    session_id = str(uuid.uuid4())[:8]

    # AI-удаление фона
    if remove_bg:
        try:
            session = get_rembg()
            for key in list(files.keys()):
                filename, content = files[key]
                img = Image.open(io.BytesIO(content))
                result = remove(img, session=session)
                buf = io.BytesIO()
                result.save(buf, format='PNG')
                files[key] = (filename.replace('.jpg', '.png'), buf.getvalue())
        except Exception as e:
            print(f"rembg error: {e}")

    # Конвертация формата
    converted = {}
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

    # QR-код
    if add_qr and "file_combined" in converted:
        filename, content = converted["file_combined"]
        img = Image.open(io.BytesIO(content))
        qr = qrcode.QRCode(box_size=10, border=2)
        me = await bot.get_me()
        qr.add_data(f"https://t.me/{me.username}")
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
        qr_size = min(img.size) // 6
        qr_img = qr_img.resize((qr_size, qr_size))
        img.paste(qr_img, (img.width - qr_size - 20, img.height - qr_size - 20))
        buf = io.BytesIO()
        img.save(buf, format='PNG' if filename.endswith('.png') else 'JPEG', quality=quality)
        converted["file_combined"] = (filename, buf.getvalue())

    # Загрузка в Cloudinary
    cloudinary_urls = {}
    for key, (filename, content) in converted.items():
        try:
            result = cloudinary.uploader.upload(
                io.BytesIO(content),
                public_id=f"user_{user_id}/{session_id}_{Path(filename).stem}",
                resource_type="image",
                overwrite=True
            )
            cloudinary_urls[key] = result['secure_url']
        except Exception as e:
            return web.json_response({"status": "error", "message": f"Cloudinary: {e}"}, status=500)

    # ZIP-архив
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for key, (filename, content) in converted.items():
            if key.startswith("file_piece_"):
                zf.writestr(filename, content)
    zip_buffer.seek(0)
    zip_url = None
    try:
        zip_result = cloudinary.uploader.upload_large(
            zip_buffer,
            public_id=f"user_{user_id}/{session_id}_archive",
            resource_type="raw", format="zip"
        )
        zip_url = zip_result['secure_url']
    except Exception as e:
        print(f"ZIP error: {e}")

    # PDF
    pdf_url = None
    if export_pdf:
        page_sizes = {'a4': (210, 297), 'a3': (297, 420), 'a5': (148, 210), 'letter': (216, 279)}
        page_mm = page_sizes.get(pdf_page, (210, 297))
        pieces_for_pdf = []
        for key in sorted(converted.keys()):
            if key.startswith("file_piece_"):
                filename, content = converted[key]
                img = Image.open(io.BytesIO(content))
                pieces_for_pdf.append((img.width, img.height, content))
        if pieces_for_pdf:
            try:
                pdf_bytes = generate_pdf(pieces_for_pdf, page_mm, dpi=300)
                pdf_result = cloudinary.uploader.upload_large(
                    io.BytesIO(pdf_bytes),
                    public_id=f"user_{user_id}/{session_id}_print",
                    resource_type="raw", format="pdf"
                )
                pdf_url = pdf_result['secure_url']
            except Exception as e:
                print(f"PDF error: {e}")

    # Отправка пользователю
    try:
        if "file_combined" in cloudinary_urls:
            await bot.send_photo(
                chat_id=user_id,
                photo=cloudinary_urls["file_combined"],
                caption=f"📦 Общее изображение ({format_type.upper()})"
            )
        if zip_url:
            await bot.send_document(
                chat_id=user_id,
                document=zip_url,
                caption=f"🗜️ Все кусочки ({len([k for k in converted if k.startswith('file_piece_')])} файлов)"
            )
        if pdf_url:
            await bot.send_document(
                chat_id=user_id,
                document=pdf_url,
                caption=f"📄 PDF для печати ({pdf_page.upper()}, 300 DPI)"
            )
        pieces_urls = [cloudinary_urls[k] for k in sorted(converted.keys()) if k.startswith("file_piece_")]
        for i in range(0, len(pieces_urls), 10):
            batch = pieces_urls[i:i+10]
            media = [InputMediaPhoto(media=url) for url in batch]
            await bot.send_media_group(chat_id=user_id, media=media)

        await save_history(
            user_id, session_id,
            cloudinary_urls.get("file_combined", ""),
            len(pieces_urls), format_type
        )
        await increment_usage(user_id)

        gallery_id = None
        if publish_gallery and cloudinary_urls.get("file_combined"):
            await publish_to_gallery(
                user_id, session_id,
                cloudinary_urls["file_combined"],
                len(pieces_urls), preset_name
            )
            async with aiosqlite.connect("bot.db") as db:
                async with db.execute(
                    "SELECT id FROM gallery WHERE session_id=?", (session_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row: gallery_id = row[0]

        return web.json_response({
            "status": "ok",
            "message": "Файлы отправлены",
            "session_id": session_id,
            "gallery_id": gallery_id
        })
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

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

# ===== ЗАПУСК =====
# ===== ПУЛЕНЕПРОБИВАЕМЫЙ ЗАПУСК =====

async def health_handler(request):
    # Максимально простой ответ. Никаких запросов к Telegram API.
    # Если сервер отвечает, значит он жив.
    return web.json_response({"status": "ok"})

async def on_startup(app):
    await init_db()
    print("✅ База данных инициализирована")
    print(f"🌐 Mini App URL: {MINI_APP_URL}")
    
    # Проверку бота делаем отдельно. Если токен плохой, это не уронит healthcheck
    try:
        me = await bot.get_me()
        print(f"🤖 Бот @{me.username} успешно подключен к Telegram!")
    except Exception as e:
        print(f"⚠️ Ошибка подключения бота (проверьте BOT_TOKEN в Variables): {e}")

async def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_post("/upload", handle_upload)
    app.router.add_get("/gallery", handle_gallery)
    app.router.add_get("/health", health_handler)

    # ВАЖНО: Railway динамически назначает порт. Читаем его из окружения.
    port = int(os.environ.get("PORT", 8080))
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # ВАЖНО: '0.0.0.0' означает, что сервер слушает все сетевые интерфейсы
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🚀 Веб-сервер запущен и слушает порт {port}")

    # Запускаем бота. Это блокирующая операция, она работает "вечно"
    print("🔄 Запускаем polling бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())