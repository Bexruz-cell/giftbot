import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Product:
    code: str
    title: str
    star_count: int


PRODUCTS: tuple[Product, ...] = (
    Product(code="mishka", title="🧸 Мишка", star_count=15),
    Product(code="rosa", title="🌹 Роза", star_count=25),
)


def get_product(code: str) -> Product | None:
    for p in PRODUCTS:
        if p.code == code:
            return p
    return None


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    shop_name: str = "RiyoShop"
    shop_url: str = "https://starvell.com/offers/233831"
    profile_url: str = "https://starvell.com/profile/riyoshop"
    db_path: str = "giftbot.db"


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN")
    admin_id_raw = os.getenv("ADMIN_ID")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    if not admin_id_raw:
        raise RuntimeError("ADMIN_ID не задан в .env")
    return Config(bot_token=token, admin_id=int(admin_id_raw))
