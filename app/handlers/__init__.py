from app.handlers.activity import router as activity_router
from app.handlers.admin import router as admin_router
from app.handlers.advertising import router as advertising_router
from app.handlers.common import router as common_router
from app.handlers.leaderboard import router as leaderboard_router
from app.handlers.moderation import router as moderation_router
from app.handlers.reports import router as reports_router
from app.handlers.shop import router as shop_router
from app.handlers.verification import router as verification_router
from app.handlers.premium import router as premium_router
from app.handlers.group_controls import router as group_controls_router
from app.handlers.broadcasts import router as broadcasts_router
from app.handlers.subscriptions import router as subscriptions_router
from app.handlers.captcha_admin import router as captcha_admin_router

__all__ = ["activity_router", "admin_router", "advertising_router", "broadcasts_router", "captcha_admin_router", "common_router", "group_controls_router", "leaderboard_router", "moderation_router", "premium_router", "reports_router", "shop_router", "subscriptions_router", "verification_router"]
