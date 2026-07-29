# DTV Bots

Telegram publication bot with channel management, post previews, inline buttons, templates, usage statistics and a quick-posting mode.

The original server contained two identical deployments that differed only by Telegram token. This repository keeps one configurable codebase instead of duplicating the same project.

## Setup

1. Create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN` and a strong `ADMIN_PASSWORD`.
3. Start the bot:

   ```powershell
   python bot.py
   ```

The bot creates its runtime files (`channels.txt`, `stats.txt`, `buttons.json`, `templates.json`) beside `bot.py`. They are intentionally ignored by Git because they can contain deployment-specific channel and user data.

## Security

- Never commit `.env` or runtime data files.
- Revoke and recreate any Telegram bot token that was ever hardcoded or shared.
- Restrict bot access to trusted administrators. The current password gate is suitable only for a small private deployment.

## Author

Copyright (c) 2026 Kriksson.
