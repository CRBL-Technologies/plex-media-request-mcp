"""Run inside the managed bot container: secrets reach one private file, not the bot's env."""

import os
import stat
import sys
from pathlib import Path

secrets = Path("/run/media-secrets/bot.env")
if not secrets.exists() or stat.S_IMODE(secrets.stat().st_mode) != 0o600:
    sys.exit("bot secrets file is missing or not owner-only")
text = secrets.read_text()
if 'TELEGRAM_BOT_TOKEN="fetched-telegram-token"' not in text:
    sys.exit("fetched Telegram token was not projected")
if 'OPENROUTER_API_KEY="fetched-openrouter-key"' not in text:
    sys.exit("fetched OpenRouter key was not projected")

children = []
for process in Path("/proc").iterdir():
    if not process.name.isdecimal():
        continue
    try:
        command = (process / "cmdline").read_bytes().split(b"\0")
        environment = (process / "environ").read_bytes().split(b"\0")
    except OSError:
        continue
    if b"media_bot.bot" in command:
        children.append(environment)
if len(children) != 1:
    sys.exit("expected exactly one running bot process")
names = {item.split(b"=", 1)[0] for item in children[0] if item}
leaked = names & {b"TELEGRAM_BOT_TOKEN", b"OPENROUTER_API_KEY"} | {
    name for name in names if name.startswith(b"OP_")
}
if leaked:
    sys.exit(f"bot process environment carries {sorted(leaked)}")
if os.environ.get("MEDIA_BOT_MODEL") != "openai/smoke":
    sys.exit("probe environment differs from the container")
