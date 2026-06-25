"""Starvell API client — auto-bump and stats."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

BASE = "https://starvell.com"


class StarvellClient:
    def __init__(self, cookie: str) -> None:
        self.cookie = cookie

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    # ── Auth helpers ───────────────────────────────────────────────────────────

    async def get_user_id(self) -> int | None:
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(
                    f"{BASE}/api/auth/session",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        uid = data.get("user", {}).get("id")
                        if uid:
                            return int(uid)
            except Exception as e:
                logger.warning("get_user_id failed: %s", e)
        return None

    async def get_build_id(self) -> str | None:
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(
                    BASE,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    html = await r.text()
                    m = re.search(
                        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                        html,
                        re.DOTALL,
                    )
                    if m:
                        data = json.loads(m.group(1))
                        return data.get("buildId")
            except Exception as e:
                logger.warning("get_build_id failed: %s", e)
        return None

    # ── Offers ────────────────────────────────────────────────────────────────

    async def fetch_offers(self, user_id: int, build_id: str) -> list[dict[str, Any]]:
        url = f"{BASE}/_next/data/{build_id}/users/{user_id}.json?user_id={user_id}"
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(
                    url,
                    headers={**self._headers(), "x-nextjs-data": "1"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
                    page = data.get("pageProps", {})
                    categories = (
                        page.get("categoriesWithOffers")
                        or page.get("userProfileOffers")
                        or (page.get("bff") or {}).get("userProfileOffers")
                        or []
                    )
                    offers: list[dict[str, Any]] = []
                    for cat in categories:
                        game_id = cat.get("gameId") or (cat.get("game") or {}).get("id")
                        cat_id = cat.get("id")
                        for offer in cat.get("offers", []):
                            offers.append(
                                {
                                    "id": offer.get("id"),
                                    "gameId": game_id,
                                    "categoryId": cat_id,
                                    "title": (offer.get("descriptions") or {})
                                    .get("rus", {})
                                    .get("briefDescription", ""),
                                    "price": offer.get("price"),
                                }
                            )
                    return offers
            except Exception as e:
                logger.warning("fetch_offers failed: %s", e)
        return []

    def _group_offers(self, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for o in offers:
            gid = o.get("gameId")
            cid = o.get("categoryId")
            if not gid or not cid:
                continue
            key = str(gid)
            if key not in groups:
                groups[key] = {"gameId": gid, "categoryIds": set()}
            groups[key]["categoryIds"].add(cid)
        return [{"gameId": v["gameId"], "categoryIds": list(v["categoryIds"])} for v in groups.values()]

    # ── Bump ──────────────────────────────────────────────────────────────────

    async def _do_bump(
        self, session: aiohttp.ClientSession, game_id: Any, category_ids: list[Any]
    ) -> dict[str, Any]:
        body = json.dumps({"gameId": game_id, "categoryIds": category_ids})
        for attempt in range(3):
            try:
                async with session.post(
                    f"{BASE}/api/offers/bump",
                    data=body,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 429:
                        retry_after = int(r.headers.get("Retry-After", 90))
                        logger.warning("429 rate-limit, waiting %ds", retry_after)
                        await asyncio.sleep(min(retry_after, 120))
                        continue
                    resp_json: dict[str, Any] = {}
                    try:
                        resp_json = await r.json()
                    except Exception:
                        pass
                    return {"ok": r.ok, "status": r.status, "body": resp_json}
            except Exception as e:
                logger.warning("bump attempt %d failed: %s", attempt, e)
                await asyncio.sleep(3)
        return {"ok": False, "status": 0}

    async def bump_lots(self) -> dict[str, Any]:
        """Bump all active lots. Returns result summary."""
        user_id = await self.get_user_id()
        if not user_id:
            return {"ok": False, "error": "Не удалось получить ID пользователя Starvell. Проверьте cookie."}

        build_id = await self.get_build_id()
        if not build_id:
            return {"ok": False, "error": "Не удалось получить buildId сайта."}

        offers = await self.fetch_offers(user_id, build_id)
        if not offers:
            return {"ok": False, "error": "Активных лотов не найдено."}

        payloads = self._group_offers(offers)
        success = 0
        failed = 0

        async with aiohttp.ClientSession() as s:
            for i, p in enumerate(payloads):
                result = await self._do_bump(s, p["gameId"], p["categoryIds"])
                if result["ok"]:
                    success += 1
                else:
                    failed += 1
                if i < len(payloads) - 1:
                    await asyncio.sleep(2)

        return {
            "ok": success > 0,
            "total_offers": len(offers),
            "groups": len(payloads),
            "success": success,
            "failed": failed,
        }

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        """Fetch basic stats: order count + review stats."""
        stats: dict[str, Any] = {}
        user_id = await self.get_user_id()
        if not user_id:
            return {"error": "Не удалось авторизоваться в Starvell."}

        stats["user_id"] = user_id

        async with aiohttp.ClientSession() as s:
            # Order count
            try:
                async with s.get(
                    f"{BASE}/api/orders/created-count",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        stats["orders_count"] = data.get("count", data)
            except Exception as e:
                logger.warning("orders count failed: %s", e)

            # Review stats
            try:
                async with s.get(
                    f"{BASE}/api/reviews/user-stats/?userId={user_id}",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        stats["reviews"] = await r.json()
            except Exception as e:
                logger.warning("reviews stats failed: %s", e)

            # Unread chats count
            try:
                async with s.post(
                    f"{BASE}/api/chats/list-unread",
                    data=json.dumps({}),
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        chats = data if isinstance(data, list) else data.get("chats", [])
                        stats["unread_chats"] = len(chats)
            except Exception as e:
                logger.warning("unread chats failed: %s", e)

        return stats
