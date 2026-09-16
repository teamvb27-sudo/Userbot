#!/usr/bin/env python3
# bot.py — NEXUS entry point

import os, asyncio, logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from botcore import db, setup, SESS_DIR

# ─── CONFIG ──────────────────────────────────────────────────────
API_ID       = int(os.getenv("API_ID",   "37550071"))
API_HASH     =     os.getenv("API_HASH", "eca739f28db4f12737a0d418f4a77343")
PHONE_NUMBER =     os.getenv("PHONE",    "+918090007204")

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO, datefmt='%H:%M:%S'
)
log = logging.getLogger("NEXUS")

# ─── CLIENT ──────────────────────────────────────────────────────
SESSION_STRING = os.getenv("SESSION_STRING")
client = (
    TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    if SESSION_STRING else
    TelegramClient(str(SESS_DIR / "userbot"), API_ID, API_HASH)
)

# ─── MAIN ────────────────────────────────────────────────────────
async def main():
    await db.load()          # MongoDB se state load
    setup(client)            # sab handlers register
    me = await client.get_me()
    db.owner_id = me.id
    await db.save()
    log.info(f"✅ Logged in as @{me.username or me.first_name}")
    log.info("⚙️  .help type karo Telegram me")
    await client.run_until_disconnected()

if __name__ == '__main__':
    print("=" * 45)
    print("  NEXUS USERBOT — Starting...")
    print("=" * 45)
    try:
        # Set event loop policy for Python 3.10+
        if asyncio.sys.version_info >= (3, 10):
            asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        
        asyncio.run(client.start(phone=PHONE_NUMBER))
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("🛑 Stopped.")
    except Exception as e:
        log.error(f"Fatal: {e}")
