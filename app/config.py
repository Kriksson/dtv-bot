from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str
    admins_file: Path
    database_path: Path
    fast_add_timeout: int
    telegram_proxy: str | None
    webapp_url: str
    webapp_port: int


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured. Copy .env.example to .env first.")
    admins_file = Path(os.getenv("ADMINS_FILE", "admins.txt"))
    timeout = int(os.getenv("FAST_ADD_TIMEOUT_SECONDS", "120"))
    proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None
    return Settings(token, admins_file, Path(os.getenv("DATABASE_PATH", "dtv_bot.sqlite3")), timeout, proxy, os.getenv("WEBAPP_URL", "").rstrip("/"), int(os.getenv("WEBAPP_PORT", "8080")))


class AdminRegistry:
    """Reads local admin IDs again when the file has changed."""
    def __init__(self, path: Path):
        self.path, self._mtime, self._ids, self._next_check = path, None, frozenset(), 0.0

    def contains(self, user_id: int) -> bool:
        now = time.monotonic()
        if now >= self._next_check:
            self._next_check = now + 2
            try:
                mtime = self.path.stat().st_mtime_ns
                if mtime != self._mtime:
                    self._ids = frozenset(int(line.strip()) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#"))
                    self._mtime = mtime
            except FileNotFoundError:
                self._ids, self._mtime = frozenset(), None
        return user_id in self._ids
