from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import ErrorEvent
from sqlalchemy import text

from app.config import get_settings
from app.database.session import create_session_factory
from app.handlers import activity_router, admin_router, advertising_router, broadcasts_router, common_router, group_controls_router, leaderboard_router, moderation_router, premium_router, reports_router, shop_router, subscriptions_router, verification_router
from app.middlewares.database import DatabaseMiddleware
from app.middlewares.navigation import NavigationMiddleware
from app.middlewares.private_navigation import PrivateNavigationMiddleware
from app.middlewares.command_cleanup import CommandCleanupMiddleware
from app.middlewares.feature_gate import FeatureGateMiddleware
from app.middlewares.member_tracking import MemberTrackingMiddleware
from app.middlewares.premium_protection import PremiumProtectionMiddleware
from app.middlewares.required_subscription import RequiredSubscriptionMiddleware
from app.handlers.verification import recover_pending_verifications
from app.handlers.private_reports import router as private_reports_router
from app.handlers.announcements import router as announcements_router
from app.handlers.rewards import router as rewards_router
from app.handlers.fallbacks import router as fallbacks_router
from app.handlers.moderator_panel import router as moderator_panel_router
from app.services.broadcasts import start_broadcast_scheduler
from app.health_server import start_health_server


async def main() -> None:
    config = get_settings()
    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(PrivateNavigationMiddleware())
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher["config"] = config
    session_factory = create_session_factory(config.database_url)
    dispatcher["session_factory"] = session_factory
    # Fail at startup with a clear log instead of letting all DB-backed commands
    # look silent while a non-DB command such as /help still works.
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
    dispatcher.update.outer_middleware(NavigationMiddleware())
    dispatcher.update.outer_middleware(DatabaseMiddleware(session_factory))
    dispatcher.update.outer_middleware(RequiredSubscriptionMiddleware())
    dispatcher.update.outer_middleware(PremiumProtectionMiddleware())
    dispatcher.update.outer_middleware(CommandCleanupMiddleware())
    dispatcher.update.outer_middleware(MemberTrackingMiddleware())
    dispatcher.update.outer_middleware(FeatureGateMiddleware())
    await recover_pending_verifications(bot, session_factory)
    await start_broadcast_scheduler(bot, session_factory)

    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> bool:
        logging.getLogger(__name__).exception("Unhandled Telegram update", exc_info=event.exception)
        if event.update.message:
            try:
                await event.update.message.answer("Произошла ошибка. Попробуйте ещё раз.")
            except Exception:
                logging.getLogger(__name__).exception("Could not send error response")
        elif event.update.callback_query:
            try:
                await event.update.callback_query.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
            except Exception:
                logging.getLogger(__name__).exception("Could not answer failed callback")
        return True
    # Commands first; activity router catches only non-command group messages.
    dispatcher.include_routers(premium_router, subscriptions_router, group_controls_router, verification_router, rewards_router, common_router, moderator_panel_router, moderation_router, private_reports_router, reports_router, leaderboard_router, shop_router, advertising_router, broadcasts_router, announcements_router, admin_router, activity_router, fallbacks_router)
    health_runner = await start_health_server(config.port)
    await bot.delete_webhook(drop_pending_updates=False)
    logging.info("Starting ModerBot")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if health_runner:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
