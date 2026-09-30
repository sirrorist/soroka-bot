"""Main entry point for the Soroka bot."""
import asyncio
import secrets
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from loguru import logger

from config import settings
from handlers import register_handlers
from database import init_db
from middleware import CommandLogMiddleware, LangMiddleware, RateLimitMiddleware, UserMiddleware
from utils.menu import set_default_menus

# Telegram присылает его в заголовке X-Telegram-Bot-Api-Secret-Token, апдейты без
# него отбрасываются: иначе любой, кто достучался до порта, подделывает апдейты
WEBHOOK_SECRET = settings.webhook_secret or secrets.token_urlsafe(32)


async def on_startup(bot: Bot) -> None:
    """Initialize on bot startup."""
    logger.info("Bot is starting up...")

    # Initialize database
    await init_db()
    await set_default_menus(bot)
    
    # Set webhook if configured
    if settings.webhook_url:
        await bot.set_webhook(
            url=f"{settings.webhook_url}/webhook",
            drop_pending_updates=True,
            secret_token=WEBHOOK_SECRET,
        )
        logger.info(f"Webhook set to {settings.webhook_url}/webhook")
    else:
        logger.info("Running in polling mode")


async def on_shutdown(bot: Bot) -> None:
    """Cleanup on bot shutdown."""
    logger.info("Bot is shutting down...")
    if settings.webhook_url:
        await bot.delete_webhook(drop_pending_updates=True)


def setup_logging():
    """Configure logging."""
    logger.remove()
    logger.add(
        "logs/bot_{time}.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )
    logger.add(
        lambda msg: print(msg, end=""),
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )


async def main():
    """Main function to run the bot."""
    setup_logging()
    
    # Initialize bot and dispatcher
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    
    # Register middleware
    # outer: пишет и те команды, которые ни один обработчик не взял
    dp.message.outer_middleware(CommandLogMiddleware())
    # язык - раньше всех: тексты нужны и middleware, и обработчикам
    dp.message.outer_middleware(LangMiddleware())
    dp.callback_query.outer_middleware(LangMiddleware())
    dp.message.middleware(UserMiddleware())
    dp.message.middleware(RateLimitMiddleware())
    dp.callback_query.middleware(UserMiddleware())
    
    # Register handlers
    register_handlers(dp)
    
    # Setup startup/shutdown handlers
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    if settings.webhook_url:
        # Webhook mode (for production)
        app = web.Application()
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=WEBHOOK_SECRET,
        )
        webhook_requests_handler.register(app, path="/webhook")
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        await site.start()
        
        logger.info(f"Starting webhook server on {settings.host}:{settings.port}")

        # Держим сервер запущенным
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()
    else:
        # Polling mode (for development)
        logger.info("Starting bot in polling mode...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
