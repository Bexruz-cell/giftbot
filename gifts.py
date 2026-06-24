import logging

from aiogram import Bot
from aiogram.methods import GetAvailableGifts, SendGift

logger = logging.getLogger("giftbot.gifts")


class GiftCatalog:
    """Кэш каталога подарков Telegram, найденных по количеству звёзд."""

    def __init__(self) -> None:
        self._by_star_count: dict[int, str] = {}
        self._loaded = False

    async def load(self, bot: Bot) -> None:
        gifts = await bot(GetAvailableGifts())
        self._by_star_count = {gift.star_count: gift.id for gift in gifts.gifts}
        self._loaded = True
        logger.info("Загружен каталог подарков: %s", self._by_star_count)

    def resolve_gift_id(self, star_count: int) -> str | None:
        if not self._loaded:
            raise RuntimeError("Каталог подарков не загружен — вызови load() при старте бота")
        return self._by_star_count.get(star_count)


async def deliver_gift(bot: Bot, user_id: int, gift_id: str, caption: str) -> None:
    await bot(SendGift(user_id=user_id, gift_id=gift_id, text=caption, text_parse_mode="HTML"))
