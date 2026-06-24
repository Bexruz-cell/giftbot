import aiosqlite
from datetime import datetime
from enum import Enum


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


SCHEMA = """
CREATE TABLE IF NOT EXISTS gift_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    product_code TEXT NOT NULL,
    comment TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gift_requests_user ON gift_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_gift_requests_status ON gift_requests(status);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def has_pending_request(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM gift_requests WHERE user_id = ? AND status = ? LIMIT 1",
                (user_id, RequestStatus.PENDING.value),
            )
            row = await cursor.fetchone()
            return row is not None

    async def create_request(self, user_id: int, username: str | None, product_code: str, comment: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO gift_requests (user_id, username, product_code, comment, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, username, product_code, comment, RequestStatus.PENDING.value, datetime.utcnow().isoformat()),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_request(self, request_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM gift_requests WHERE id = ?", (request_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def resolve_request(self, request_id: int, status: RequestStatus, admin_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE gift_requests
                   SET status = ?, resolved_at = ?, resolved_by = ?
                   WHERE id = ? AND status = ?""",
                (status.value, datetime.utcnow().isoformat(), admin_id, request_id, RequestStatus.PENDING.value),
            )
            await db.commit()
            return cursor.rowcount > 0
