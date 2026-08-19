"""The agent's identity, shipped in the image rather than left on the host.

SOUL.md fills Hermes' first system-prompt slot: who the bot is, how it talks,
and what it must never claim or reveal. It is deliberately the one prompt tier
that names no tool. Tools describe themselves in their own schemas, Telegram's
facts live in ``PLATFORM_HINT``, and this file holds only what stays true when
both of those change -- because it is also the tier with the weakest feedback
loop. It sat on a NAS for three months describing tools that had been renamed or
deleted, and nothing failed.

Shipping it in the image closes that gap: the identity is versioned beside the
code it describes, installed into HERMES_HOME before the agent starts, and read
back through Hermes' own loader before the agent is allowed to serve. The cost is
that the host copy is now a build artifact -- an edit made on the NAS is reverted
by the next deploy.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

SOUL_FILENAME = "SOUL.md"
SOUL_MD = Path(__file__).with_name(SOUL_FILENAME).read_text(encoding="utf-8")


def install_soul(home: Path) -> bool:
    """Write the shipped identity into ``home``; report whether it changed.

    The file is left world-readable deliberately. It holds no secret, and a mode
    only its owner can read would turn a uid mismatch between this init script
    and the agent into a silently missing identity -- the failure the module
    exists to prevent.
    """

    target = home / SOUL_FILENAME
    try:
        if target.read_text(encoding="utf-8") == SOUL_MD:
            return False
    except OSError:
        pass  # Absent, unreadable or not a file: write it and find out.
    home.mkdir(parents=True, exist_ok=True)
    target.write_text(SOUL_MD, encoding="utf-8")
    target.chmod(0o644)
    # Match whoever owns HERMES_HOME rather than a hardcoded uid: the base image
    # has already prepared the directory for the user the agent runs as.
    owner = home.stat()
    # Unprivileged or non-POSIX: the write itself already succeeded.
    with contextlib.suppress(AttributeError, OSError):
        os.chown(target, owner.st_uid, owner.st_gid)
    return True
