#!/usr/bin/env python3
# NEXUS USERBOT — clone-factory core (per-user config, no hardcoded secrets)
#
# Run with: python nexus_core.py --config /path/to/config.json
# --login-only : interactive phone/OTP login in THIS terminal only, saves
#                the session, then exits (never through any bot chat).

import argparse
import os, sys, json, asyncio, random, re, time, logging, shutil
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.tl.types import ChatBannedRights, User
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
from telethon.tl.functions.channels import EditBannedRequest, GetParticipantRequest
from telethon.tl.functions.messages import EditChatTitleRequest
from telethon.tl.functions.channels import EditTitleRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

from telethon.sessions import StringSession

# ═══════════════ CONFIG — loaded from a per-clone JSON file ═══════════════
parser = argparse.ArgumentParser(description="Nexus Userbot clone (per-user config)")
parser.add_argument("--config", required=True, help="Path to this clone's config.json")
parser.add_argument("--login-only", action="store_true",
                     help="Only perform interactive login and save the session, then exit")
ARGS = parser.parse_args()

CONFIG_PATH = Path(ARGS.config).resolve()
if not CONFIG_PATH.is_file():
    print(f"❌ Config file not found: {CONFIG_PATH}")
    sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    _cfg = json.load(f)

API_ID = int(_cfg["api_id"])
API_HASH = str(_cfg["api_hash"])
OWNER_ID = int(_cfg["owner_id"])
SESSION_FILE = _cfg.get("session_file", str(CONFIG_PATH.parent / "nexus_session.txt"))
DATA_FILE = Path(_cfg.get("data_file", str(CONFIG_PATH.parent / "nexus_data.json")))
LOG_FILE = _cfg.get("log_file", str(CONFIG_PATH.parent / "nexus.log"))

# Optional MongoDB sync — mirrors funbot_core.py's pattern so the factory
# can rebuild this clone's session on a fresh container (no persistent disk).
MONGODB_URI = _cfg.get("mongodb_uri", "")
MONGODB_DB_NAME = _cfg.get("mongodb_db_name", "nexus_factory")
MONGO_OWNER_KEY = _cfg.get("factory_owner_telegram_id", OWNER_ID)

# ═══════════════ PATHS ═══════════════
DATA_DIR = CONFIG_PATH.parent
GHOST_PHOTOS_DIR = DATA_DIR / "ghost_photos"
GHOST_PHOTOS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("NEXUS_USERBOT")


# ═══════════════ DATA MANAGER ═══════════════
class BotDB:
    def __init__(self):
        self.owner_id = OWNER_ID
        self.admins = []
        self.ghost_data = {}
        self.ghost_on = False
        self.auto_react_dm_enabled = False
        self.auto_react_group_enabled = False
        self.auto_react_emojis = [
            '🥰', '😭', '😂', '🤗', '🌝', '😇', '🤩', '😎', '🫡', '🤝',
            '👍', '❤️', '❤️‍🔥', '🔥', '🙈', '👀', '😁', '🤨', '🕊', '🎉',
            '👏', '⚡️', '🤷', '👌', '🧑‍💻', '💔', '✍️', '👻', '🤔'
        ]
        self.auto_reply = {"enabled": False, "text": "Owner is currently offline. Please wait!"}
        self.pmguard = {"enabled": False}
        self.pm_warnings = {}
        self.approved = []
        self.blacklist = []
        self.muted = {}
        self.gmuted = {}
        self.blocklist = []
        self.locked_groups = []
        self.start_time = datetime.now()
        self.load()

    def load(self):
        if DATA_FILE.exists():
            try:
                d = json.loads(DATA_FILE.read_text())
                for key in d:
                    if hasattr(self, key):
                        setattr(self, key, d[key])
            except: pass

    def save(self):
        DATA_FILE.write_text(json.dumps({
            'owner_id': self.owner_id,
            'admins': self.admins,
            'ghost_data': self.ghost_data,
            'ghost_on': self.ghost_on,
            'auto_react_dm_enabled': self.auto_react_dm_enabled,
            'auto_react_group_enabled': self.auto_react_group_enabled,
            'auto_react_emojis': self.auto_react_emojis,
            'auto_reply': self.auto_reply,
            'pmguard': self.pmguard,
            'pm_warnings': self.pm_warnings,
            'approved': self.approved,
            'blacklist': self.blacklist,
            'muted': self.muted,
            'gmuted': self.gmuted,
            'blocklist': self.blocklist,
            'locked_groups': self.locked_groups,
        }, indent=2))

db = BotDB()

# ═══════════════ SESSION SUPPORT ═══════════════
def _load_string_session() -> str:
    if os.path.isfile(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            log.error(f"Could not read {SESSION_FILE}: {e}")
    return ""


def _save_string_session(session_str: str):
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            f.write(session_str)
        os.chmod(SESSION_FILE, 0o600)
    except Exception as e:
        log.error(f"Could not save {SESSION_FILE}: {e}")

    if MONGODB_URI:
        try:
            from pymongo import MongoClient
            mclient = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
            mclient[MONGODB_DB_NAME]["clones"].update_one(
                {"owner_id": MONGO_OWNER_KEY},
                {"$set": {"session_string": session_str}},
                upsert=True,
            )
            mclient.close()
            log.info("Session string synced to MongoDB.")
        except Exception as e:
            log.warning(f"MongoDB session sync failed (non-fatal, local file still saved): {e}")


client = TelegramClient(StringSession(_load_string_session()), API_ID, API_HASH)

# ═══════════════ HELPERS ═══════════════
def get_args(msg):
    t = msg.text or ''
    for m in re.finditer(r'\.\w+\s*(.*)', t):
        return m.group(1).strip()
    return ''

def is_owner(uid): return uid == db.owner_id
def is_admin(uid): return uid == db.owner_id or uid in db.admins

async def resolve_user(client, msg, text):
    if msg.is_reply:
        return (await msg.get_reply_message()).sender_id
    if text:
        m = re.search(r'@(\w+)', text)
        if m:
            try:
                u = await client.get_entity(m.group(1))
                return u.id
            except: return None
        try:
            u = await client.get_entity(int(text))
            return u.id
        except: return None
    return None

def user_link(uid):
    return f"[User](tg://user?id={uid})"

async def delete_command_message(event, delay=2):
    try:
        await asyncio.sleep(delay)
        await event.delete()
    except: pass

# ═══════════════ COMMAND DECORATOR ═══════════════
def cmd(pattern: str, owner_only: bool = False, delete_cmd: bool = True):
    def wrapper(func):
        @client.on(events.NewMessage(pattern=rf'^\.{pattern}(\s.*)?$'))
        async def handler(event):
            if owner_only and not is_owner(event.sender_id):
                return
            if not owner_only and not is_owner(event.sender_id) and not is_admin(event.sender_id):
                return
            try:
                if delete_cmd:
                    asyncio.create_task(delete_command_message(event, 2))
                await func(event)
            except FloodWaitError as e:
                log.warning(f"Flood wait {e.seconds}s")
                await event.reply(f"⏳ Flood wait {e.seconds} sec...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                log.error(f"Cmd .{pattern} error: {e}")
                await event.reply(f"❌ Error: {str(e)[:200]}")
        return handler
    return wrapper# ═══════════════ BASIC COMMANDS ═══════════════
@cmd('help', delete_cmd=False)
async def help_cmd(event):
    await event.reply("""**⚙️ NEXUS USERBOT ⚙️**

**🔹 Basic Commands**
`.help` — Show menu
`.alive` — Check status
`.profile` — Your profile
`.id` — Get ID
`.info` — Get user info

**🔹 Ghost Mode**
`.ghost` — Hide your profile (name/photo/bio) temporarily
`.unghost` — Restore your profile

**🔹 Admin Commands**
`.admins` — Admin list
`.addadmin <reply/@id>` — Add admin
`.deladmin <reply/@id>` — Remove admin

**🔹 Mute & Restrict**
`.mute <reply/@id>` — Local mute
`.unmute <reply/@id>` — Unmute
`.gmute <reply/@id>` — Global mute (deletes all messages)
`.gunmute <reply/@id>` — Global unmute
`.lock` — Lock group (only Add Users & Reactions allowed)
`.unlock` — Unlock group (basic permissions, but some remain off)

**🔹 Group Mod**
`.tagall text` — Tag all members (visible mention)
`.purge` — **Reply to a message to delete ALL messages below it (unlimited)**
`.throw <reply/@id>` — Kick user

**🔹 Auto System**
`.autoreact on` — Enable auto‑reaction in DMs
`.autoreact off` — Disable auto‑reaction in DMs
`.autoreactg on` — Enable auto‑reaction in all groups
`.autoreactg off` — Disable auto‑reaction in groups
`.autoreply set text` — Set reply
`.autoreply on/off` — Toggle reply
`.pmguard on/off` — PM Guard
`.approve` — Approve
`.disapprove` — Disapprove
`.blacklist` — Blacklist
`.removeblacklist` — Remove blacklist

**🔹 Fun**
`.ping` — Latency
`.flip` — Coin flip
`.dice` — Roll dice
""", parse_mode='markdown')

@cmd('alive', delete_cmd=False)
async def alive_cmd(event):
    uptime = datetime.now() - db.start_time
    h, r = divmod(int(uptime.total_seconds()), 3600)
    m, s = divmod(r, 60)
    await event.reply(f"✅ **ALIVE**\n⏱ {h}h {m}m {s}s", parse_mode='markdown')

@cmd('profile', delete_cmd=False)
async def profile_cmd(event):
    me = await client.get_me()
    await event.reply(f"**👤 Your Profile**\nName: {me.first_name}\nID: `{me.id}`\nUsername: @{me.username or 'None'}", parse_mode='markdown')

@cmd('id', delete_cmd=False)
async def id_cmd(event):
    if event.is_reply:
        msg = await event.get_reply_message()
        uid = msg.sender_id
        await event.reply(f"**User ID:** `{uid}`", parse_mode='markdown')
    else:
        await event.reply(f"**Chat ID:** `{event.chat_id}`", parse_mode='markdown')

@cmd('info', delete_cmd=False)
async def info_cmd(event):
    text = get_args(event.message)
    uid = await resolve_user(client, event.message, text)
    if not uid:
        return await event.reply("❌ User not found.")
    try:
        u = await client.get_entity(uid)
        await event.reply(f"**👤 User Info**\nName: {u.first_name}\nID: `{u.id}`\nUsername: @{u.username or 'None'}", parse_mode='markdown')
    except:
        await event.reply("❌ Could not get user info.")

# ═══════════════ GHOST MODE ═══════════════
@cmd('ghost')
async def ghost_cmd(event):
    if not is_owner(event.sender_id):
        return
    if db.ghost_on:
        return await event.reply("👻 Ghost mode already active!")

    me = await client.get_me()
    db.ghost_data = {
        'first_name': me.first_name or '',
        'last_name': me.last_name or '',
        'bio': '',
    }
    try:
        full_me = await client(functions.users.GetFullUserRequest(id=me.id))
        db.ghost_data['bio'] = full_me.full_user.about or ''
    except:
        db.ghost_data['bio'] = ''

    photo_paths = []
    try:
        shutil.rmtree(GHOST_PHOTOS_DIR, ignore_errors=True)
        GHOST_PHOTOS_DIR.mkdir(exist_ok=True)
        all_photos = await client(functions.photos.GetUserPhotosRequest(
            user_id=me.id, offset=0, max_id=0, limit=100
        ))
        photos_list = list(all_photos.photos)
        photos_list.reverse()
        for i, photo in enumerate(photos_list):
            photo_path = str(GHOST_PHOTOS_DIR / f"ghost_photo_{i:03d}.jpg")
            downloaded = await client.download_media(photo, photo_path)
            if downloaded:
                photo_paths.append(photo_path)
        log.info(f"✅ Saved {len(photo_paths)} photos for ghost mode")
    except Exception as e:
        log.warning(f"Could not save photos for ghost: {e}")
    
    db.ghost_data['photo_paths'] = photo_paths
    
    try:
        await client(UpdateProfileRequest(first_name="Deleted", last_name="Account"))
        await client(functions.account.UpdateProfileRequest(about="This account has been deleted"))
        current_photos = await client(functions.photos.GetUserPhotosRequest(
            user_id=me.id, offset=0, max_id=0, limit=100
        ))
        if current_photos.photos:
            await client(DeletePhotosRequest(id=current_photos.photos))
        
        db.ghost_on = True
        db.save()
        await event.reply("👻 Ghost Mode Activated!")
    except Exception as e:
        await event.reply(f"❌ Ghost mode failed: {str(e)[:100]}")

@cmd('unghost')
async def unghost_cmd(event):
    if not is_owner(event.sender_id):
        return
    if not db.ghost_on:
        return await event.reply("❌ Ghost mode is not active!")

    gd = db.ghost_data
    try:
        await client(UpdateProfileRequest(
            first_name=gd.get('first_name', ''),
            last_name=gd.get('last_name', '')
        ))
        if gd.get('bio'):
            await client(functions.account.UpdateProfileRequest(about=gd['bio']))
        else:
            await client(functions.account.UpdateProfileRequest(about=""))
        
        photo_paths = gd.get('photo_paths', [])
        if photo_paths:
            me = await client.get_me()
            current_photos = await client(functions.photos.GetUserPhotosRequest(
                user_id=me.id, offset=0, max_id=0, limit=100
            ))
            if current_photos.photos:
                await client(DeletePhotosRequest(id=current_photos.photos))
            for photo_path in photo_paths:
                if os.path.exists(photo_path):
                    try:
                        await client(functions.photos.UploadProfilePhotoRequest(
                            file=await client.upload_file(photo_path)
                        ))
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        log.warning(f"Failed to restore photo {photo_path}: {e}")
        
        db.ghost_on = False
        db.ghost_data = {}
        db.save()
        shutil.rmtree(GHOST_PHOTOS_DIR, ignore_errors=True)
        GHOST_PHOTOS_DIR.mkdir(exist_ok=True)
        
        await event.reply("✅ Ghost Mode Deactivated!")
    except Exception as e:
        await event.reply(f"❌ Unghost failed: {str(e)[:100]}")

# ═══════════════ ADMIN COMMANDS ═══════════════
@cmd('admins')
async def admins_cmd(event):
    await event.reply(f"👑 Owner: `{db.owner_id}`\n**Admins:** {db.admins}", parse_mode='markdown')

@cmd('addadmin')
async def addadmin_cmd(event):
    if not is_owner(event.sender_id):
        return
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if uid in db.admins:
        return await event.reply("⏺️ Already admin.")
    db.admins.append(uid)
    db.save()
    await event.reply("✅ **Admin Added!**")

@cmd('deladmin')
async def deladmin_cmd(event):
    if not is_owner(event.sender_id):
        return
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if uid in db.admins:
        db.admins.remove(uid)
        db.save()
    await event.reply("✅ **Admin Removed!**")

# ═══════════════ MUTE & RESTRICT ═══════════════
@cmd('mute', delete_cmd=False)
async def mute_cmd(event):
    if not event.is_group:
        return
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    try:
        await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, send_messages=True)))
        await event.reply("🔇 **User Muted!**")
    except Exception as e:
        await event.reply(f"❌ {str(e)[:100]}")

@cmd('unmute', delete_cmd=False)
async def unmute_cmd(event):
    if not event.is_group:
        return
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    try:
        await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, send_messages=False)))
        await event.reply("🔊 **User Unmuted!**")
    except Exception as e:
        await event.reply(f"❌ {str(e)[:100]}")

@cmd('gmute')
async def gmute_cmd(event):
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    db.gmuted[str(uid)] = True
    db.save()
    await event.reply("🌐 **User Globally Muted!** (all messages will be deleted)")

@cmd('gunmute')
async def gunmute_cmd(event):
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if str(uid) in db.gmuted:
        del db.gmuted[str(uid)]
        db.save()
    await event.reply("🌐 **User Globally Unmuted!**")

# ==================== LOCK & UNLOCK ====================
def get_base_banned_rights():
    return ChatBannedRights(
        until_date=None,
        send_messages=False,
        send_media=False,
        send_stickers=True,
        send_gifs=True,
        send_games=False,
        send_inline=False,
        send_polls=False,
        embed_links=True,
        send_photos=False,
        send_videos=False,
        send_audios=False,
        send_voices=False,
        send_docs=False,
        pin_messages=True,
        change_info=True,
        invite_users=False,
    )

@cmd('lock')
async def lock_cmd(event):
    if not event.is_group:
        return await event.reply("❌ This is not a group.")
    try:
        base = get_base_banned_rights()
        base.send_messages = True
        base.send_media = True
        base.send_photos = True
        base.send_videos = True
        base.send_audios = True
        base.send_voices = True
        base.send_docs = True
        base.send_polls = True
        base.send_games = True
        base.send_inline = True
        await client(EditChatDefaultBannedRightsRequest(peer=event.chat_id, banned_rights=base))
        await event.reply("🔒 Group Locked!")
    except Exception as e:
        await event.reply(f"❌ Lock failed: {str(e)[:100]}")

@cmd('unlock')
async def unlock_cmd(event):
    if not event.is_group:
        return await event.reply("❌ This is not a group.")
    try:
        base = get_base_banned_rights()
        base.send_messages = False
        base.send_media = False
        base.send_photos = False
        base.send_videos = False
        base.send_audios = False
        base.send_voices = False
        base.send_docs = False
        base.send_polls = False
        base.send_games = False
        base.send_inline = False
        await client(EditChatDefaultBannedRightsRequest(peer=event.chat_id, banned_rights=base))
        await event.reply("🔓 Group Unlocked!")
    except Exception as e:
        await event.reply(f"❌ Unlock failed: {str(e)[:100]}")# ═══════════════ GROUP MOD ═══════════════
@cmd('tagall')
async def tagall_cmd(event):
    if not event.is_group:
        return await event.reply("❌ This is not a group.")
    text = get_args(event.message)
    chat_id = event.chat_id
    all_users = []
    try:
        async for user in client.iter_participants(chat_id):
            if not user.deleted and not user.bot:
                all_users.append(user)
    except Exception as e:
        await event.reply(f"❌ Failed to fetch members: {str(e)[:100]}")
        return
    if not all_users:
        await event.reply("❌ No members found or insufficient permissions.")
        return
    chunk_size = 50
    for i in range(0, len(all_users), chunk_size):
        chunk = all_users[i:i + chunk_size]
        mentions = []
        for user in chunk:
            name = user.first_name or "User"
            name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            mentions.append(f'<a href="tg://user?id={user.id}">{name}</a>')
        mention_str = ' '.join(mentions)
        if i == 0 and text:
            msg = f"{text}\n{mention_str}"
        else:
            msg = mention_str
        await event.respond(msg, parse_mode='html')
        await asyncio.sleep(1.5)

@cmd('purge', delete_cmd=True)
async def purge_cmd(event):
    if not event.is_reply:
        return await event.reply("❌ Reply to a message to delete all messages below it.\nExample: Reply to a message and type `.purge`")
    reply_msg = await event.get_reply_message()
    if not reply_msg:
        return await event.reply("❌ Could not fetch the replied message.")
    chat_id = event.chat_id
    start_id = reply_msg.id
    all_ids = []
    last_id = start_id
    while True:
        try:
            msgs = await client.get_messages(chat_id, min_id=last_id, limit=100, reverse=False)
            if not msgs:
                break
            ids = [m.id for m in msgs]
            all_ids.extend(ids)
            last_id = min(ids) if ids else start_id
            if len(msgs) < 100:
                break
        except Exception as e:
            await event.reply(f"❌ Error fetching messages: {str(e)[:100]}")
            return
    if not all_ids:
        return await event.reply("✅ No messages below the replied message to delete.")
    total_deleted = 0
    chunk_size = 100
    for i in range(0, len(all_ids), chunk_size):
        chunk = all_ids[i:i+chunk_size]
        try:
            await client.delete_messages(chat_id, chunk)
            total_deleted += len(chunk)
            await asyncio.sleep(0.5)
        except Exception as e:
            await event.reply(f"❌ Delete failed at chunk {i//chunk_size + 1}: {str(e)[:100]}")
            return
    await event.reply(f"✅ Purged **{total_deleted}** messages below the replied message.")

@cmd('throw')
async def throw_cmd(event):
    if not event.is_group:
        return
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    try:
        await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=True)))
        await asyncio.sleep(0.5)
        await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=False)))
        await event.reply("👢 **User Kicked!**")
    except Exception as e:
        await event.reply(f"❌ {str(e)[:100]}")

# ═══════════════ AUTO SYSTEM ═══════════════
@cmd('autoreact')
async def autoreact_cmd(event):
    arg = get_args(event.message).lower()
    if arg == 'on':
        db.auto_react_dm_enabled = True
        db.save()
        await event.reply("✅ **DM Auto‑React ON** – I'll react to all private messages.")
    elif arg == 'off':
        db.auto_react_dm_enabled = False
        db.save()
        await event.reply("✅ **DM Auto‑React OFF**")
    else:
        await event.reply("**Usage:** `.autoreact on` or `.autoreact off`")

@cmd('autoreactg')
async def autoreactg_cmd(event):
    arg = get_args(event.message).lower()
    if arg == 'on':
        db.auto_react_group_enabled = True
        db.save()
        await event.reply("✅ **Group Auto‑React ON** – I'll react to all group messages.")
    elif arg == 'off':
        db.auto_react_group_enabled = False
        db.save()
        await event.reply("✅ **Group Auto‑React OFF**")
    else:
        await event.reply("**Usage:** `.autoreactg on` or `.autoreactg off`")

@cmd('autoreply')
async def autoreply_cmd(event):
    text = get_args(event.message)
    parts = text.split(' ', 1)
    if not parts:
        return await event.reply("Usage: `.autoreply set <text>` | `.autoreply on` | `.autoreply off`")
    action = parts[0].lower()
    if action == 'set' and len(parts) > 1:
        db.auto_reply['text'] = parts[1]
        db.auto_reply['enabled'] = True
        db.save()
        await event.reply(f"✅ Auto reply set to:\n{parts[1]}")
    elif action == 'on':
        db.auto_reply['enabled'] = True
        db.save()
        await event.reply("✅ **Auto Reply ON**")
    elif action == 'off':
        db.auto_reply['enabled'] = False
        db.save()
        await event.reply("✅ **Auto Reply OFF**")
    else:
        await event.reply("Usage: `.autoreply set <text>` | `.autoreply on` | `.autoreply off`")

@cmd('pmguard')
async def pmguard_cmd(event):
    arg = get_args(event.message).lower()
    if arg == 'on':
        db.pmguard['enabled'] = True
        db.save()
        await event.reply("🛡️ **PM Guard ON**")
    elif arg == 'off':
        db.pmguard['enabled'] = False
        db.save()
        await event.reply("🛡️ **PM Guard OFF**")
    else:
        await event.reply("Usage: `.pmguard on` or `.pmguard off`")

@cmd('approve')
async def approve_cmd(event):
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if uid in db.approved:
        return await event.reply("✅ Already approved.")
    db.approved.append(uid)
    db.pm_warnings.pop(str(uid), None)
    try:
        await client(UnblockRequest(id=uid))
    except: pass
    db.save()
    await event.reply("✅ **User Approved!**")

@cmd('disapprove')
async def disapprove_cmd(event):
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if uid in db.approved:
        db.approved.remove(uid)
        db.save()
    await event.reply("✅ **User Disapproved!**")

@cmd('blacklist')
async def blacklist_cmd(event):
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if uid not in db.blacklist:
        db.blacklist.append(uid)
        db.save()
    await event.reply("🚫 **User Blacklisted!**")

@cmd('removeblacklist')
async def removeblacklist_cmd(event):
    uid = await resolve_user(client, event.message, get_args(event.message))
    if not uid:
        return await event.reply("❌ User not found.")
    if uid in db.blacklist:
        db.blacklist.remove(uid)
        db.save()
    await event.reply("✅ **User Removed from Blacklist!**")

# ═══════════════ PM GUARD HANDLER ═══════════════
@client.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def pm_guard_handler(event):
    uid = event.sender_id
    if uid == db.owner_id:
        return
    if not db.pmguard.get('enabled', False):
        return
    if uid in db.approved:
        return
    if uid in db.blacklist:
        await client(BlockRequest(id=uid))
        return
    w = db.pm_warnings.get(str(uid), 0) + 1
    db.pm_warnings[str(uid)] = w
    db.save()
    if w < 5:
        await event.reply(f"""**🔒 SECURITY ALERT**

Owner is currently busy/offline😌 Please wait and don't spam!

⚠️ Warning: {w}/5

🚫 5th warning = Automatic Block""")
    else:
        await event.reply("🚫 **You have been blocked for spamming!**")
        try:
            await client(BlockRequest(id=uid))
            if uid not in db.blocklist:
                db.blocklist.append(uid)
                db.save()
        except: pass

# ═══════════════ GLOBAL MUTE FILTER ═══════════════
@client.on(events.NewMessage)
async def gmute_filter(event):
    if event.out:
        return
    uid = event.sender_id
    if str(uid) in db.gmuted:
        try:
            await event.delete()
            if event.is_private:
                await client(BlockRequest(id=uid))
        except Exception as e:
            log.debug(f"Could not delete gmuted message: {e}")

# ═══════════════ AUTO-REACT HANDLER ═══════════════
@client.on(events.NewMessage)
async def auto_react_handler(event):
    if event.out:
        return
    if event.is_private:
        if db.auto_react_dm_enabled:
            emoji = random.choice(db.auto_react_emojis)
            try:
                await client(SendReactionRequest(
                    peer=event.chat_id,
                    msg_id=event.message.id,
                    reaction=[ReactionEmoji(emoticon=emoji)]
                ))
                print(f"✅ DM react: {emoji} on msg {event.message.id}")
            except Exception as e:
                print(f"❌ DM react error: {e}")
    else:
        if db.auto_react_group_enabled:
            emoji = random.choice(db.auto_react_emojis)
            try:
                await client(SendReactionRequest(
                    peer=event.chat_id,
                    msg_id=event.message.id,
                    reaction=[ReactionEmoji(emoticon=emoji)]
                ))
                print(f"✅ Group react: {emoji} on msg {event.message.id}")
            except Exception as e:
                print(f"❌ Group react error: {e}")

# ═══════════════ AUTO REPLY HANDLER ═══════════════
@client.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def auto_reply_handler(event):
    uid = event.sender_id
    if uid == db.owner_id:
        return
    if db.auto_reply.get('enabled', False):
        text = db.auto_reply.get('text', 'Owner is currently offline. Please wait!')
        await asyncio.sleep(0.5)
        await event.reply(text)

# ═══════════════ FUN COMMANDS ═══════════════
@cmd('ping', delete_cmd=False)
async def ping_cmd(event):
    start = time.time()
    msg = await event.reply("🏓 Pong!")
    await msg.edit(f"🏓 **Pong!** `{round((time.time()-start)*1000,2)}ms`", parse_mode='markdown')

@cmd('flip', delete_cmd=False)
async def flip_cmd(event):
    await event.reply(f"{random.choice(['**Heads** 🪙', '**Tails** 🪙'])}", parse_mode='markdown')

@cmd('dice', delete_cmd=False)
async def dice_cmd(event):
    await event.reply(f"🎲 **Dice:** `{random.randint(1,6)}`", parse_mode='markdown')

# ═══════════════ STARTUP ═══════════════
async def login_only():
    """Interactive phone/OTP login directly in THIS terminal (Colab/local),
    never through any bot chat — then saves the session and exits."""
    await client.start()
    _save_string_session(client.session.save())
    me = await client.get_me()
    print(f"✅ Login successful — logged in as {me.first_name} (@{me.username}).")
    print("Session saved. You can now let the factory bot start this clone.")
    await client.disconnect()


async def main():
    await client.start()
    _save_string_session(client.session.save())
    me = await client.get_me()
    db.owner_id = me.id
    db.save()
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("🤖 NEXUS USERBOT STARTED")
    log.info(f"👤 Logged in as → {me.first_name} (@{me.username})")
    log.info(f"🆔 User ID → {me.id}")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    await client.run_until_disconnected()


if __name__ == '__main__':
    if ARGS.login_only:
        asyncio.run(login_only())
    else:
        asyncio.run(main())
