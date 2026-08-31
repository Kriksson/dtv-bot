from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite


class Database:
    def __init__(self, path: str):
        self.path = path

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS channels (
              chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 1,
              checked_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS templates (
              user_id INTEGER NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS authorized_users (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS clicks (id INTEGER PRIMARY KEY AUTOINCREMENT, clicked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_callbacks (callback_data TEXT PRIMARY KEY, channel_id TEXT NOT NULL);
        """)
        await self.db.commit()

    async def close(self) -> None:
        await self.db.close()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def authorize(self, user_id: int) -> None:
        await self.db.execute("INSERT OR IGNORE INTO authorized_users VALUES (?)", (user_id,))
        await self.db.commit()

    async def is_authorized(self, user_id: int) -> bool:
        row = await (await self.db.execute("SELECT 1 FROM authorized_users WHERE user_id=?", (user_id,))).fetchone()
        return row is not None

    async def add_channel(self, chat_id: int, title: str, is_admin: bool = True) -> None:
        await self.db.execute("""INSERT INTO channels(chat_id,title,is_admin,checked_at,created_at) VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,is_admin=excluded.is_admin,checked_at=excluded.checked_at""",
            (str(chat_id), title, int(is_admin), self.now(), self.now()))
        await self.db.commit()

    async def update_channel_status(self, chat_id: str, is_admin: bool, title: str | None = None) -> None:
        if title is None:
            await self.db.execute("UPDATE channels SET is_admin=?, checked_at=? WHERE chat_id=?", (int(is_admin), self.now(), chat_id))
        else:
            await self.db.execute("UPDATE channels SET is_admin=?, title=?, checked_at=? WHERE chat_id=?", (int(is_admin), title, self.now(), chat_id))
        await self.db.commit()

    async def get_channel(self, chat_id: str):
        return await (await self.db.execute("SELECT * FROM channels WHERE chat_id=?", (chat_id,))).fetchone()

    async def channel_page(self, page: int, size: int = 8):
        total = (await (await self.db.execute("SELECT COUNT(*) FROM channels WHERE is_admin=1")).fetchone())[0]
        rows = await (await self.db.execute("SELECT * FROM channels WHERE is_admin=1 ORDER BY lower(title), chat_id LIMIT ? OFFSET ?", (size, page * size))).fetchall()
        return rows, total

    async def search_channels(self, query: str, limit: int = 20):
        needle = f"%{query.strip()}%"
        return await (await self.db.execute("SELECT * FROM channels WHERE is_admin=1 AND (title LIKE ? COLLATE NOCASE OR chat_id LIKE ?) ORDER BY lower(title) LIMIT ?", (needle, needle, limit))).fetchall()

    async def all_channels(self):
        return await (await self.db.execute("SELECT * FROM channels ORDER BY chat_id")).fetchall()

    async def delete_channel(self, chat_id: str) -> None:
        await self.db.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        await self.db.commit()

    async def save_template(self, user_id: int, name: str, payload: dict) -> None:
        await self.db.execute("INSERT INTO templates VALUES(?,?,?) ON CONFLICT(user_id,name) DO UPDATE SET payload=excluded.payload", (user_id, name, json.dumps(payload, ensure_ascii=False)))
        await self.db.commit()

    async def templates(self, user_id: int):
        return await (await self.db.execute("SELECT name,payload FROM templates WHERE user_id=? ORDER BY name", (user_id,))).fetchall()

    async def template(self, user_id: int, name: str):
        return await (await self.db.execute("SELECT payload FROM templates WHERE user_id=? AND name=?", (user_id, name))).fetchone()

    async def delete_template(self, user_id: int, name: str) -> None:
        await self.db.execute("DELETE FROM templates WHERE user_id=? AND name=?", (user_id, name))
        await self.db.commit()

    async def add_click(self) -> None:
        await self.db.execute("INSERT INTO clicks(clicked_at) VALUES(?)", (self.now(),))
        await self.db.commit()

    async def click_count(self) -> int:
        return (await (await self.db.execute("SELECT COUNT(*) FROM clicks")).fetchone())[0]

    async def add_legacy_callback(self, callback_data: str, channel_id: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO legacy_callbacks VALUES (?,?)", (callback_data, channel_id))
        await self.db.commit()

    async def legacy_channel(self, callback_data: str):
        row = await (await self.db.execute("SELECT channel_id FROM legacy_callbacks WHERE callback_data=?", (callback_data,))).fetchone()
        return row[0] if row else None
