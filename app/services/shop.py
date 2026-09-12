from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PurchaseStatus, ShopItem, ShopPurchase, User


class PurchaseError(Exception):
    pass


async def create_purchase(session: AsyncSession, *, buyer_id: int, item_id: int, request_key: str | None = None) -> ShopPurchase:
    """Debit points and reserve stock in a single transaction-safe decision."""
    if request_key and await session.scalar(select(ShopPurchase.id).where(ShopPurchase.request_key == request_key)):
        raise PurchaseError("Эта покупка уже оформлена. Для новой покупки откройте /shop.")
    item = await session.get(ShopItem, item_id, with_for_update=True)
    if not item or not item.is_active:
        raise PurchaseError("Этот товар больше недоступен.")
    if item.stock is not None and item.stock < 1:
        raise PurchaseError("Товар закончился.")

    if item.price <= 0:
        raise PurchaseError("У товара некорректная цена. Обратитесь к владельцу.")
    async with session.begin_nested():
        reserved = await session.execute(update(ShopItem).where(
            ShopItem.id == item.id, ShopItem.is_active.is_(True),
            or_(ShopItem.stock.is_(None), ShopItem.stock > 0),
        ).values(stock=ShopItem.stock - 1))
        if reserved.rowcount != 1:
            raise PurchaseError("Товар закончился.")
        debit = await session.execute(
            update(User).where(User.id == buyer_id, User.points >= item.price).values(points=User.points - item.price)
        )
        if debit.rowcount != 1:
            raise PurchaseError("Недостаточно очков для покупки.")
        purchase = ShopPurchase(buyer_user_id=buyer_id, item_id=item.id, price_paid=item.price, request_key=request_key)
        session.add(purchase)
        await session.flush()
    return purchase


async def refund_purchase(session: AsyncSession, purchase_id: int) -> ShopPurchase | None:
    """Refund exactly once; a second callback observes an already closed purchase."""
    purchase = await session.get(ShopPurchase, purchase_id, with_for_update=True)
    if not purchase or purchase.status != PurchaseStatus.PENDING:
        return None
    claim = await session.execute(update(ShopPurchase).where(
        ShopPurchase.id == purchase_id, ShopPurchase.status == PurchaseStatus.PENDING,
    ).values(status=PurchaseStatus.REFUNDED, refunded_at=datetime.now(timezone.utc)))
    if claim.rowcount != 1:
        return None
    if purchase.buyer_user_id:
        await session.execute(update(User).where(User.id == purchase.buyer_user_id).values(points=User.points + purchase.price_paid))
    if purchase.item_id:
        await session.execute(update(ShopItem).where(ShopItem.id == purchase.item_id, ShopItem.stock.is_not(None)).values(stock=ShopItem.stock + 1))
    return purchase
