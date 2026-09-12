from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.common import back_button
from app.services.features import feature_enabled
from app.services.rewards import claim_daily_reward
from app.services.users import upsert_user

router = Router(name='rewards')


async def reward_text(session, telegram_user):
    if not await feature_enabled(session, 'points'):
        return '⛔ Ежедневная награда недоступна, пока начисление очков отключено.'
    user = await upsert_user(session, telegram_user)
    reward, claimed = await claim_daily_reward(session, user)
    await session.commit()
    if claimed:
        return f'🎁 <b>ЕЖЕДНЕВНАЯ НАГРАДА</b>\n━━━━━━━━━━━━\n\n⭐ Получено: <b>+{reward.reward}</b> очков\n🔥 Серия: <b>{reward.streak} дн.</b>\n\nВозвращайтесь завтра: серия увеличивает бонус до седьмого дня.'
    return f'🕐 <b>НАГРАДА УЖЕ ПОЛУЧЕНА</b>\n━━━━━━━━━━━━\n\nСегодня: <b>+{reward.reward} ⭐</b>\n🔥 Серия: <b>{reward.streak} дн.</b>\n\nСледующая награда будет доступна завтра.'


@router.message(Command('daily'), F.chat.type == 'private')
async def daily_command(message: Message, session: AsyncSession):
    await message.answer(await reward_text(session, message.from_user), reply_markup=back_button('nav:private_main'))


@router.callback_query(F.data == 'daily:claim', F.message.chat.type == 'private')
async def daily_callback(callback: CallbackQuery, session: AsyncSession):
    await callback.answer()
    await callback.message.edit_text(await reward_text(session, callback.from_user), reply_markup=back_button('nav:private_main'))
