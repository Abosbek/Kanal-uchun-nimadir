"""
main.py
Telegram Channel Manager AI botini ishga tushiruvchi asosiy fayl.

- Polling yoki Webhook rejimida ishlashi mumkin (RUN_MODE env orqali).
- Render.com'da 24/7 uxlab qolmasligi uchun aiohttp orqali /health endpoint ochadi
  (UptimeRobot shu manzilga muntazam so'rov yuborib turadi).
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from database import Database
from handlers.admin import router as admin_router, publish_post_to_channel, send_daily_draft
import ai_service
import image_service
from database import ImageSourceType

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot_local.db")
RUN_MODE = os.getenv("RUN_MODE", "polling").lower()  # "polling" | "webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN topilmadi. .env faylini tekshiring.")
    sys.exit(1)


async def health_check(request: web.Request) -> web.Response:
    """UptimeRobot va Render uchun oddiy health-check endpoint."""
    return web.json_response({"status": "ok", "service": "telegram-channel-manager-bot"})


async def self_ping_loop() -> None:
    """
    Botni Render'ning bepul tarifida 24/7 uyg'oq ushlab turish uchun,
    bot o'zining ochiq /health manzilini har necha daqiqada bir marta
    o'zi so'raydi. Bu UptimeRobot kabi tashqi xizmatga qo'shimcha,
    ikkinchi ("zaxira") mudofaa qatlami sifatida ishlaydi.

    Render web-service uchun avtomatik RENDER_EXTERNAL_URL muhit
    o'zgaruvchisini beradi — shuning uchun qo'shimcha sozlash shart emas.
    """
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not external_url:
        logger.info("RENDER_EXTERNAL_URL topilmadi, self-ping o'chirilgan (masalan lokal ishga tushirishda normal holat).")
        return

    ping_url = f"{external_url}/health"
    interval_seconds = int(os.getenv("SELF_PING_INTERVAL_SECONDS", "600"))  # standart: 10 daqiqa

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    logger.info("Self-ping yuborildi (%s) -> status %s", ping_url, resp.status)
            except Exception as e:
                logger.warning("Self-ping muvaffaqiyatsiz bo'ldi: %s", e)


async def scheduler_loop(bot: Bot, db: Database) -> None:
    """
    Har 60 soniyada bazani tekshirib, vaqti kelgan rejalashtirilgan postlarni
    avtomatik ravishda kanalga chop etadi.
    """
    while True:
        await asyncio.sleep(60)
        try:
            now_utc = datetime.now(timezone.utc)
            due_posts = await db.get_due_scheduled_posts(now_utc)
            for post in due_posts:
                success, info = await publish_post_to_channel(bot, db, post.id)
                if success:
                    logger.info("Rejalashtirilgan post #%s muvaffaqiyatli chop etildi.", post.id)
                else:
                    logger.warning("Rejalashtirilgan post #%s chop etilmadi: %s", post.id, info)
        except Exception as e:
            logger.exception("Scheduler loop'da xatolik: %s", e)


async def daily_content_loop(bot: Bot, db: Database) -> None:
    """
    Har kuni admin belgilagan vaqtda (Toshkent vaqti bo'yicha) avtomatik ravishda:
    - DAILY_NEWS_COUNT ta eng yangi/qiziqarli texnologik yangilik (RSS orqali)
    - DAILY_AI_COUNT ta AI tomonidan yaratilgan qiziqarli post (turli mavzular)
    tayyorlab, har birini admin(lar)ga moderatsiya uchun yuboradi.
    AI'ni haddan tashqari yuklamaslik uchun postlar orasida DAILY_POST_GAP_SECONDS kutiladi.
    """
    daily_time_str = os.getenv("DAILY_POST_TIME", "09:00")
    try:
        target_hour, target_minute = map(int, daily_time_str.strip().split(":"))
    except ValueError:
        logger.warning("DAILY_POST_TIME formati noto'g'ri, standart 09:00 ishlatiladi.")
        target_hour, target_minute = 9, 0

    tz_offset = int(os.getenv("ADMIN_TIMEZONE_OFFSET_HOURS", "5"))
    gap_seconds = int(os.getenv("DAILY_POST_GAP_SECONDS", "300"))
    news_count = int(os.getenv("DAILY_NEWS_COUNT", "5"))
    ai_count = int(os.getenv("DAILY_AI_COUNT", "5"))

    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
    if not admin_ids:
        logger.warning("ADMIN_IDS topilmadi — kunlik avtomatik postlar o'chirilgan.")
        return
    primary_admin_id = admin_ids[0]

    admin_tz = timezone(timedelta(hours=tz_offset))
    last_run_date = None

    logger.info(
        "Kunlik avtomatik post generatsiyasi yoqildi: har kuni soat %02d:%02d (Toshkent vaqti).",
        target_hour, target_minute,
    )

    while True:
        await asyncio.sleep(30)
        now_local = datetime.now(admin_tz)
        if (
            now_local.hour == target_hour
            and now_local.minute == target_minute
            and last_run_date != now_local.date()
        ):
            last_run_date = now_local.date()
            logger.info("Kunlik avtomatik post generatsiyasi boshlandi...")
            try:
                await _run_daily_generation(bot, db, primary_admin_id, news_count, ai_count, gap_seconds)
            except Exception as e:
                logger.exception("Kunlik generatsiyada xatolik: %s", e)
            logger.info("Kunlik avtomatik post generatsiyasi yakunlandi.")


async def _run_daily_generation(
    bot: Bot, db: Database, admin_chat_id: int, news_count: int, ai_count: int, gap_seconds: int
) -> None:
    is_first_post = True

    # 1) Eng so'nggi texnologik yangiliklar (RSS + rasm sifatida asl maqola rasmi)
    news_items = await ai_service.get_daily_news_items(news_count)
    for item in news_items:
        if not is_first_post:
            await asyncio.sleep(gap_seconds)
        is_first_post = False

        try:
            content = await ai_service.generate_post_from_rss_item(item)
        except Exception as e:
            logger.exception("Yangilik postini yaratishda xatolik: %s", e)
            continue

        image_bytes = None
        image_source = ImageSourceType.NONE
        try:
            og_image_url = await ai_service.fetch_og_image(item.link)
            if og_image_url:
                image_bytes = await image_service.download_image_bytes(og_image_url)
                image_source = ImageSourceType.REAL_SEARCH
        except Exception as e:
            logger.warning("Yangilik uchun rasm topilmadi: %s", e)

        await send_daily_draft(
            bot, db, admin_chat_id, content,
            source_type="rss", source_ref=item.link,
            auto_image_bytes=image_bytes, auto_image_source=image_source,
        )

    # 2) AI tomonidan yaratilgan qiziqarli postlar (turli mavzular)
    topics = ai_service.get_daily_ai_topics(ai_count)
    for topic in topics:
        if not is_first_post:
            await asyncio.sleep(gap_seconds)
        is_first_post = False

        try:
            content = await ai_service.generate_post_from_topic(topic)
        except Exception as e:
            logger.exception("AI mavzu postini yaratishda xatolik: %s", e)
            continue

        image_bytes = None
        image_source = ImageSourceType.NONE
        try:
            image_bytes = await image_service.generate_ai_image(topic)
            image_source = ImageSourceType.AI_GENERATED
        except Exception as e:
            logger.warning("AI post uchun rasm yaratilmadi: %s", e)

        await send_daily_draft(
            bot, db, admin_chat_id, content,
            source_type="topic", source_ref=topic,
            auto_image_bytes=image_bytes, auto_image_source=image_source,
        )


async def on_startup(bot: Bot, db: Database) -> None:
    # Jadvallar allaqachon run_polling/run_webhook ichida yaratilgan bo'ladi,
    # lekin ehtiyot uchun bu yerda ham tekshirib qo'yamiz (idempotent amal).
    await db.init_models()
    if RUN_MODE == "webhook" and WEBHOOK_URL:
        full_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(full_url, drop_pending_updates=True)
        logger.info("Webhook o'rnatildi: %s", full_url)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot polling rejimida ishga tushmoqda.")


async def on_shutdown(bot: Bot, db: Database) -> None:
    logger.info("Bot to'xtatilmoqda...")
    await db.close()
    await bot.session.close()


def create_dispatcher(db: Database) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(admin_router)

    # Barcha handlerlarga avtomatik uzatiladigan umumiy ma'lumotlar (dependency injection)
    dp["db"] = db

    dp.startup.register(lambda bot: on_startup(bot, db))
    dp.shutdown.register(lambda bot: on_shutdown(bot, db))
    return dp


async def run_polling() -> None:
    db = Database(DATABASE_URL)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dispatcher(db)

    # KRITIK: jadvallar polling boshlanishidan OLDIN, aniq va kutilgan holda yaratiladi.
    # dp.startup hodisasiga tayanmaymiz, chunki ba'zi muhitlarda uning tugashi
    # kafolatlanmasligi mumkin va bu "no such table" xatosiga olib kelishi mumkin.
    await db.init_models()
    logger.info("Ma'lumotlar bazasi jadvallari tayyor (init_models bajarildi).")

    # Render.com'da web-service sifatida ishlasa ham port ochiq turishi kerak,
    # shuning uchun polling rejimida ham yengil health-server ko'taramiz.
    app = web.Application()
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health-check server %s portda ishga tushdi.", PORT)

    # Fon rejimida self-ping va rejalashtirilgan postlar vazifalarini ishga tushiramiz
    asyncio.create_task(self_ping_loop())
    asyncio.create_task(scheduler_loop(bot, db))
    asyncio.create_task(daily_content_loop(bot, db))

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


async def run_webhook() -> None:
    db = Database(DATABASE_URL)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dispatcher(db)

    # KRITIK: jadvallar server ishga tushishidan OLDIN aniq yaratiladi (xuddi polling rejimida kabi).
    await db.init_models()
    logger.info("Ma'lumotlar bazasi jadvallari tayyor (init_models bajarildi).")

    app = web.Application()
    app.router.add_get("/health", health_check)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Webhook server %s portda ishga tushdi (yo'l: %s).", PORT, WEBHOOK_PATH)

    # Fon rejimida self-ping va rejalashtirilgan postlar vazifalarini ishga tushiramiz
    asyncio.create_task(self_ping_loop())
    asyncio.create_task(scheduler_loop(bot, db))
    asyncio.create_task(daily_content_loop(bot, db))

    # Server abadiy ishlab tursin
    await asyncio.Event().wait()


async def main() -> None:
    if RUN_MODE == "webhook":
        if not WEBHOOK_URL:
            logger.critical("RUN_MODE=webhook tanlangan, lekin WEBHOOK_URL bo'sh.")
            sys.exit(1)
        await run_webhook()
    else:
        await run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
