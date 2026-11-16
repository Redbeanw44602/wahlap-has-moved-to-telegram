"""SQLite account isolation, keyed exclusively by immutable Telegram user IDs."""

import asyncio
from pathlib import Path

import aiosqlite

from .endpoints import UserEndpoint


class UserStore:
    def __init__(self, path: Path):
        self.path = path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create with private permissions before SQLite can write credentials.
        descriptor = self.path.open("a", encoding="utf-8")
        descriptor.close()
        self.path.chmod(0o600)
        connection = await aiosqlite.connect(self.path)
        try:
            await connection.execute("PRAGMA busy_timeout=5000")
            await connection.execute("PRAGMA secure_delete=ON")
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    auth_token TEXT
                )"""
            )
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("UserStore is not open")
        return self._connection

    async def get(self, user_id: int) -> UserEndpoint | None:
        async with self._lock:
            async with self._db().execute(
                "SELECT user_id, endpoint, auth_token FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return UserEndpoint(*row) if row else None

    async def save(self, user: UserEndpoint) -> None:
        async with self._lock:
            await self._db().execute(
                """INSERT INTO users (user_id, endpoint, auth_token) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   endpoint = excluded.endpoint, auth_token = excluded.auth_token""",
                (user.user_id, user.endpoint, user.auth_token),
            )
            await self._db().commit()

    async def delete(self, user_id: int) -> None:
        async with self._lock:
            await self._db().execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            await self._db().commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
