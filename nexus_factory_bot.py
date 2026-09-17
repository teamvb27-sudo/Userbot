# ────────────────────────────────────────────────
#   NEXUS USERBOT — CLONE FACTORY (management Bot API bot)
#
#   WHAT THIS DOES:
#   - Runs as a normal Telegram Bot (via @BotFather token), NOT a userbot.
#   - Lets anyone generate their own Nexus Userbot clone by giving API_ID,
#     API_HASH, and OWNER_ID through chat.
#   - Deliberately does NOT ask for phone number or OTP anywhere in this
#     bot. Login for each clone happens in the owner's own terminal/Colab by
#     running nexus_core.py --login-only, where Telethon's own prompt
#     asks for phone/code directly — this bot never sees or stores it.
#   - Once a clone has logged in (its session file exists), this factory
#     auto-starts and monitors it as a background subprocess.
#
#   SETUP:
#   1. Get a bot token from @BotFather → set BOT_TOKEN below.
#   2. Get your own API_ID / API_HASH from my.telegram.org → set below
#      (this is for the FACTORY bot's own connection, separate from any
#      clone's credentials).
#   3. Make sure nexus_core.py sits in the same folder as this script.
#   4. python nexus_factory_bot.py
# ────────────────────────────────────────────────

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import psutil
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.errors import RPCError, MessageNotModifiedError

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

# ────────────────────────────────────────────────
#   CONFIG — reads from environment variables (set these in Render's
#   dashboard under your app's Environment Variables, NOT in this file).
# ────────────────────────────────────────────────
FACTORY_API_ID = int(os.environ.get("FACTORY_API_ID", "12345678"))
FACTORY_API_HASH = os.environ.get("FACTORY_API_HASH", "your_api_hash_here")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "123456789:your-botfather-token-here")

# Numeric Telegram user ID(s) of YOU, the person(s) running this factory
# bot. Only these IDs can open /admin and see the owner panel below.
# Supports multiple accounts — separate with a comma, e.g. "111,222".
FACTORY_OWNER_ID = int(os.environ.get("FACTORY_OWNER_ID", "0").split(",")[0].strip() or "0")
FACTORY_OWNER_IDS = {
    int(x.strip()) for x in os.environ.get("FACTORY_OWNER_ID", "0").split(",") if x.strip().isdigit()
} - {0}

# MongoDB — set MONGODB_URI to your Atlas connection string as an env var.
MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "nexus_factory")

# Public/restricted Mongo URI (used ONLY in the Colab login snippet we
# hand to end-users, so they never see your main admin-level MONGODB_URI).
PUBLIC_MONGODB_URI = os.environ.get("PUBLIC_MONGODB_URI", MONGODB_URI)

# Raw GitHub URL to nexus_core.py — the Colab snippet downloads it from
# here so end-users don't need git installed on their own device.
CORE_SCRIPT_RAW_URL = os.environ.get(
    "CORE_SCRIPT_RAW_URL",
    "https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/YOUR_REPO/main/nexus_core.py",
)

# How often (seconds) the factory re-checks MongoDB for freshly-logged-in
# clones (session_string appeared) without needing a manual redeploy.
MONGO_SYNC_INTERVAL_SECONDS = int(os.environ.get("MONGO_SYNC_INTERVAL_SECONDS", "60"))

# Resource Usage Guard thresholds — a clone restarts itself if it crosses
# either of these, sustained, to protect the whole host.
MAX_CPU_PERCENT = float(os.environ.get("MAX_CPU_PERCENT", "80"))
MAX_MEM_MB = float(os.environ.get("MAX_MEM_MB", "300"))

BASE_DIR = Path(__file__).resolve().parent
CLONES_DIR = BASE_DIR / "clones"
CORE_SCRIPT = BASE_DIR / "nexus_core.py"
CLONES_DIR.mkdir(exist_ok=True)

WATCH_INTERVAL_SECONDS = 15   # how often the factory checks/restarts clones
AUTO_BACKUP_INTERVAL_SECONDS = 24 * 60 * 60   # send a backup to the owner every 24h

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("nexus_factory.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("nexus_factory")

factory = TelegramClient("nexus_factory_session", FACTORY_API_ID, FACTORY_API_HASH)

running_procs: Dict[str, subprocess.Popen] = {}   # owner_id(str) -> process handle

# Chat IDs that currently have an open wizard conversation — prevents the
# "Cannot open exclusive conversation" crash from a double-tap.
ACTIVE_WIZARDS: set = set()

mongo_client = AsyncIOMotorClient(MONGODB_URI) if (MONGODB_URI and AsyncIOMotorClient) else None
mongo_db = mongo_client[MONGODB_DB_NAME] if mongo_client is not None else None
mongo_clones = mongo_db["clones"] if mongo_db is not None else None
mongo_blacklist = mongo_db["blacklist"] if mongo_db is not None else None

# ────────────────────────────────────────────────
#   GLOBAL BLACKLIST (G-BAN / anti-abuse)
# ────────────────────────────────────────────────
BANNED_IDS: set = set()


async def load_blacklist():
    if mongo_blacklist is None:
        return
    try:
        async for doc in mongo_blacklist.find({}):
            uid = doc.get("owner_id")
            if uid is not None:
                BANNED_IDS.add(int(uid))
        log.info(f"🚫 Loaded {len(BANNED_IDS)} banned ID(s) from MongoDB.")
    except Exception as e:
        log.error(f"load_blacklist failed: {e}")


async def ban_owner(owner_id: int, reason: str = ""):
    BANNED_IDS.add(owner_id)
    proc = running_procs.pop(str(owner_id), None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
    owner_dir = clone_dir(owner_id)
    if owner_dir.is_dir():
        shutil.rmtree(owner_dir, ignore_errors=True)
    await mongo_delete_clone(owner_id)
    if mongo_blacklist is not None:
        try:
            await mongo_blacklist.update_one(
                {"owner_id": owner_id},
                {"$set": {"owner_id": owner_id, "reason": reason, "banned_at": datetime.now().isoformat()}},
                upsert=True,
            )
        except Exception as e:
            log.error(f"ban_owner mongo write failed: {e}")


async def unban_owner(owner_id: int):
    BANNED_IDS.discard(owner_id)
    if mongo_blacklist is not None:
        try:
            await mongo_blacklist.delete_one({"owner_id": owner_id})
        except Exception as e:
            log.error(f"unban_owner mongo delete failed: {e}")


@factory.on(events.NewMessage())
async def _block_banned_messages(event):
    if event.sender_id in BANNED_IDS and event.sender_id not in FACTORY_OWNER_IDS:
        await event.reply("🚫 Aap is bot se ban ho — kisi bhi feature ka access nahi hai.")
        raise events.StopPropagation


@factory.on(events.CallbackQuery())
async def _block_banned_callbacks(event):
    if event.sender_id in BANNED_IDS and event.sender_id not in FACTORY_OWNER_IDS:
        await event.answer("🚫 Aap is bot se ban ho.", alert=True)
        raise events.StopPropagation


async def safe_edit(event, *args, **kwargs):
    try:
        await event.edit(*args, **kwargs)
    except MessageNotModifiedError:
        pass


MAIN_MENU = [
    [Button.inline("🧬 Make My Own Clone", b"make_clone")],
    [Button.inline("📋 My Clone Status", b"my_status")],
    [Button.inline("🗑️ Delete My Clone", b"delete_clone")],
]

BACK_BUTTON = [[Button.inline("⬅️ Back", b"back_main")]]


# ────────────────────────────────────────────────
#   PER-OWNER FILE HELPERS
# ────────────────────────────────────────────────
def clone_dir(owner_id: int) -> Path:
    d = CLONES_DIR / str(owner_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path(owner_id: int) -> Path:
    return clone_dir(owner_id) / "config.json"


def session_path(owner_id: int) -> Path:
    return clone_dir(owner_id) / "nexus_session.txt"


def load_json(path: Path) -> Optional[dict]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Could not read {path}: {e}")
    return None


def save_json(path: Path, obj: dict):
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def is_factory_owner(uid: int) -> bool:
    return uid in FACTORY_OWNER_IDS


def all_owner_dirs():
    for d in CLONES_DIR.iterdir():
        if d.is_dir():
            yield d.name, d


# ────────────────────────────────────────────────
#   MONGODB HELPERS
# ────────────────────────────────────────────────
async def mongo_upsert_clone(owner_id: int, cfg: Optional[dict] = None):
    if mongo_clones is None:
        return
    doc = {}
    if cfg is not None:
        doc["config"] = cfg
    if not doc:
        return
    try:
        await mongo_clones.update_one({"owner_id": owner_id}, {"$set": doc}, upsert=True)
    except Exception as e:
        log.error(f"Mongo upsert failed for {owner_id}: {e}")


async def mongo_delete_clone(owner_id: int):
    if mongo_clones is None:
        return
    try:
        await mongo_clones.delete_one({"owner_id": owner_id})
    except Exception as e:
        log.error(f"Mongo delete failed for {owner_id}: {e}")


async def mongo_hydrate_all():
    if mongo_clones is None:
        log.info("MONGODB_URI not set — running in local-files-only mode.")
        return
    try:
        count = 0
        async for doc in mongo_clones.find({}):
            owner_id = doc.get("owner_id")
            if owner_id is None:
                continue
            cfg = doc.get("config")
            session_str = doc.get("session_string")
            if cfg:
                save_json(config_path(owner_id), cfg)
            if session_str:
                session_path(owner_id).write_text(session_str, encoding="utf-8")
            count += 1
        log.info(f"✅ Hydrated {count} clone(s) from MongoDB.")
    except Exception as e:
        log.error(f"Mongo hydrate failed: {e}")


async def mongo_sync_new_sessions():
    if mongo_clones is None:
        return
    while True:
        await asyncio.sleep(MONGO_SYNC_INTERVAL_SECONDS)
        try:
            async for doc in mongo_clones.find({"session_string": {"$exists": True, "$ne": ""}}):
                owner_id = doc.get("owner_id")
                if owner_id is None:
                    continue
                sess_file = session_path(owner_id)
                mongo_sess = doc.get("session_string", "")
                local_sess = sess_file.read_text(encoding="utf-8").strip() if sess_file.is_file() else ""
                if mongo_sess and mongo_sess != local_sess:
                    cfg = doc.get("config")
                    if cfg and not config_path(owner_id).is_file():
                        save_json(config_path(owner_id), cfg)
                    sess_file.write_text(mongo_sess, encoding="utf-8")
                    log.info(f"🔄 Synced new session for owner {owner_id} from MongoDB (no redeploy needed).")
        except Exception as e:
            log.error(f"mongo_sync_new_sessions error: {e}")


# ────────────────────────────────────────────────
#   /start AND MAIN MENU
# ────────────────────────────────────────────────
@factory.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    await event.reply(
        "👋 **Nexus Userbot — Clone Factory**\n\n"
        "Apna khud ka Nexus userbot banao. Login kabhi bhi is bot ke "
        "through nahi hota — phone number/OTP hamesha tumhare apne Colab/terminal "
        "me, sirf tumhare control me.\n\n"
        "Neeche se option chuno:\n\n"
        "Made with ❤️ TEAMVB",
        buttons=MAIN_MENU,
    )


@factory.on(events.CallbackQuery(data=b"back_main"))
async def back_main(event):
    await safe_edit(event,
        "👋 **Nexus Userbot — Clone Factory**\n\nNeeche se option chuno:\n\nMade with ❤️ TEAMVB",
        buttons=MAIN_MENU,
    )


# ────────────────────────────────────────────────
#   MAKE MY OWN CLONE (wizard — API_ID / API_HASH / OWNER_ID only)
# ────────────────────────────────────────────────
@factory.on(events.CallbackQuery(data=b"make_clone"))
async def make_clone(event):
    owner_id = event.sender_id
    if config_path(owner_id).is_file():
        await event.answer("Aapka clone already exist karta hai — Status se manage karo.", alert=True)
        return
    if event.chat_id in ACTIVE_WIZARDS:
        await event.answer(
            "⏳ Ek wizard already chal raha hai isi chat me — upar wale message ka jawab do "
            "(API_ID/HASH/OWNER_ID), ya 5 minute wait karke dobara try karo.",
            alert=True,
        )
        return
    await event.answer()
    await run_clone_wizard(owner_id, event.chat_id)


async def run_clone_wizard(owner_id: int, chat_id: int):
    ACTIVE_WIZARDS.add(chat_id)
    try:
        async with factory.conversation(chat_id, timeout=300) as conv:
            await conv.send_message(
                "🧬 **Naya Clone Setup — Step 1/3**\n\n"
                "Apna **API_ID** bhejo (my.telegram.org se, sirf number, apne khud ke phone number se banaya hua — "
                "kisi aur ka mat use karo, warna Telegram us API_ID ko flag kar sakta hai)."
            )
            r = await conv.get_response()
            api_id_text = r.raw_text.strip()
            if not api_id_text.isdigit():
                await conv.send_message("⚠️ API_ID sirf number hona chahiye. **Make My Own Clone** se dobara try karo.")
                return
            api_id = int(api_id_text)

            await conv.send_message("**Step 2/3** — apna **API_HASH** bhejo.")
            r = await conv.get_response()
            api_hash = r.raw_text.strip()
            if len(api_hash) < 10:
                await conv.send_message("⚠️ Ye API_HASH sahi nahi lag raha. **Make My Own Clone** se dobara try karo.")
                return

            await conv.send_message(
                "**Step 3/3** — apna **numeric Telegram user ID** (OWNER_ID) bhejo.\n"
                "Pata nahi to @userinfobot ko message karo."
            )
            r = await conv.get_response()
            owner_id_text = r.raw_text.strip()
            if not owner_id_text.isdigit():
                await conv.send_message("⚠️ OWNER_ID sirf number hona chahiye. **Make My Own Clone** se dobara try karo.")
                return

            cfg = {
                "api_id": api_id,
                "api_hash": api_hash,
                "owner_id": int(owner_id_text),
                "session_file": str(session_path(owner_id)),
                "data_file": str(clone_dir(owner_id) / "nexus_data.json"),
                "log_file": str(clone_dir(owner_id) / "nexus.log"),
                "factory_owner_telegram_id": owner_id,
                "mongodb_uri": MONGODB_URI,
                "mongodb_db_name": MONGODB_DB_NAME,
            }
            save_json(config_path(owner_id), cfg)
            await mongo_upsert_clone(owner_id, cfg=cfg)

            colab_snippet = (
                "!pip install -q telethon pymongo\n\n"
                "import json, urllib.request\n\n"
                "config = {\n"
                f'    "api_id": {api_id},\n'
                f'    "api_hash": "{api_hash}",\n'
                f'    "owner_id": {int(owner_id_text)},\n'
                f'    "factory_owner_telegram_id": {owner_id},\n'
                '    "session_file": "nexus_session.txt",\n'
                f'    "mongodb_uri": "{PUBLIC_MONGODB_URI}",\n'
                f'    "mongodb_db_name": "{MONGODB_DB_NAME}",\n'
                "}\n"
                "with open(\"config.json\", \"w\") as f:\n"
                "    json.dump(config, f)\n\n"
                f'urllib.request.urlretrieve("{CORE_SCRIPT_RAW_URL}", "nexus_core.py")\n\n'
                "!python nexus_core.py --config config.json --login-only"
            )
            await conv.send_message(
                "✅ **Config ban gaya!**\n\n"
                "⚠️ Security ki wajah se login is bot ke through kabhi nahi hota — "
                "phone number ya OTP main kabhi nahi maangta. Login sirf ek baar, "
                "khud tumhare control me hoga — Google Colab ke through (free, "
                "kuch install nahi karna):\n\n"
                "**Kaise karna hai:**\n"
                "1️⃣ [colab.research.google.com](https://colab.research.google.com) kholo, "
                "**New Notebook** banao\n"
                "2️⃣ Neeche diya poora code ek cell me paste karo\n"
                "3️⃣ ▶️ Run dabao\n"
                "4️⃣ Cell ke neeche phone number aur OTP maangega — wahi type karke Enter dabao "
                "(ye seedha Telegram ko jata hai, mujhe kabhi nahi dikhta)\n\n"
                f"```\n{colab_snippet}\n```\n\n"
                "Login ho jaane ke ~1 minute baad (main Mongo automatically check karta rehta "
                "hoon) tumhara clone khud start ho jayega. **📋 My Clone Status** dabake confirm "
                "kar sakte ho.",
                buttons=MAIN_MENU,
            )
    except asyncio.TimeoutError:
        await factory.send_message(chat_id, "⏳ Time out ho gaya. **Make My Own Clone** se dobara try karo.", buttons=MAIN_MENU)
    except Exception as e:
        if "exclusive conversation" in str(e):
            log.warning(f"Wizard double-open blocked for {owner_id} (harmless).")
        else:
            log.error(f"Wizard error for {owner_id}: {e}")
            await factory.send_message(chat_id, f"❌ Kuch galat ho gaya: {e}", buttons=MAIN_MENU)
    finally:
        ACTIVE_WIZARDS.discard(chat_id)


# ────────────────────────────────────────────────
#   STATUS
# ────────────────────────────────────────────────
@factory.on(events.CallbackQuery(data=b"my_status"))
async def my_status(event):
    owner_id = event.sender_id
    if not config_path(owner_id).is_file():
        await event.answer("Aapka koi clone nahi bana hai.", alert=True)
        return

    if not session_path(owner_id).is_file():
        await safe_edit(event,
            "⏳ **Login baaki hai**\n\n"
            "Config ban chuka hai lekin login abhi complete nahi hua. "
            "Wizard ke message me diya gaya Colab code chalao.",
            buttons=MAIN_MENU,
        )
        return

    proc = running_procs.get(str(owner_id))
    alive = proc is not None and proc.poll() is None
    status_txt = "🟢 Running" if alive else "🟡 Login ho chuka hai, agle check me (≤15s) auto-start hoga"
    await safe_edit(event, f"📋 **Clone Status**\n{status_txt}", buttons=MAIN_MENU)


# ────────────────────────────────────────────────
#   DELETE CLONE
# ────────────────────────────────────────────────
@factory.on(events.CallbackQuery(data=b"delete_clone"))
async def delete_clone(event):
    owner_id = event.sender_id
    if not config_path(owner_id).is_file():
        await event.answer("Aapka koi clone nahi hai.", alert=True)
        return
    await safe_edit(event,
        "🗑️ **Pakka delete karna hai?**\n\n"
        "Ye tumhara clone stop kar dega aur uska config/session/data sab delete kar dega. "
        "Ye undo nahi ho sakta.",
        buttons=[
            [Button.inline("✅ Haan, delete karo", b"confirm_delete")],
            [Button.inline("❌ Nahi, cancel", b"back_main")],
        ],
    )


@factory.on(events.CallbackQuery(data=b"confirm_delete"))
async def confirm_delete(event):
    owner_id = event.sender_id
    owner_key = str(owner_id)
    proc = running_procs.pop(owner_key, None)
    if proc and proc.poll() is None:
        proc.terminate()
        log.info(f"Terminated clone process for {owner_id}")

    d = clone_dir(owner_id)
    try:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    except Exception as e:
        log.error(f"Delete error for {owner_id}: {e}")

    await mongo_delete_clone(owner_id)
    await safe_edit(event, "🗑️ Clone delete ho gaya.", buttons=MAIN_MENU)


# ────────────────────────────────────────────────
#   OWNER PANEL
# ────────────────────────────────────────────────
ADMIN_MENU = [
    [Button.inline("📢 Broadcast Message", b"adm_broadcast")],
    [Button.inline("👥 View Users", b"adm_users")],
    [Button.inline("🐞 View Errors", b"adm_errors")],
    [Button.inline("📡 Live Logs", b"adm_logs")],
    [Button.inline("📊 Stats", b"adm_stats")],
    [Button.inline("📦 Backup Now", b"adm_backup")],
    [Button.inline("🚫 Ban User", b"adm_ban"), Button.inline("✅ Unban User", b"adm_unban")],
]


@factory.on(events.CallbackQuery(data=b"adm_ban"))
async def adm_ban_start(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()
    chat_id = event.sender_id
    try:
        async with factory.conversation(chat_id, timeout=120) as conv:
            await conv.send_message(
                "🚫 **Ban User — numeric Telegram ID bhejo** jise ban karna hai.\n"
                "Iska clone turant stop + delete ho jayega. Cancel ke liye /cancel.",
            )
            r = await conv.get_response()
            if r.raw_text.strip() == "/cancel":
                await conv.send_message("❌ Cancel kar diya.", buttons=ADMIN_MENU)
                return
            if not r.raw_text.strip().isdigit():
                await conv.send_message("⚠️ Sirf numeric ID bhejo. Dobara **Ban User** try karo.", buttons=ADMIN_MENU)
                return
            target_id = int(r.raw_text.strip())
            await ban_owner(target_id, reason=f"banned by owner {chat_id}")
            try:
                await factory.send_message(target_id, "🚫 Aapko is bot se permanently ban kar diya gaya hai.")
            except Exception:
                pass
            await conv.send_message(f"✅ `{target_id}` ban ho gaya — clone stop/delete ho gaya.", buttons=ADMIN_MENU)
    except asyncio.TimeoutError:
        await factory.send_message(chat_id, "⏳ Time out ho gaya.", buttons=ADMIN_MENU)


@factory.on(events.CallbackQuery(data=b"adm_unban"))
async def adm_unban_start(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()
    chat_id = event.sender_id
    try:
        async with factory.conversation(chat_id, timeout=120) as conv:
            await conv.send_message("✅ **Unban User — numeric Telegram ID bhejo.** Cancel ke liye /cancel.")
            r = await conv.get_response()
            if r.raw_text.strip() == "/cancel":
                await conv.send_message("❌ Cancel kar diya.", buttons=ADMIN_MENU)
                return
            if not r.raw_text.strip().isdigit():
                await conv.send_message("⚠️ Sirf numeric ID bhejo. Dobara **Unban User** try karo.", buttons=ADMIN_MENU)
                return
            target_id = int(r.raw_text.strip())
            await unban_owner(target_id)
            await conv.send_message(f"✅ `{target_id}` unban ho gaya.", buttons=ADMIN_MENU)
    except asyncio.TimeoutError:
        await factory.send_message(chat_id, "⏳ Time out ho gaya.", buttons=ADMIN_MENU)


def make_backup_zip() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_base = Path(tempfile.gettempdir()) / f"nexus_backup_{ts}"
    archive_path = shutil.make_archive(str(out_base), "zip", root_dir=str(CLONES_DIR))
    return Path(archive_path)


async def send_backup_to_owner(reason: str = "manual"):
    if FACTORY_OWNER_ID == 0:
        return
    try:
        zip_path = make_backup_zip()
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        await factory.send_file(
            FACTORY_OWNER_ID,
            str(zip_path),
            caption=f"📦 **Backup** ({reason})\n🗓️ {datetime.now():%Y-%m-%d %H:%M}\n💾 {size_mb:.1f} MB\n\n"
                    "⚠️ Isme sabhi clones ke config + login session hain — kisi ko forward mat karo.",
        )
        zip_path.unlink(missing_ok=True)
        log.info(f"Backup sent to owner ({reason})")
    except Exception as e:
        log.error(f"Backup send failed: {e}")


async def backup_scheduler():
    while True:
        await asyncio.sleep(AUTO_BACKUP_INTERVAL_SECONDS)
        await send_backup_to_owner(reason="daily auto-backup")


async def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="nexus factory is alive"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server listening on port {port}")


@factory.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_panel(event):
    if not is_factory_owner(event.sender_id):
        return
    await event.reply("👑 **Owner Panel**\n\nChuno:", buttons=ADMIN_MENU)


@factory.on(events.CallbackQuery(data=b"adm_back"))
async def admin_back(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await safe_edit(event, "👑 **Owner Panel**\n\nChuno:", buttons=ADMIN_MENU)


@factory.on(events.CallbackQuery(data=b"adm_broadcast"))
async def adm_broadcast(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()
    chat_id = event.chat_id
    try:
        async with factory.conversation(chat_id, timeout=300) as conv:
            await conv.send_message(
                "📢 **Broadcast — Step 1/2**\n\n"
                "Jo message bhejna hai wo yaha type karo. Markdown chalta hai. Cancel ke liye /cancel."
            )
            r = await conv.get_response()
            if r.raw_text.strip().lower() == "/cancel":
                await conv.send_message("❌ Cancel kar diya.", buttons=ADMIN_MENU)
                return
            msg_text = r.raw_text

            await conv.send_message(
                "**Step 2/2** — Message ke neeche ek button bhi laga sakte ho.\n"
                "Format: `Button Label | https://example.com`\nNahi chahiye to /skip bhejo."
            )
            r2 = await conv.get_response()
            btn_input = r2.raw_text.strip()
            buttons = None
            if btn_input.lower() != "/skip" and "|" in btn_input:
                label, url = btn_input.split("|", 1)
                label, url = label.strip(), url.strip()
                if label and url.startswith(("http://", "https://", "tg://")):
                    buttons = [[Button.url(label, url)]]
                else:
                    await conv.send_message("⚠️ Button URL http(s):// se shuru honi chahiye — button skip kar diya.")

            sent, failed = 0, 0
            for owner_key, _ in all_owner_dirs():
                try:
                    target_id = int(owner_key)
                except ValueError:
                    continue
                try:
                    await factory.send_message(
                        target_id, f"📢 **Announcement**\n\n{msg_text}",
                        buttons=buttons, parse_mode="markdown", link_preview=False,
                    )
                    sent += 1
                except Exception as e:
                    failed += 1
                    log.warning(f"Broadcast failed for {target_id}: {e}")
                await asyncio.sleep(0.3)

            await conv.send_message(
                f"✅ Broadcast bhej diya.\n📨 Sent: {sent}  ❌ Failed: {failed}", buttons=ADMIN_MENU,
            )
    except asyncio.TimeoutError:
        await factory.send_message(chat_id, "⏳ Time out ho gaya.", buttons=ADMIN_MENU)


@factory.on(events.CallbackQuery(data=b"adm_users"))
async def adm_users(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()

    rows = []
    for owner_key, owner_dir in all_owner_dirs():
        cfg = load_json(owner_dir / "config.json")
        sess_exists = (owner_dir / "nexus_session.txt").is_file()
        proc = running_procs.get(owner_key)
        alive = proc is not None and proc.poll() is None
        if not cfg:
            state = "⚠️ config missing"
        elif not sess_exists:
            state = "⏳ not logged in"
        elif alive:
            state = "🟢 running"
        else:
            state = "🟡 stopped"
        rows.append(f"• `{owner_key}` — {state}")

    text = "👥 **Users**\n\nAbhi tak koi clone register nahi hua." if not rows else \
        "👥 **Users** (" + str(len(rows)) + ")\n\n" + "\n".join(rows)
    text += f"\n\n🕐 Updated: {datetime.now():%H:%M:%S}"
    if len(text) > 3900:
        text = text[:3900] + "\n\n… (list truncated)"

    await safe_edit(event, text, buttons=[
        [Button.inline("🔄 Refresh", b"adm_users")],
        [Button.inline("⬅️ Back", b"adm_back")],
    ])


@factory.on(events.CallbackQuery(data=b"adm_errors"))
async def adm_errors(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()

    chunks = []
    factory_log = BASE_DIR / "nexus_factory.log"
    if factory_log.is_file():
        lines = factory_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        err_lines = [l for l in lines if "ERROR" in l or "WARNING" in l][-10:]
        if err_lines:
            chunks.append("**Factory log:**\n```\n" + "\n".join(err_lines) + "\n```")

    for owner_key, owner_dir in all_owner_dirs():
        for fname in ("nexus.log", "clone_output.log"):
            fpath = owner_dir / fname
            if not fpath.is_file():
                continue
            lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
            err_lines = [l for l in lines if "ERROR" in l][-5:]
            if err_lines:
                chunks.append(f"**Clone {owner_key} ({fname}):**\n```\n" + "\n".join(err_lines) + "\n```")

    text = "🐞 **Errors**\n\nAbhi tak koi error log nahi mila. Sab theek lag raha hai ✅" if not chunks else \
        "🐞 **Recent Errors**\n\n" + "\n\n".join(chunks)
    text += f"\n\n🕐 Updated: {datetime.now():%H:%M:%S}"
    if len(text) > 3900:
        text = text[:3900] + "\n\n… (truncated)"

    await safe_edit(event, text, buttons=[
        [Button.inline("🔄 Refresh", b"adm_errors")],
        [Button.inline("⬅️ Back", b"adm_back")],
    ])


@factory.on(events.CallbackQuery(data=b"adm_logs"))
async def adm_logs(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()

    factory_log = BASE_DIR / "nexus_factory.log"
    if not factory_log.is_file():
        text = "📡 **Live Logs**\n\nAbhi tak koi log file nahi mili."
    else:
        lines = factory_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        tail_lines = lines[-35:]
        log_text = "\n".join(tail_lines) if tail_lines else "(khali)"
        text = "📡 **Live Logs** (last 35 lines)\n\n```\n" + log_text + "\n```"

    text += f"\n\n🕐 Updated: {datetime.now():%H:%M:%S}"
    if len(text) > 3900:
        overflow = len(text) - 3850
        text = "📡 **Live Logs** (truncated)\n\n```\n…" + text[overflow:]

    await safe_edit(event, text, buttons=[
        [Button.inline("🔄 Refresh", b"adm_logs")],
        [Button.inline("⬅️ Back", b"adm_back")],
    ])


@factory.on(events.CallbackQuery(data=b"adm_stats"))
async def adm_stats(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer()

    total = logged_in = running = 0
    for owner_key, owner_dir in all_owner_dirs():
        total += 1
        if (owner_dir / "nexus_session.txt").is_file():
            logged_in += 1
        proc = running_procs.get(owner_key)
        if proc is not None and proc.poll() is None:
            running += 1

    text = (
        "📊 **Stats**\n\n"
        f"👥 Total registered clones: **{total}**\n"
        f"🔑 Logged in: **{logged_in}**\n"
        f"🟢 Currently running: **{running}**\n\n"
        f"🕐 Updated: {datetime.now():%H:%M:%S}"
    )
    await safe_edit(event, text, buttons=[
        [Button.inline("🔄 Refresh", b"adm_stats")],
        [Button.inline("⬅️ Back", b"adm_back")],
    ])


@factory.on(events.CallbackQuery(data=b"adm_backup"))
async def adm_backup(event):
    if not is_factory_owner(event.sender_id):
        await event.answer("⛔ Ye sirf owner ke liye hai.", alert=True)
        return
    await event.answer("📦 Backup bana raha hoon…")
    await send_backup_to_owner(reason="manual (owner panel)")
    await safe_edit(event,
        "✅ Backup tumhari DM me bhej diya (isi chat me upar dekho).",
        buttons=[[Button.inline("⬅️ Back", b"adm_back")]],
    )


# ────────────────────────────────────────────────
#   BACKGROUND WATCHER
# ────────────────────────────────────────────────
def start_clone_process(owner_key: str, owner_dir: Path, cfg_file: Path):
    log_path = owner_dir / "clone_output.log"
    logf = open(log_path, "a", encoding="utf-8")
    p = subprocess.Popen(
        [sys.executable, str(CORE_SCRIPT), "--config", str(cfg_file)],
        stdout=logf, stderr=subprocess.STDOUT,
        cwd=str(owner_dir),
    )
    running_procs[owner_key] = p
    log.info(f"(Re)started clone for owner {owner_key} (pid {p.pid})")


async def handle_clone_crash(owner_key: str, owner_dir: Path, exit_code: Optional[int]) -> bool:
    log_path = owner_dir / "clone_output.log"
    tail = ""
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        tail = "\n".join(lines[-15:])

    session_invalid = any(
        marker in tail for marker in
        ("AuthKeyInvalidError", "AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError", "SessionPasswordNeededError")
    )

    try:
        owner_id = int(owner_key)
    except ValueError:
        owner_id = None

    if session_invalid:
        text = (
            "🔑 **Session Expired / Invalid**\n\n"
            "Tumhara clone ka login session invalid ho gaya hai. Jab tak dobara login nahi "
            "karte, bot start nahi hoga. **Make My Own Clone** delete karke fresh se login karo."
        )
        try:
            (owner_dir / "nexus_session.txt").unlink(missing_ok=True)
            if mongo_clones is not None and owner_id is not None:
                await mongo_clones.update_one({"owner_id": owner_id}, {"$unset": {"session_string": ""}})
        except Exception:
            pass
        should_restart = False
    else:
        text = (
            "⚠️ **Clone Crash — Restarting**\n\n"
            f"Tumhara clone crash ho gaya (exit code: {exit_code}). Main ise turant restart kar raha hoon.\n\n"
            f"**Last log lines:**\n```\n{(tail[-800:] if tail else 'log khali hai')}\n```"
        )
        should_restart = True

    if owner_id:
        try:
            await factory.send_message(owner_id, text)
        except Exception as e:
            log.warning(f"Could not DM clone owner {owner_id}: {e}")
    if FACTORY_OWNER_ID and owner_id != FACTORY_OWNER_ID:
        try:
            await factory.send_message(FACTORY_OWNER_ID, f"🐞 Clone `{owner_key}` crashed.\n\n{text}")
        except Exception:
            pass

    return should_restart


async def process_watcher():
    while True:
        try:
            for owner_dir in CLONES_DIR.iterdir():
                if not owner_dir.is_dir():
                    continue
                owner_key = owner_dir.name
                cfg_file = owner_dir / "config.json"
                sess_file = owner_dir / "nexus_session.txt"
                if not cfg_file.is_file() or not sess_file.is_file():
                    continue

                proc = running_procs.get(owner_key)
                if proc is None:
                    start_clone_process(owner_key, owner_dir, cfg_file)
                elif proc.poll() is not None:
                    exit_code = proc.returncode
                    running_procs.pop(owner_key, None)
                    log.warning(f"Clone {owner_key} exited (code {exit_code})")
                    should_restart = await handle_clone_crash(owner_key, owner_dir, exit_code)
                    if should_restart and sess_file.is_file():
                        start_clone_process(owner_key, owner_dir, cfg_file)
        except Exception as e:
            log.error(f"process_watcher error: {e}")
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)


async def resource_guard():
    while True:
        await asyncio.sleep(30)
        for owner_key, proc in list(running_procs.items()):
            if proc.poll() is not None:
                continue
            try:
                ps_proc = psutil.Process(proc.pid)
                cpu = ps_proc.cpu_percent(interval=0.5)
                mem_mb = ps_proc.memory_info().rss / (1024 * 1024)
                if cpu > MAX_CPU_PERCENT or mem_mb > MAX_MEM_MB:
                    log.warning(f"Clone {owner_key} over limits (cpu={cpu:.0f}%, mem={mem_mb:.0f}MB) — restarting")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    running_procs.pop(owner_key, None)
                    try:
                        owner_id = int(owner_key)
                        await factory.send_message(
                            owner_id,
                            "🛡️ **Resource Guard**\n\nTumhara clone high resource use kar raha "
                            f"tha (CPU {cpu:.0f}%, RAM {mem_mb:.0f}MB) — safety ke liye restart "
                            "kar diya. Agle check (≤15s) me wapas start ho jayega.",
                        )
                    except Exception:
                        pass
            except psutil.NoSuchProcess:
                continue
            except Exception as e:
                log.error(f"resource_guard error for {owner_key}: {e}")


# ────────────────────────────────────────────────
#   START
# ────────────────────────────────────────────────
async def main():
    await start_health_server()
    await factory.start(bot_token=BOT_TOKEN)
    me = await factory.get_me()
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("🏭 NEXUS CLONE FACTORY STARTED")
    log.info(f"🤖 @{me.username}")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if mongo_clones is not None:
        await mongo_hydrate_all()
    await load_blacklist()
    asyncio.create_task(process_watcher())
    asyncio.create_task(backup_scheduler())
    asyncio.create_task(resource_guard())
    asyncio.create_task(mongo_sync_new_sessions())
    await factory.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
