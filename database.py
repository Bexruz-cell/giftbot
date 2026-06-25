import aiosqlite
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO = "auto"


SCHEMA = """
CREATE TABLE IF NOT EXISTS gift_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    product_code TEXT NOT NULL,
    comment TEXT NOT NULL,
    chat_link TEXT,
    link_code TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gift_requests_user ON gift_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_gift_requests_status ON gift_requests(status);

CREATE TABLE IF NOT EXISTS gift_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    product_code TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_by INTEGER,
    used_at TEXT,
    created_at TEXT NOT NULL,
    created_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gift_links_code ON gift_links(code);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    first_seen TEXT NOT NULL,
    last_active TEXT NOT NULL,
    total_gifts_received INTEGER NOT NULL DEFAULT 0,
    total_donated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_donations_user ON donations(user_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('gifts_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('notifications_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('donation_ask_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('extra_admins', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('starvell_cookie', '');
INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_bump_enabled', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_bump_interval', '30');

CREATE TABLE IF NOT EXISTS auto_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    reply_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            # Migrate existing table: add new columns if missing
            for col, definition in [
                ("chat_link", "TEXT"),
                ("link_code", "TEXT"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE gift_requests ADD COLUMN {col} {definition}")
                except Exception:
                    pass
            await db.commit()

    # ── Settings ──────────────────────────────────────────────────────────────

    async def get_setting(self, key: str) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else ""

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def is_gifts_enabled(self) -> bool:
        return (await self.get_setting("gifts_enabled")) == "1"

    async def toggle_gifts(self) -> bool:
        enabled = await self.is_gifts_enabled()
        new_val = "0" if enabled else "1"
        await self.set_setting("gifts_enabled", new_val)
        return new_val == "1"

    async def toggle_notifications(self) -> bool:
        val = await self.get_setting("notifications_enabled")
        new_val = "0" if val == "1" else "1"
        await self.set_setting("notifications_enabled", new_val)
        return new_val == "1"

    async def toggle_donation_ask(self) -> bool:
        val = await self.get_setting("donation_ask_enabled")
        new_val = "0" if val == "1" else "1"
        await self.set_setting("donation_ask_enabled", new_val)
        return new_val == "1"

    # ── Admin management ──────────────────────────────────────────────────────

    async def get_extra_admins(self) -> list[int]:
        raw = await self.get_setting("extra_admins")
        if not raw:
            return []
        return [int(x) for x in raw.split(",") if x.strip().isdigit()]

    async def add_admin(self, user_id: int) -> bool:
        admins = await self.get_extra_admins()
        if user_id in admins:
            return False
        admins.append(user_id)
        await self.set_setting("extra_admins", ",".join(str(a) for a in admins))
        return True

    async def remove_admin(self, user_id: int) -> bool:
        admins = await self.get_extra_admins()
        if user_id not in admins:
            return False
        admins = [a for a in admins if a != user_id]
        await self.set_setting("extra_admins", ",".join(str(a) for a in admins))
        return True

    # ── Auto-replies ──────────────────────────────────────────────────────────

    async def get_auto_replies(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, keyword, reply_text FROM auto_replies ORDER BY id"
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def add_auto_reply(self, keyword: str, reply_text: str) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO auto_replies (keyword, reply_text) VALUES (?, ?)",
                    (keyword.lower().strip(), reply_text),
                )
                await db.commit()
            return True
        except Exception:
            return False

    async def remove_auto_reply(self, reply_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM auto_replies WHERE id = ?", (reply_id,))
            await db.commit()
            return cur.rowcount > 0

    async def find_auto_reply(self, text: str) -> str | None:
        text_lower = text.lower()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT keyword, reply_text FROM auto_replies") as cur:
                async for row in cur:
                    if row["keyword"] in text_lower:
                        return row["reply_text"]
        return None

    # ── Users ─────────────────────────────────────────────────────────────────

    async def upsert_user(self, user_id: int, username: str | None, first_name: str, last_name: str | None) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO users (id, username, first_name, last_name, first_seen, last_active)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     username=excluded.username,
                     first_name=excluded.first_name,
                     last_name=excluded.last_name,
                     last_active=excluded.last_active""",
                (user_id, username, first_name, last_name, now, now),
            )
            await db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_users(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users ORDER BY last_active DESC")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_user_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            row = await cur.fetchone()
            return row[0]

    # ── Gift Links ────────────────────────────────────────────────────────────

    async def create_gift_link(self, code: str, product_code: str, created_by: int) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO gift_links (code, product_code, created_at, created_by) VALUES (?, ?, ?, ?)",
                (code, product_code, now, created_by),
            )
            await db.commit()

    async def get_gift_link(self, code: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM gift_links WHERE code = ?", (code,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def use_gift_link(self, code: str, user_id: int) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE gift_links SET used=1, used_by=?, used_at=? WHERE code=?",
                (user_id, now, code),
            )
            await db.commit()

    async def get_admin_links(self, admin_id: int, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM gift_links WHERE created_by=? ORDER BY created_at DESC LIMIT ?",
                (admin_id, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_link_count(self) -> tuple[int, int]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT COUNT(*), SUM(used) FROM gift_links")
            row = await cur.fetchone()
            return (row[0] or 0, row[1] or 0)

    # ── Gift Requests ─────────────────────────────────────────────────────────

    async def has_pending_request(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM gift_requests WHERE user_id=? AND status=? LIMIT 1",
                (user_id, RequestStatus.PENDING.value),
            )
            return (await cur.fetchone()) is not None

    async def create_request(
        self,
        user_id: int,
        username: str | None,
        product_code: str,
        comment: str,
        chat_link: str | None = None,
        link_code: str | None = None,
        status: RequestStatus = RequestStatus.PENDING,
    ) -> int:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO gift_requests
                   (user_id, username, product_code, comment, chat_link, link_code, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, product_code, comment, chat_link, link_code, status.value, now),
            )
            await db.commit()
            return cur.lastrowid

    async def get_request(self, request_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM gift_requests WHERE id=?", (request_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def resolve_request(self, request_id: int, status: RequestStatus, admin_id: int) -> bool:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """UPDATE gift_requests SET status=?, resolved_at=?, resolved_by=?
                   WHERE id=? AND status=?""",
                (status.value, now, admin_id, request_id, RequestStatus.PENDING.value),
            )
            await db.commit()
            return cur.rowcount > 0

    async def get_requests_by_status(self, status: str, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM gift_requests WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def clear_pending(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM gift_requests WHERE status=?", (RequestStatus.PENDING.value,)
            )
            await db.commit()
            return cur.rowcount

    async def increment_gifts_received(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET total_gifts_received=total_gifts_received+1 WHERE id=?", (user_id,)
            )
            await db.commit()

    # ── Statistics ────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            totals = {}
            for status in ("pending", "approved", "rejected", "auto"):
                cur = await db.execute(
                    "SELECT COUNT(*) FROM gift_requests WHERE status=?", (status,)
                )
                totals[status] = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COUNT(*) FROM gift_requests")
            totals["total"] = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COUNT(*) FROM users")
            totals["users"] = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COALESCE(SUM(amount),0) FROM donations")
            totals["total_donated"] = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COUNT(*) FROM donations")
            totals["donation_count"] = (await cur.fetchone())[0]

            return totals

    async def get_period_stats(self, days: int) -> dict:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            totals = {}
            for status in ("pending", "approved", "rejected", "auto"):
                cur = await db.execute(
                    "SELECT COUNT(*) FROM gift_requests WHERE status=? AND created_at>=?",
                    (status, since),
                )
                totals[status] = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT COUNT(*) FROM gift_requests WHERE created_at>=?", (since,)
            )
            totals["total"] = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM donations WHERE created_at>=?", (since,)
            )
            totals["donated"] = (await cur.fetchone())[0]
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE first_seen>=?", (since,)
            )
            totals["new_users"] = (await cur.fetchone())[0]
            return totals

    # ── Donations ─────────────────────────────────────────────────────────────

    async def record_donation(self, user_id: int, amount: int, payload: str) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO donations (user_id, amount, payload, created_at) VALUES (?, ?, ?, ?)",
                (user_id, amount, payload, now),
            )
            await db.execute(
                "UPDATE users SET total_donated=total_donated+? WHERE id=?", (amount, user_id)
            )
            await db.commit()

    async def get_top_donors(self, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT u.id, u.username, u.first_name, u.total_donated,
                          COUNT(d.id) as donation_count
                   FROM users u
                   JOIN donations d ON d.user_id = u.id
                   GROUP BY u.id
                   ORDER BY u.total_donated DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
