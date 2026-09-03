import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from database import Database
from handlers.admin import router as admin_router

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


async def on_startup(bot: Bot, db: Database) -> None:
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

    # Render.com'da web-service sifatida ishlasa ham port ochiq turishi kerak,
    # shuning uchun polling rejimida ham yengil health-server ko'taramiz.
    app = web.Application()
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health-check server %s portda ishga tushdi.", PORT)

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
