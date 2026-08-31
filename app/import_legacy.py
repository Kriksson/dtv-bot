"""One-time importer for channels.txt, templates.json, buttons.json and stats.txt."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .database import Database


async def run(source: Path, database: Path) -> None:
    db = Database(str(database)); await db.connect()
    try:
        channels = source / "channels.txt"
        if channels.exists():
            for value in {line.strip() for line in channels.read_text(encoding="utf-8").splitlines() if line.strip()}:
                await db.add_channel(int(value), f"Канал {value}")
        buttons = source / "buttons.json"
        if buttons.exists():
            for callback, item in json.loads(buttons.read_text(encoding="utf-8")).items():
                if isinstance(item, dict) and item.get("ch"):
                    await db.add_legacy_callback(callback, str(item["ch"]))
        templates = source / "templates.json"
        if templates.exists():
            for user_id, items in json.loads(templates.read_text(encoding="utf-8")).items():
                for name, payload in items.items():
                    await db.save_template(int(user_id), name[:40], payload)
        stats = source / "stats.txt"
        if stats.exists():
            for line in stats.read_text(encoding="utf-8").splitlines():
                try:
                    stamp = datetime.strptime(line.strip(), "%d.%m.%Y").replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    continue
                await db.db.execute("INSERT INTO clicks(clicked_at) VALUES(?)", (stamp,))
            await db.db.commit()
    finally:
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m app.import_legacy <source-folder> <database-path>")
    asyncio.run(run(Path(sys.argv[1]), Path(sys.argv[2])))
