import asyncio
import logging
import secrets
import string

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
    LabeledPrice,
    Message,
    PreCheckoutQuery,
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
bot_username: str = ""

bot = Bot(token=cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ── States ────────────────────────────────────────────────────────────────────

class GiftFlow(StatesGroup):
    waiting_comment = State()
    waiting_donate_amount = State()


class AdminFlow(StatesGroup):
    waiting_broadcast = State()
    waiting_find_user = State()


# ── Helpers ───────────────────────────────────────────────────────────────────

def gen_code(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔗 Создать ссылку", callback_data="adm:create_link"),
            InlineKeyboardButton(text="📋 Мои ссылки", callback_data="adm:my_links"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
            InlineKeyboardButton(text="📈 За сегодня", callback_data="adm:today"),
        ],
        [
            InlineKeyboardButton(text="📅 За неделю", callback_data="adm:week"),
            InlineKeyboardButton(text="💰 Топ донатеров", callback_data="adm:top_donors"),
        ],
        [
            InlineKeyboardButton(text="⏳ Ожидающие", callback_data="adm:pending"),
            InlineKeyboardButton(text="✅ Выданные", callback_data="adm:approved"),
        ],
        [
            InlineKeyboardButton(text="❌ Отклонённые", callback_data="adm:rejected"),
            InlineKeyboardButton(text="🤖 Авто-выданные", callback_data="adm:auto"),
        ],
        [
            InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm:all_users"),
            InlineKeyboardButton(text="👤 Найти юзера", callback_data="adm:find_user"),
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast"),
            InlineKeyboardButton(text="💎 Запрос доната всем", callback_data="adm:donate_all"),
        ],
        [
            InlineKeyboardButton(text="🎁 Список товаров", callback_data="adm:products"),
            InlineKeyboardButton(text="🔄 Обновить каталог", callback_data="adm:reload_catalog"),
        ],
        [
            InlineKeyboardButton(text="🧹 Очистить ожидающие", callback_data="adm:clear_pending"),
            InlineKeyboardButton(text="🔗 Статус ссылок", callback_data="adm:link_stats"),
        ],
        [
            InlineKeyboardButton(text="🎁 Вкл/Выкл подарки", callback_data="adm:toggle_gifts"),
            InlineKeyboardButton(text="🔔 Вкл/Выкл уведом.", callback_data="adm:toggle_notif"),
        ],
        [
            InlineKeyboardButton(text="💬 Вкл/Выкл вопрос доната", callback_data="adm:toggle_donate_ask"),
            InlineKeyboardButton(text="⚙️ Все настройки", callback_data="adm:settings"),
        ],
    ])


def product_link_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{p.title} — {p.star_count}⭐",
            callback_data=f"adm:genlink:{p.code}"
        )]
        for p in PRODUCTS
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в панель", callback_data="adm:back")]
    ])


def donate_ask_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❤️ Да, помогу!", callback_data="donate:yes"),
            InlineKeyboardButton(text="Нет, спасибо", callback_data="donate:no"),
        ]
    ])


def donate_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ 15 звёзд", callback_data="donate:amount:15"),
            InlineKeyboardButton(text="⭐ 50 звёзд", callback_data="donate:amount:50"),
        ],
        [InlineKeyboardButton(text="💎 Своя сумма", callback_data="donate:amount:custom")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="donate:no")],
    ])


def admin_decision_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{request_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request_id}"),
        ]
    ])


COMPLETION_TEXT = (
    "✅ <b>Заказ выполнен!</b>\n"
    "Зайдите на сайт и подтвердите получение\n"
    "Спасибо за покупку!\n"
    "❤️Если не сложно, подтверди выполнение и оставь отзыв.\n\n"
    "Буду рад видеть тебя снова! ☘️"
)

NOT_PURCHASED_TEXT = (
    "❌ <b>Вы не приобрели товар.</b>\n\n"
    "Чтобы получить подарок, сначала оформите заказ на сайте:\n"
    f"👉 <a href='https://starvell.com/offers/233831'>Купить подарок</a>\n\n"
    "После оплаты продавец отправит вам специальную ссылку для получения."
)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.first_name, user.last_name)

    # Admin panel
    if user.id == cfg.admin_id:
        gifts_on = await db.is_gifts_enabled()
        status_icon = "✅" if gifts_on else "❌"
        await message.answer(
            f"👋 Привет, администратор!\n\n"
            f"🎁 Приём подарков: <b>{status_icon} {'включён' if gifts_on else 'выключен'}</b>\n\n"
            f"Выберите действие:",
            reply_markup=admin_main_keyboard(),
        )
        return

    # Deep link?
    payload = message.text.split(maxsplit=1)
    if len(payload) > 1 and payload[1].startswith("gl_"):
        code = payload[1][3:]
        link = await db.get_gift_link(code)

        if link is None or link["used"]:
            await message.answer("❌ Эта ссылка уже использована или недействительна.")
            return

        if not await db.is_gifts_enabled():
            await message.answer("⏸ Приём подарков временно приостановлен. Попробуйте позже.")
            return

        product = get_product(link["product_code"])
        if product is None:
            await message.answer("❌ Товар не найден.")
            return

        await state.update_data(link_code=code, product_code=product.code)
        await state.set_state(GiftFlow.waiting_comment)
        await message.answer(
            f"🎉 Привет, <b>{user.first_name}</b>!\n\n"
            f"Ваш заказ: <b>{product.title}</b> ({product.star_count}⭐)\n\n"
            "✏️ Какой комментарий написать к подарку?\n"
            "<i>Например: «С днём рождения!» или «Спасибо!»</i>"
        )
        return

    # Regular user without link
    await message.answer(NOT_PURCHASED_TEXT, disable_web_page_preview=True)


# ── Gift comment flow ─────────────────────────────────────────────────────────

@router.message(GiftFlow.waiting_comment, F.text)
async def receive_comment(message: Message, state: FSMContext) -> None:
    comment = message.text.strip()
    if not comment:
        await message.answer("Комментарий не может быть пустым. Введите текст.")
        return

    data = await state.get_data()
    link_code = data.get("link_code")
    product_code = data.get("product_code")
    product = get_product(product_code) if product_code else None
    user = message.from_user

    if product is None:
        await message.answer("❌ Сессия устарела. Пройдите по ссылке заново.")
        await state.clear()
        return

    await state.clear()

    # Mark link as used
    await db.use_gift_link(link_code, user.id)

    # Loading animation
    loading_msg = await message.answer("⏳ <b>Обрабатываем ваш заказ...</b>")
    await asyncio.sleep(1)
    await loading_msg.edit_text("⏳ <b>Отправляем подарок...</b>")

    # Resolve gift id
    gift_id = catalog.resolve_gift_id(product.star_count)

    # Save request
    request_id = await db.create_request(
        user_id=user.id,
        username=user.username,
        product_code=product.code,
        comment=comment,
        link_code=link_code,
        status=RequestStatus.AUTO,
    )

    if gift_id is None:
        await loading_msg.edit_text(
            "⚠️ Не удалось найти подарок в каталоге Telegram. "
            "Обратитесь к продавцу — он решит вопрос вручную."
        )
        notif_on = (await db.get_setting("notifications_enabled")) == "1"
        if notif_on:
            await bot.send_message(
                cfg.admin_id,
                f"⚠️ <b>Авто-выдача не удалась</b> (нет gift_id)\n\n"
                f"📦 Заявка #{request_id}\n"
                f"🎁 Товар: {product.title} ({product.star_count}⭐)\n"
                f"👤 @{user.username or user.full_name} (ID: <code>{user.id}</code>)\n"
                f"💬 Комментарий: <code>{comment}</code>",
                reply_markup=admin_decision_keyboard(request_id),
            )
        return

    # Deliver gift
    try:
        await deliver_gift(bot, user_id=user.id, gift_id=gift_id, caption=comment)
    except Exception as exc:
        logger.exception("Ошибка отправки подарка #%s", request_id)
        await loading_msg.edit_text("⚠️ Ошибка при отправке подарка. Обратитесь к продавцу.")
        notif_on = (await db.get_setting("notifications_enabled")) == "1"
        if notif_on:
            await bot.send_message(
                cfg.admin_id,
                f"⚠️ <b>Ошибка sendGift</b> по заявке #{request_id}: {exc}\n"
                f"👤 @{user.username or user.full_name} (ID: <code>{user.id}</code>)\n"
                f"🎁 {product.title}, комментарий: <code>{comment}</code>",
                reply_markup=admin_decision_keyboard(request_id),
            )
        return

    await db.increment_gifts_received(user.id)

    # Success message
    await loading_msg.edit_text(COMPLETION_TEXT)

    # Admin log
    notif_on = (await db.get_setting("notifications_enabled")) == "1"
    if notif_on:
        await bot.send_message(
            cfg.admin_id,
            f"✅ <b>Подарок выдан автоматически</b>\n\n"
            f"📦 Заявка #{request_id}\n"
            f"🎁 {product.title} ({product.star_count}⭐)\n"
            f"👤 @{user.username or user.full_name} (ID: <code>{user.id}</code>)\n"
            f"💬 Комментарий: <code>{comment}</code>",
        )

    # Donation ask
    donate_ask_on = (await db.get_setting("donation_ask_enabled")) == "1"
    if donate_ask_on:
        await asyncio.sleep(1)
        await message.answer(
            "💙 Не хотите помочь моему проекту?\n"
            "Это займёт всего секунду и очень поддержит развитие!",
            reply_markup=donate_ask_keyboard(),
        )


# ── Donation flow ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "donate:yes")
async def donate_yes(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "❤️ Спасибо! Выберите сумму поддержки:",
        reply_markup=donate_amount_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "donate:no")
async def donate_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Хорошо! Спасибо за покупку ☘️")
    await callback.answer()


@router.callback_query(F.data.startswith("donate:amount:"))
async def donate_amount(callback: CallbackQuery, state: FSMContext) -> None:
    amount_str = callback.data.split(":")[-1]
    if amount_str == "custom":
        await state.set_state(GiftFlow.waiting_donate_amount)
        await callback.message.edit_text(
            "💎 Введите сумму в звёздах (целое число, минимум 1):"
        )
        await callback.answer()
        return

    amount = int(amount_str)
    await _send_donation_invoice(callback.message, callback.from_user.id, amount)
    await callback.answer()


@router.message(GiftFlow.waiting_donate_amount, F.text)
async def receive_donate_amount(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        amount = int(message.text.strip())
        if amount < 1:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число больше 0.")
        return
    await _send_donation_invoice(message, message.from_user.id, amount)


async def _send_donation_invoice(message: Message, user_id: int, amount: int) -> None:
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="❤️ Поддержать RiyoShop",
            description=f"Добровольное пожертвование {amount}⭐ на развитие магазина",
            payload=f"donation_{amount}",
            currency="XTR",
            prices=[LabeledPrice(label="Поддержка", amount=amount)],
        )
        await message.answer("🧾 Счёт отправлен! Спасибо за поддержку ❤️")
    except Exception as e:
        logger.exception("Ошибка отправки инвойса: %s", e)
        await message.answer("⚠️ Не удалось создать счёт. Попробуйте позже.")


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    amount = message.successful_payment.total_amount
    payload = message.successful_payment.invoice_payload
    user = message.from_user

    await db.record_donation(user.id, amount, payload)
    await message.answer(f"🌟 Получили {amount}⭐ — огромное спасибо! Ты лучший! ❤️")

    notif_on = (await db.get_setting("notifications_enabled")) == "1"
    if notif_on:
        await bot.send_message(
            cfg.admin_id,
            f"💰 <b>Новый донат!</b>\n"
            f"👤 @{user.username or user.full_name} (ID: <code>{user.id}</code>)\n"
            f"⭐ Сумма: <b>{amount} звёзд</b>",
        )


# ── Admin callbacks ───────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id == cfg.admin_id


@router.callback_query(F.data == "adm:back")
async def adm_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    gifts_on = await db.is_gifts_enabled()
    icon = "✅" if gifts_on else "❌"
    await callback.message.edit_text(
        f"👋 Панель администратора\n\n"
        f"🎁 Приём подарков: <b>{icon} {'включён' if gifts_on else 'выключен'}</b>",
        reply_markup=admin_main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:create_link")
async def adm_create_link(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔗 Выберите товар для генерации ссылки:",
        reply_markup=product_link_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:genlink:"))
async def adm_gen_link(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    product_code = callback.data.split(":")[-1]
    product = get_product(product_code)
    if product is None:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    code = gen_code()
    await db.create_gift_link(code, product_code, callback.from_user.id)

    link = f"https://t.me/{bot_username}?start=gl_{code}"
    await callback.message.edit_text(
        f"✅ <b>Ссылка создана!</b>\n\n"
        f"🎁 Товар: {product.title} ({product.star_count}⭐)\n\n"
        f"🔗 <code>{link}</code>\n\n"
        f"<i>Одноразовая ссылка. Пользователь перейдёт → введёт комментарий → получит подарок автоматически.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Ещё ссылку", callback_data="adm:create_link")],
            [InlineKeyboardButton(text="◀️ В панель", callback_data="adm:back")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    s = await db.get_stats()
    links_total, links_used = await db.get_link_count()
    text = (
        "📊 <b>Общая статистика</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b>\n\n"
        f"📦 Заявок всего: <b>{s['total']}</b>\n"
        f"  ⏳ Ожидают: <b>{s['pending']}</b>\n"
        f"  ✅ Одобрено: <b>{s['approved']}</b>\n"
        f"  🤖 Авто-выдано: <b>{s['auto']}</b>\n"
        f"  ❌ Отклонено: <b>{s['rejected']}</b>\n\n"
        f"🔗 Ссылок создано: <b>{links_total}</b> (использовано: {links_used})\n\n"
        f"💰 Донатов получено: <b>{s['donation_count']}</b> на <b>{s['total_donated']}⭐</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:today")
async def adm_today(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    s = await db.get_period_stats(1)
    text = (
        "📈 <b>Статистика за сегодня</b>\n\n"
        f"📦 Заявок: <b>{s['total']}</b>\n"
        f"  ✅ Одобрено: <b>{s['approved']}</b>\n"
        f"  🤖 Авто: <b>{s['auto']}</b>\n"
        f"  ❌ Отклонено: <b>{s['rejected']}</b>\n"
        f"👤 Новых пользователей: <b>{s['new_users']}</b>\n"
        f"💰 Донатов: <b>{s['donated']}⭐</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:week")
async def adm_week(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    s = await db.get_period_stats(7)
    text = (
        "📅 <b>Статистика за 7 дней</b>\n\n"
        f"📦 Заявок: <b>{s['total']}</b>\n"
        f"  ✅ Одобрено: <b>{s['approved']}</b>\n"
        f"  🤖 Авто: <b>{s['auto']}</b>\n"
        f"  ❌ Отклонено: <b>{s['rejected']}</b>\n"
        f"👤 Новых пользователей: <b>{s['new_users']}</b>\n"
        f"💰 Донатов: <b>{s['donated']}⭐</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:top_donors")
async def adm_top_donors(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    donors = await db.get_top_donors(10)
    if not donors:
        text = "💰 <b>Топ донатеров</b>\n\nДонатов пока нет."
    else:
        lines = ["💰 <b>Топ донатеров</b>\n"]
        medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 10
        for i, d in enumerate(donors):
            uname = f"@{d['username']}" if d["username"] else d["first_name"]
            lines.append(f"{medals[i]} {uname} — <b>{d['total_donated']}⭐</b> ({d['donation_count']} донатов)")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data.in_({"adm:pending", "adm:approved", "adm:rejected", "adm:auto"}))
async def adm_requests(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    status_map = {
        "adm:pending": ("pending", "⏳ Ожидающие заявки"),
        "adm:approved": ("approved", "✅ Одобренные заявки"),
        "adm:rejected": ("rejected", "❌ Отклонённые заявки"),
        "adm:auto": ("auto", "🤖 Авто-выданные"),
    }
    status, title = status_map[callback.data]
    requests = await db.get_requests_by_status(status, limit=10)

    if not requests:
        text = f"{title}\n\nЗаявок нет."
    else:
        lines = [f"<b>{title}</b> (последние {len(requests)})\n"]
        for r in requests:
            uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
            product = get_product(r["product_code"])
            p_str = product.title if product else r["product_code"]
            lines.append(f"#{r['id']} | {uname} | {p_str} | <code>{r['comment'][:30]}</code>")
        text = "\n".join(lines)

    kb = back_keyboard()
    if status == "pending" and requests:
        rows = [[
            InlineKeyboardButton(text=f"✅ #{r['id']}", callback_data=f"approve:{r['id']}"),
            InlineKeyboardButton(text=f"❌ #{r['id']}", callback_data=f"reject:{r['id']}"),
        ] for r in requests]
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:all_users")
async def adm_all_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    users = await db.get_all_users()
    total = len(users)
    if not users:
        text = "👥 Пользователей пока нет."
    else:
        lines = [f"👥 <b>Пользователи</b> (всего: {total})\n"]
        for u in users[:15]:
            uname = f"@{u['username']}" if u["username"] else u["first_name"]
            lines.append(
                f"• {uname} | 🎁{u['total_gifts_received']} | 💰{u['total_donated']}⭐"
            )
        if total > 15:
            lines.append(f"\n<i>...и ещё {total - 15}</i>")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:find_user")
async def adm_find_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.waiting_find_user)
    await callback.message.edit_text(
        "👤 Введите Telegram ID пользователя:",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_find_user, F.text)
async def adm_find_user_result(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Введите числовой ID.")
        return

    user = await db.get_user(uid)
    if not user:
        await message.answer("Пользователь не найден.", reply_markup=back_keyboard())
        return

    uname = f"@{user['username']}" if user["username"] else user["first_name"]
    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{user['id']}</code>\n"
        f"Имя: {uname}\n"
        f"Первый визит: {user['first_seen'][:10]}\n"
        f"Последний визит: {user['last_active'][:10]}\n"
        f"Подарков получено: <b>{user['total_gifts_received']}</b>\n"
        f"Задонатил: <b>{user['total_donated']}⭐</b>"
    )
    await message.answer(text, reply_markup=back_keyboard())


@router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.waiting_broadcast)
    await callback.message.edit_text(
        "📢 Введите текст рассылки (HTML поддерживается).\n"
        "Будет отправлено всем пользователям бота:",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_broadcast, F.text)
async def adm_broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    users = await db.get_all_users()
    text = message.text

    sent = 0
    failed = 0
    status_msg = await message.answer(f"📢 Рассылка началась... 0/{len(users)}")

    for i, user in enumerate(users):
        try:
            await bot.send_message(user["id"], text)
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % 10 == 0:
            try:
                await status_msg.edit_text(f"📢 Рассылка... {i+1}/{len(users)}")
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"📢 <b>Рассылка завершена</b>\n\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        reply_markup=back_keyboard(),
    )


@router.callback_query(F.data == "adm:donate_all")
async def adm_donate_all(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    users = await db.get_all_users()
    sent = 0
    for user in users:
        if user["id"] == cfg.admin_id:
            continue
        try:
            await bot.send_message(
                user["id"],
                "💙 Привет! Если нравится наш магазин — поддержи проект звёздами!\n"
                "Это займёт секунду и очень поможет развитию 🚀",
                reply_markup=donate_ask_keyboard(),
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await callback.message.edit_text(
        f"💎 Запрос доната отправлен {sent} пользователям.",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:products")
async def adm_products(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    lines = ["🎁 <b>Список товаров</b>\n"]
    for p in PRODUCTS:
        gift_id = catalog.resolve_gift_id(p.star_count)
        status = f"✅ gift_id: <code>{gift_id}</code>" if gift_id else "❌ Не найден в каталоге"
        lines.append(f"• {p.title} ({p.star_count}⭐) — {status}")
    await callback.message.edit_text("\n".join(lines), reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:reload_catalog")
async def adm_reload_catalog(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await callback.answer("Обновляю каталог...")
    try:
        await catalog.load(bot)
        await callback.message.edit_text(
            f"🔄 Каталог обновлён!\n\nДоступно подарков: {len(catalog._by_star_count)}",
            reply_markup=back_keyboard(),
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка обновления каталога: {e}",
            reply_markup=back_keyboard(),
        )


@router.callback_query(F.data == "adm:clear_pending")
async def adm_clear_pending(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    count = await db.clear_pending()
    await callback.message.edit_text(
        f"🧹 Удалено ожидающих заявок: <b>{count}</b>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:link_stats")
async def adm_link_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    total, used = await db.get_link_count()
    unused = total - used
    await callback.message.edit_text(
        f"🔗 <b>Статистика ссылок</b>\n\n"
        f"Создано всего: <b>{total}</b>\n"
        f"Использовано: <b>{used}</b>\n"
        f"Свободных: <b>{unused}</b>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:my_links")
async def adm_my_links(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    links = await db.get_admin_links(callback.from_user.id, limit=10)
    if not links:
        text = "🔗 Вы ещё не создавали ссылок."
    else:
        lines = [f"🔗 <b>Последние ссылки</b> (до 10)\n"]
        for lnk in links:
            product = get_product(lnk["product_code"])
            p_name = product.title if product else lnk["product_code"]
            status = "✅ Использована" if lnk["used"] else "⏳ Свободна"
            code = lnk["code"]
            lines.append(
                f"• {p_name} | {status}\n"
                f"  <code>https://t.me/{bot_username}?start=gl_{code}</code>"
            )
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:toggle_gifts")
async def adm_toggle_gifts(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    now_on = await db.toggle_gifts()
    icon = "✅" if now_on else "❌"
    state_text = "включён" if now_on else "выключен"
    await callback.message.edit_text(
        f"🎁 Приём подарков теперь <b>{icon} {state_text}</b>",
        reply_markup=back_keyboard(),
    )
    await callback.answer(f"Подарки {state_text}")


@router.callback_query(F.data == "adm:toggle_notif")
async def adm_toggle_notif(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    now_on = await db.toggle_notifications()
    state_text = "включены" if now_on else "выключены"
    await callback.message.edit_text(
        f"🔔 Уведомления теперь <b>{'✅' if now_on else '❌'} {state_text}</b>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:toggle_donate_ask")
async def adm_toggle_donate_ask(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    now_on = await db.toggle_donation_ask()
    state_text = "включён" if now_on else "выключен"
    await callback.message.edit_text(
        f"💬 Вопрос о донате после выдачи теперь <b>{'✅' if now_on else '❌'} {state_text}</b>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    gifts_on = await db.is_gifts_enabled()
    notif_on = (await db.get_setting("notifications_enabled")) == "1"
    donate_ask_on = (await db.get_setting("donation_ask_enabled")) == "1"

    text = (
        "⚙️ <b>Настройки бота</b>\n\n"
        f"🎁 Приём подарков: {'✅ Вкл' if gifts_on else '❌ Выкл'}\n"
        f"🔔 Уведомления: {'✅ Вкл' if notif_on else '❌ Выкл'}\n"
        f"💬 Вопрос о донате: {'✅ Вкл' if donate_ask_on else '❌ Выкл'}\n\n"
        f"🤖 Юзернейм бота: @{bot_username}\n"
        f"🛒 Магазин: {cfg.shop_name}\n"
        f"🔗 Профиль: {cfg.profile_url}"
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


# ── Manual approve/reject (for pending requests) ──────────────────────────────

@router.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
async def handle_admin_decision(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    action, raw_id = callback.data.split(":", maxsplit=1)
    request_id = int(raw_id)
    request = await db.get_request(request_id)

    if request is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if request["status"] not in (RequestStatus.PENDING.value,):
        await callback.answer("Заявка уже обработана.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    product = get_product(request["product_code"])
    user_id = request["user_id"]

    if action == "reject":
        await db.resolve_request(request_id, RequestStatus.REJECTED, callback.from_user.id)
        await bot.send_message(
            user_id,
            "❌ Ваша заявка отклонена администратором.\n"
            "Если ошибка — напишите в чат заказа на площадке.",
        )
        await callback.message.edit_text(
            f"{callback.message.html_text}\n\n<b>Статус: ❌ ОТКЛОНЁН</b>"
        )
        await callback.answer("Отклонено.")
        return

    gift_id = catalog.resolve_gift_id(product.star_count) if product else None
    if gift_id is None:
        await callback.answer("Подарок не найден в каталоге.", show_alert=True)
        return

    try:
        await deliver_gift(
            bot,
            user_id=user_id,
            gift_id=gift_id,
            caption=request["comment"],
        )
    except Exception as exc:
        logger.exception("Ошибка отправки подарка по заявке #%s", request_id)
        await callback.answer("Ошибка отправки — см. лог.", show_alert=True)
        await bot.send_message(cfg.admin_id, f"⚠️ Ошибка sendGift #{request_id}: {exc}")
        return

    await db.resolve_request(request_id, RequestStatus.APPROVED, callback.from_user.id)
    await db.increment_gifts_received(user_id)
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n<b>Статус: ✅ ВЫДАН</b>"
    )
    await callback.answer("Подарок отправлен.")

    await bot.send_message(user_id, COMPLETION_TEXT)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global bot_username
    await db.init()
    await catalog.load(bot)
    me = await bot.get_me()
    bot_username = me.username
    logger.info("Gift bot @%s started for %s", bot_username, cfg.shop_name)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
