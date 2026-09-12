"""Graceful recovery for buttons from old messages or interrupted FSM flows."""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.common import MAIN_MENU_TEXT, menu_links, user_feature_states
from app.keyboards.common import admin_menu, main_menu


router = Router(name='fallbacks')
log = logging.getLogger(__name__)


@router.callback_query(F.message.chat.type == 'private')
async def stale_private_button(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Settings) -> None:
    """Never leave a private inline button spinning when its state is gone."""
    value = callback.data or ''
    log.info('Recovering unhandled private callback %r from user %s', value, callback.from_user.id)
    await state.clear()
    await callback.answer('Эта кнопка устарела. Возвращаю в меню.')
    owner_flow = value.startswith(('admin:', 'broadcast:', 'announce:', 'purchase:', 'report:', 'private_report:'))
    if config.is_owner(callback.from_user.id) and owner_flow:
        text, markup = '👑 <b>СУПЕР-АДМИНКА</b>\n\nГлобальное управление ботом.', admin_menu()
    else:
        me = await callback.bot.get_me()
        group_url, channel_url = await menu_links(session, config)
        features = await user_feature_states(session)
        text = MAIN_MENU_TEXT
        markup = main_menu(config.is_owner(callback.from_user.id), bot_username=me.username, group_url=group_url, channel_url=channel_url, features=features)
    try:
        if callback.message.text:
            await callback.message.edit_text(text, reply_markup=markup)
        else:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(text, reply_markup=markup)
    except TelegramAPIError:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.message.chat.type.in_({'group', 'supergroup'}))
async def stale_group_button(callback: CallbackQuery) -> None:
    log.info('Ignoring unhandled group callback %r in chat %s', callback.data, callback.message.chat.id)
    await callback.answer('Кнопка устарела. Откройте команду заново.', show_alert=True)
