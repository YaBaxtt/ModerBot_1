from aiogram import BaseMiddleware
import asyncio
from app.keyboards.common import admin_menu, main_menu
from app.middlewares.private_navigation import navigation_context


class NavigationMiddleware(BaseMiddleware):
    """Authorize private owner flows and process cancellation before FSM input."""
    async def __call__(self, handler, update, data):
        callback = update.callback_query
        message = update.message
        config = data["config"]
        state = data.get("state")
        if callback:
            value = callback.data or ""
            if value.startswith(("admin:", "broadcast:", "announce:")):
                if not config.is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
                    await callback.answer("Этот раздел доступен владельцу в личных сообщениях.", show_alert=True)
                    return
            if (value.startswith(('menu:', 'admin:', 'moder:', 'groupcfg:', 'subcfg:', 'premium:', 'top:', 'daily:')) or value in {'nav:private_main', 'ads:start', 'shop:list', 'broadcast:start', 'announce:start'}) and state:
                await state.clear()
                data['raw_state'] = None
        if message and message.text and message.chat.type == "private":
            token = (message.text.split() or [''])[0].lower()
            command, _, mention = token.partition("@")
            own_command = not mention or mention == (await data["bot"].get_me()).username.lower()
            if command == "/cancel" and own_command:
                if data.get('raw_state') == 'PrivateReportForm:media':
                    return await handler(update, data)
                if state:
                    await state.clear()
                markup = admin_menu() if config.is_owner(message.from_user.id) else main_menu()
                await message.answer("Действие отменено.", reply_markup=markup)
                return
            if command in {"/start", "/admin", "/moder", "/report", "/help", "/me", "/profile", "/shop", "/top", "/rules"} and own_command and state:
                await state.clear()
                data['raw_state'] = None
        source = callback.message if callback else message
        token = None
        if source and source.chat.type == 'private':
            value = (callback.data or '') if callback else ''
            admin_flow = value.startswith(('admin:', 'broadcast:', 'announce:', 'purchase:', 'report:', 'private_report:review:', 'private_report:close:')) or (data.get('raw_state') or '').startswith(('RulesForm:', 'ShopCreate:', 'MenuLinkForm:', 'BroadcastForm:', 'AnnouncementForm:')) or (message and (message.text or '').split('@')[0] == '/admin')
            destination = 'admin:home' if admin_flow and config.is_owner((callback or message).from_user.id) else 'nav:private_main'
            token = navigation_context.set((asyncio.current_task(), source.chat.id, destination))
        try:
            return await handler(update, data)
        finally:
            if token is not None:
                navigation_context.reset(token)
