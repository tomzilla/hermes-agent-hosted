#!/usr/bin/env python3
"""
Slack token writer — writes the bot token from env to a file so the
webhook adapter can read it per-request.

Required env vars:
    SLACK_BOT_TOKEN   - Permanent bot token
    TOKEN_FILE_PATH   - Where to write the token (default: /opt/data/slack_token.txt)

Usage:
    python3 token_manager.py
"""

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [token_manager] %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
TOKEN_FILE = os.getenv("TOKEN_FILE_PATH", "/opt/data/slack_token.txt")


def main():
    if not BOT_TOKEN:
        logger.error("SLACK_BOT_TOKEN is required")
        sys.exit(1)

    path = Path(TOKEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BOT_TOKEN)
    logger.info("Wrote bot token to %s", TOKEN_FILE)


if __name__ == "__main__":
    main()
