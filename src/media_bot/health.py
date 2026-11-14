"""Container health: the Telegram long poll returned recently.

This runs every 30 seconds, so it imports nothing from the bot itself.
"""

import os
import time

HEARTBEAT_FILE = "/tmp/media-bot.heartbeat"
STALE_SECONDS = 180

if __name__ == "__main__":
    try:
        fresh = time.time() - os.stat(HEARTBEAT_FILE).st_mtime < STALE_SECONDS
    except OSError:
        fresh = False
    raise SystemExit(0 if fresh else 1)
