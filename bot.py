import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import PRODUCTS, get_product, load_config
from database import Database, RequestStatus
from gifts import GiftCatalog, deliver_gift

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("giftbot")

cfg = load_config()
db = Database(cfg.db_path)
catalog = GiftCatalog()

bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class GiftFlow(StatesGroup):
    waiting_product = State()
    waiting_confirmation = State()
    waiting_comment = State()


def product_choice_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p.title} — {p.star_count}⭐", callback_data=f"product:{p.code}")]
        for p in PRODUCTS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_decision_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{request_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request_id}"),
            ]
        ]
    )


WELCOME_TEXT = (
    f"👋 Добро пожаловать в магазин <b>{cfg.shop_name}</b>!\n\n"
    "Выберите товар, который вы оплатили на площадке:"
)

ASK_COMMENT_TEXT = (
    "✏️ Отправьте тот же комментарий, который вы указывали при оплате заказа.\n"
    "Это нужно для проверки заявки администратором."
)

PENDING_TEXT = (
    "⏳ <b>Ожидайте</b> — заявка отправлена администратору на проверку.\n"
    "Как только подарок будет подтверждён, я пришлю его прямо в этот чат."
)

ALREADY_PENDING_TEXT = (
    "⏳ У вас уже есть заявка на проверке. Дождитесь решения администратора — "
    "повторно отправлять комментарий не нужно."
)

REJECTED_USER_TEXT = (
    "❌ Заявка отклонена администратором.\n"
    "Если считаете это ошибкой — напишите в чат заказа на площадке, разберёмся."
)

DELIVERY_FAILED_ADMIN_TEXT = (
    "⚠️ Подарок не отправлен автоматически — у бота не хватает звёзд на балансе "
    "или gift_id не найден в каталоге. Пополните баланс бота через @PremiumBot и "
    "нажмите «Подтвердить» ещё раз."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(GiftFlow.waiting_product)
    await message.answer(WELCOME_TEXT, reply_markup=product_choice_keyboard())


@router.callback_query(GiftFlow.waiting_product, F.data.startswith("product:"))
async def choose_product(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.split(":", maxsplit=1)[1]
    product = get_product(code)

    if product is None:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    if await db.has_pending_request(callback.from_user.id):
        await callback.message.edit_text(ALREADY_PENDING_TEXT)
        await callback.answer()
        return

    await state.update_data(product_code=product.code)
    await state.set_state(GiftFlow.waiting_confirmation)

    await callback.message.edit_text(
        f"Вы выбрали: <b>{product.title}</b> ({product.star_count}⭐)\n\n"
        "Если вы уже оплатили заказ — напишите <b>1</b>, чтобы продолжить выдачу подарка."
    )
    await callback.answer()


@router.message(GiftFlow.waiting_confirmation, F.text == "1")
async def confirm_payment(message: Message, state: FSMContext) -> None:
    await state.set_state(GiftFlow.waiting_comment)
    await message.answer(ASK_COMMENT_TEXT)


@router.message(GiftFlow.waiting_confirmation)
async def confirm_payment_invalid(message: Message) -> None:
    await message.answer("Напишите <b>1</b>, если вы уже оплатили заказ на площадке.")


@router.message(GiftFlow.waiting_comment, F.text.len() > 0)
async def receive_comment(message: Message, state: FSMContext) -> None:
    comment = message.text.strip()
    user = message.from_user

    data = await state.get_data()
    product_code = data.get("product_code")
    product = get_product(product_code) if product_code else None

    if product is None:
        await message.answer("Сессия сброшена — начните заново через /start.")
        await state.clear()
        return

    request_id = await db.create_request(
        user_id=user.id, username=user.username, product_code=product.code, comment=comment
    )
    await state.clear()

    await message.answer(PENDING_TEXT)

    admin_text = (
        f"🆕 <b>Новая заявка #{request_id}</b>\n\n"
        f"🎁 Товар: {product.title} ({product.star_count}⭐)\n"
        f"👤 Покупатель: {user.full_name} (@{user.username or 'без username'})\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"💬 Комментарий: <code>{comment}</code>\n"
        f"🏪 Лот: {cfg.shop_url}"
    )
    await bot.send_message(cfg.admin_id, admin_text, reply_markup=admin_decision_keyboard(request_id))


@router.message(GiftFlow.waiting_comment)
async def receive_comment_empty(message: Message) -> None:
    await message.answer("Комментарий не может быть пустым. Отправьте текст комментария.")


@router.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
async def handle_admin_decision(callback: CallbackQuery) -> None:
    if callback.from_user.id != cfg.admin_id:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    action, raw_id = callback.data.split(":", maxsplit=1)
    request_id = int(raw_id)

    request = await db.get_request(request_id)
    if request is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if request["status"] != RequestStatus.PENDING.value:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    product = get_product(request["product_code"])
    user_id = request["user_id"]

    if action == "reject":
        await db.resolve_request(request_id, RequestStatus.REJECTED, callback.from_user.id)
        await bot.send_message(user_id, REJECTED_USER_TEXT)
        await callback.message.edit_text(f"{callback.message.html_text}\n\n<b>Статус: ❌ ОТКЛОНЁН</b>")
        await callback.answer("Отклонено.")
        return

    gift_id = catalog.resolve_gift_id(product.star_count) if product else None

    if gift_id is None:
        await callback.answer("Подарок не найден в каталоге Telegram.", show_alert=True)
        await bot.send_message(cfg.admin_id, DELIVERY_FAILED_ADMIN_TEXT)
        return

    try:
        await deliver_gift(
            bot,
            user_id=user_id,
            gift_id=gift_id,
            caption=f"🎁 Подарок от {cfg.shop_name}! Спасибо за покупку 💙",
        )
    except Exception as exc:
        logger.exception("Не удалось отправить подарок по заявке #%s", request_id)
        await callback.answer("Ошибка отправки подарка — см. лог.", show_alert=True)
        await bot.send_message(cfg.admin_id, f"⚠️ Ошибка sendGift по заявке #{request_id}: {exc}")
        return

    await db.resolve_request(request_id, RequestStatus.APPROVED, callback.from_user.id)
    await callback.message.edit_text(f"{callback.message.html_text}\n\n<b>Статус: ✅ ВЫДАН</b>")
    await callback.answer("Подарок отправлен.")


async def main() -> None:
    await db.init()
    await catalog.load(bot)
    logger.info("Gift bot started for %s", cfg.shop_name)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
