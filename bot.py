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
from starvell import StarvellClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("giftbot")

cfg = load_config()
db = Database(cfg.db_path)
catalog = GiftCatalog()
bot_username: str = ""
admin_ids: set[int] = set()

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
    waiting_topup_amount = State()
    waiting_add_admin = State()
    waiting_remove_admin = State()
    waiting_bump_cookie = State()
    waiting_bump_interval = State()
    waiting_ar_keyword = State()
    waiting_ar_reply = State()
    waiting_ar_delete = State()


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
        [
            InlineKeyboardButton(text="💫 Пополнить звёзды", callback_data="adm:topup"),
            InlineKeyboardButton(text="👑 Управление админами", callback_data="adm:admins"),
        ],
        [
            InlineKeyboardButton(text="🚀 Авто-поднятие Starvell", callback_data="adm:bump"),
            InlineKeyboardButton(text="💬 Авто-ответы", callback_data="adm:autoreplies"),
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

SUPPORT_TEXT = (
    "💙 Привет! Если нравится наш магазин — поддержи проект звёздами!\n"
    "Это займёт секунду и очень поможет развитию 🚀"
)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.first_name, user.last_name)

    # Admin panel
    if is_admin(user.id):
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

    # Regular user without link — first show shop info, then support ask
    await message.answer(NOT_PURCHASED_TEXT, disable_web_page_preview=True)
    await message.answer(SUPPORT_TEXT, reply_markup=donate_ask_keyboard())


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
            SUPPORT_TEXT,
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
    return user_id in admin_ids


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


# ── Top-up stars ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:topup")
async def adm_topup(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.waiting_topup_amount)
    await callback.message.edit_text(
        "💫 <b>Пополнение баланса звёзд бота</b>\n\n"
        "Введите количество звёзд для пополнения:\n"
        "<i>Например: 50, 100, 500</i>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_topup_amount, F.text)
async def adm_topup_amount(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        amount = int(message.text.strip())
        if amount < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число больше 0.", reply_markup=back_keyboard())
        return

    try:
        await bot.send_invoice(
            chat_id=message.from_user.id,
            title="💫 Пополнение баланса бота",
            description=f"Пополнение баланса бота на {amount}⭐ для выдачи подарков",
            payload=f"topup_{amount}",
            currency="XTR",
            prices=[LabeledPrice(label="Пополнение", amount=amount)],
        )
        await message.answer(
            f"🧾 Счёт на {amount}⭐ отправлен выше.\n"
            "После оплаты баланс бота пополнится автоматически."
        )
    except Exception as e:
        logger.exception("Ошибка создания инвойса на пополнение: %s", e)
        await message.answer(f"❌ Ошибка создания счёта: {e}", reply_markup=back_keyboard())


# ── Admin management ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:admins")
async def adm_admins(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    extra = await db.get_extra_admins()
    if extra:
        admins_list = "\n".join(f"  • <code>{uid}</code>" for uid in extra)
        text = f"👑 <b>Управление администраторами</b>\n\nДополнительные админы:\n{admins_list}"
    else:
        text = "👑 <b>Управление администраторами</b>\n\nДополнительных админов нет."

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm:add_admin")],
            [InlineKeyboardButton(text="➖ Удалить админа", callback_data="adm:remove_admin")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:add_admin")
async def adm_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    # Only main admin can add/remove admins
    if callback.from_user.id != cfg.admin_id:
        await callback.answer("Только главный администратор может управлять другими админами.", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_add_admin)
    await callback.message.edit_text(
        "➕ <b>Добавить администратора</b>\n\n"
        "Введите Telegram ID пользователя, которого хотите сделать администратором:\n"
        "<i>Попросите пользователя написать боту /start и найдите его ID в функции «Найти юзера»</i>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_add_admin, F.text)
async def adm_add_admin_confirm(message: Message, state: FSMContext) -> None:
    if message.from_user.id != cfg.admin_id:
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID.", reply_markup=back_keyboard())
        return

    if uid == cfg.admin_id:
        await message.answer("ℹ️ Это уже главный администратор.", reply_markup=back_keyboard())
        return

    added = await db.add_admin(uid)
    if added:
        admin_ids.add(uid)
        user = await db.get_user(uid)
        name = f"@{user['username']}" if user and user.get("username") else f"ID {uid}"
        await message.answer(
            f"✅ <b>{name}</b> добавлен как администратор.\n"
            "Теперь он имеет доступ к панели управления.",
            reply_markup=back_keyboard(),
        )
        # Notify new admin
        try:
            await bot.send_message(uid, "👑 Вам выдан статус администратора бота!")
        except Exception:
            pass
    else:
        await message.answer(f"ℹ️ Пользователь <code>{uid}</code> уже является администратором.", reply_markup=back_keyboard())


@router.callback_query(F.data == "adm:remove_admin")
async def adm_remove_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    if callback.from_user.id != cfg.admin_id:
        await callback.answer("Только главный администратор может удалять других админов.", show_alert=True)
        return
    extra = await db.get_extra_admins()
    if not extra:
        await callback.answer("Нет дополнительных администраторов.", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_remove_admin)
    admins_list = "\n".join(f"  • <code>{uid}</code>" for uid in extra)
    await callback.message.edit_text(
        f"➖ <b>Удалить администратора</b>\n\nТекущие:\n{admins_list}\n\nВведите ID для удаления:",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_remove_admin, F.text)
async def adm_remove_admin_confirm(message: Message, state: FSMContext) -> None:
    if message.from_user.id != cfg.admin_id:
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID.", reply_markup=back_keyboard())
        return

    removed = await db.remove_admin(uid)
    if removed:
        admin_ids.discard(uid)
        await message.answer(
            f"✅ Администратор <code>{uid}</code> удалён.",
            reply_markup=back_keyboard(),
        )
        try:
            await bot.send_message(uid, "ℹ️ Ваши права администратора были отозваны.")
        except Exception:
            pass
    else:
        await message.answer(
            f"❌ Пользователь <code>{uid}</code> не найден среди администраторов.",
            reply_markup=back_keyboard(),
        )


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


# ── Starvell auto-bump ────────────────────────────────────────────────────────

def bump_status_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "⏹ Остановить авто-поднятие" if enabled else "▶️ Запустить авто-поднятие"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Поднять прямо сейчас", callback_data="adm:bump_now")],
        [InlineKeyboardButton(text=toggle_text, callback_data="adm:bump_toggle")],
        [InlineKeyboardButton(text="🍪 Задать Cookie Starvell", callback_data="adm:bump_cookie")],
        [InlineKeyboardButton(text="⏱ Интервал поднятия", callback_data="adm:bump_interval")],
        [InlineKeyboardButton(text="📊 Статистика Starvell", callback_data="adm:starvell_stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")],
    ])


@router.callback_query(F.data == "adm:bump")
async def adm_bump_menu(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    enabled = (await db.get_setting("auto_bump_enabled")) == "1"
    interval = await db.get_setting("auto_bump_interval")
    cookie = await db.get_setting("starvell_cookie")
    cookie_status = "✅ Задан" if cookie else "❌ Не задан"
    await callback.message.edit_text(
        f"🚀 <b>Авто-поднятие Starvell</b>\n\n"
        f"Статус: {'✅ Работает' if enabled else '⏹ Остановлено'}\n"
        f"Интервал: <b>{interval} мин</b>\n"
        f"Cookie: <b>{cookie_status}</b>\n\n"
        f"<i>Cookie нужен для авторизации на Starvell.\n"
        f"Возьми его из браузера: DevTools → Application → Cookies → starvell.com</i>",
        reply_markup=bump_status_keyboard(enabled),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:bump_cookie")
async def adm_bump_cookie(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.waiting_bump_cookie)
    await callback.message.edit_text(
        "🍪 <b>Введи Cookie Starvell</b>\n\n"
        "Как получить:\n"
        "1. Зайди на starvell.com и войди в аккаунт\n"
        "2. Открой DevTools (F12) → Application → Cookies → starvell.com\n"
        "3. Скопируй всю строку cookies (все значения через ; )\n\n"
        "Отправь строку сюда:",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_bump_cookie, F.text)
async def adm_bump_cookie_set(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    cookie = message.text.strip()
    await db.set_setting("starvell_cookie", cookie)
    await message.answer(
        "✅ Cookie сохранён!\n\n"
        "Попробуй <b>Поднять прямо сейчас</b> чтобы проверить что всё работает.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К авто-поднятию", callback_data="adm:bump")],
        ]),
    )


@router.callback_query(F.data == "adm:bump_interval")
async def adm_bump_interval(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    current = await db.get_setting("auto_bump_interval")
    await state.set_state(AdminFlow.waiting_bump_interval)
    await callback.message.edit_text(
        f"⏱ <b>Интервал авто-поднятия</b>\n\nТекущий: <b>{current} мин</b>\n\n"
        "Введи новый интервал в минутах (минимум 15):",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_bump_interval, F.text)
async def adm_bump_interval_set(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        mins = int(message.text.strip())
        if mins < 15:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи число минут (минимум 15).", reply_markup=back_keyboard())
        return
    await db.set_setting("auto_bump_interval", str(mins))
    await message.answer(
        f"✅ Интервал установлен: <b>{mins} мин</b>\n"
        "Перезапусти авто-поднятие чтобы применилось.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К авто-поднятию", callback_data="adm:bump")],
        ]),
    )


@router.callback_query(F.data == "adm:bump_toggle")
async def adm_bump_toggle(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    enabled = (await db.get_setting("auto_bump_enabled")) == "1"
    new_val = "0" if enabled else "1"
    await db.set_setting("auto_bump_enabled", new_val)
    if new_val == "1":
        cookie = await db.get_setting("starvell_cookie")
        if not cookie:
            await db.set_setting("auto_bump_enabled", "0")
            await callback.answer("❌ Сначала задай Cookie Starvell!", show_alert=True)
            return
        await callback.answer("✅ Авто-поднятие запущено!")
    else:
        await callback.answer("⏹ Авто-поднятие остановлено.")
    await adm_bump_menu(callback)


@router.callback_query(F.data == "adm:bump_now")
async def adm_bump_now(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    cookie = await db.get_setting("starvell_cookie")
    if not cookie:
        await callback.answer("❌ Сначала задай Cookie Starvell!", show_alert=True)
        return
    await callback.answer("⏳ Поднимаю лоты...")
    await callback.message.edit_text("⏳ Поднимаю лоты, подождите...")
    client = StarvellClient(cookie)
    result = await client.bump_lots()
    if result.get("ok"):
        text = (
            f"✅ <b>Лоты подняты!</b>\n\n"
            f"Офферов: {result.get('total_offers', 0)}\n"
            f"Групп: {result.get('groups', 0)}\n"
            f"Успешно: {result.get('success', 0)}\n"
            f"Ошибок: {result.get('failed', 0)}"
        )
    else:
        text = f"❌ <b>Ошибка поднятия</b>\n\n{result.get('error', 'Неизвестная ошибка')}"
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К авто-поднятию", callback_data="adm:bump")],
        ]),
    )


@router.callback_query(F.data == "adm:starvell_stats")
async def adm_starvell_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    cookie = await db.get_setting("starvell_cookie")
    if not cookie:
        await callback.answer("❌ Сначала задай Cookie Starvell!", show_alert=True)
        return
    await callback.answer("⏳ Загружаю статистику...")
    await callback.message.edit_text("⏳ Загружаю статистику Starvell...")
    client = StarvellClient(cookie)
    stats = await client.get_stats()
    if "error" in stats:
        text = f"❌ <b>Ошибка</b>\n\n{stats['error']}"
    else:
        reviews = stats.get("reviews", {})
        pos = reviews.get("positive", 0) if isinstance(reviews, dict) else 0
        neg = reviews.get("negative", 0) if isinstance(reviews, dict) else 0
        total_r = reviews.get("total", pos + neg) if isinstance(reviews, dict) else 0
        text = (
            f"📊 <b>Статистика Starvell</b>\n\n"
            f"🆔 User ID: <code>{stats.get('user_id', '—')}</code>\n"
            f"📦 Заказов создано: <b>{stats.get('orders_count', '—')}</b>\n"
            f"⭐ Отзывов: <b>{total_r}</b> (👍{pos} / 👎{neg})\n"
            f"💬 Непрочитанных чатов: <b>{stats.get('unread_chats', '—')}</b>"
        )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К авто-поднятию", callback_data="adm:bump")],
        ]),
    )


# ── Auto-replies ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:autoreplies")
async def adm_autoreplies(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    replies = await db.get_auto_replies()
    if replies:
        lines = "\n".join(
            f"  <b>{r['id']}.</b> <code>{r['keyword']}</code> → {r['reply_text'][:40]}…"
            if len(r['reply_text']) > 40
            else f"  <b>{r['id']}.</b> <code>{r['keyword']}</code> → {r['reply_text']}"
            for r in replies
        )
        text = f"💬 <b>Авто-ответы</b> ({len(replies)} шт.)\n\n{lines}"
    else:
        text = "💬 <b>Авто-ответы</b>\n\nПока нет ни одного авто-ответа."
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="adm:ar_add")],
            [InlineKeyboardButton(text="➖ Удалить по номеру", callback_data="adm:ar_delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm:back")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:ar_add")
async def adm_ar_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.waiting_ar_keyword)
    await callback.message.edit_text(
        "➕ <b>Новый авто-ответ</b>\n\n"
        "Шаг 1/2: Введи <b>ключевое слово</b> (или фразу).\n"
        "Когда пользователь напишет что-то содержащее это слово — бот ответит автоматически.\n\n"
        "<i>Например: цена, доставка, привет, помощь</i>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_ar_keyword, F.text)
async def adm_ar_keyword(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.update_data(keyword=message.text.strip())
    await state.set_state(AdminFlow.waiting_ar_reply)
    await message.answer(
        f"✅ Ключевое слово: <code>{message.text.strip()}</code>\n\n"
        "Шаг 2/2: Введи <b>текст ответа</b>, который получит пользователь:",
        reply_markup=back_keyboard(),
    )


@router.message(AdminFlow.waiting_ar_reply, F.text)
async def adm_ar_reply(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    keyword = data.get("keyword", "")
    await state.clear()
    ok = await db.add_auto_reply(keyword, message.text.strip())
    if ok:
        await message.answer(
            f"✅ <b>Авто-ответ добавлен!</b>\n\n"
            f"Слово: <code>{keyword}</code>\n"
            f"Ответ: {message.text.strip()}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список авто-ответов", callback_data="adm:autoreplies")],
            ]),
        )
    else:
        await message.answer(
            f"❌ Ключевое слово <code>{keyword}</code> уже существует.",
            reply_markup=back_keyboard(),
        )


@router.callback_query(F.data == "adm:ar_delete")
async def adm_ar_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    replies = await db.get_auto_replies()
    if not replies:
        await callback.answer("Нет авто-ответов для удаления.", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_ar_delete)
    lines = "\n".join(f"  <b>{r['id']}.</b> <code>{r['keyword']}</code>" for r in replies)
    await callback.message.edit_text(
        f"➖ <b>Удалить авто-ответ</b>\n\n{lines}\n\nВведи <b>номер</b> для удаления:",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@router.message(AdminFlow.waiting_ar_delete, F.text)
async def adm_ar_delete_confirm(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        rid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи числовой ID.", reply_markup=back_keyboard())
        return
    removed = await db.remove_auto_reply(rid)
    if removed:
        await message.answer(
            f"✅ Авто-ответ #{rid} удалён.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Список авто-ответов", callback_data="adm:autoreplies")],
            ]),
        )
    else:
        await message.answer(f"❌ Авто-ответ #{rid} не найден.", reply_markup=back_keyboard())


# ── Auto-reply handler for regular users ──────────────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def auto_reply_handler(message: Message, state: FSMContext) -> None:
    if is_admin(message.from_user.id):
        return
    current_state = await state.get_state()
    if current_state is not None:
        return
    reply = await db.find_auto_reply(message.text)
    if reply:
        await message.answer(reply)


# ── Auto-bump background task ─────────────────────────────────────────────────

async def auto_bump_loop() -> None:
    logger.info("Auto-bump background task started.")
    while True:
        try:
            enabled = (await db.get_setting("auto_bump_enabled")) == "1"
            interval_min = int(await db.get_setting("auto_bump_interval") or "30")
            if enabled:
                cookie = await db.get_setting("starvell_cookie")
                if cookie:
                    client = StarvellClient(cookie)
                    result = await client.bump_lots()
                    if result.get("ok"):
                        msg = (
                            f"🚀 <b>Авто-поднятие выполнено</b>\n"
                            f"Офферов: {result.get('total_offers', 0)}, "
                            f"групп: {result.get('groups', 0)}"
                        )
                    else:
                        msg = f"⚠️ <b>Авто-поднятие: ошибка</b>\n{result.get('error', '?')}"
                    try:
                        await bot.send_message(cfg.admin_id, msg)
                    except Exception:
                        pass
                    logger.info("Auto-bump result: %s", result)
                else:
                    logger.warning("Auto-bump enabled but no cookie set.")
            await asyncio.sleep(interval_min * 60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Auto-bump loop error: %s", e)
            await asyncio.sleep(60)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global bot_username, admin_ids
    await db.init()
    await catalog.load(bot)
    me = await bot.get_me()
    bot_username = me.username
    # Load all admin IDs (main + extra)
    admin_ids = {cfg.admin_id}
    extra = await db.get_extra_admins()
    admin_ids.update(extra)
    logger.info("Gift bot @%s started. Admins: %s", bot_username, admin_ids)
    await bot.delete_webhook(drop_pending_updates=True)
    # Start auto-bump background task
    asyncio.create_task(auto_bump_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
