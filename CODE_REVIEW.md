# Code review

## Addressed before publication

- Removed hardcoded Telegram token and administrator password.
- Added `.env` loading, explicit startup validation, `.env.example` and dependency definitions.
- Replaced ordinary password equality with `hmac.compare_digest`.
- Excluded runtime data, backup files and virtual environments from Git.
- Merged two token-only deployments into one configurable source tree.

## Follow-up improvements

1. Replace the shared password with an allowlist of Telegram user IDs or a real administrator authorization flow. A password in an environment variable is still plaintext at runtime.
2. Move in-memory state and JSON runtime files to a database if multiple workers, restarts or higher traffic are expected.
3. Add a send queue with retry/backoff for Telegram API rate limits.
4. Split `bot.py` into modules for configuration, storage, authorization and publication workflow; the current single-file implementation is difficult to test.
