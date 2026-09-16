#!/usr/bin/env python3
# botcore.py — NEXUS logic engine · MongoDB persistence

import os, asyncio, random, re, time, logging, shutil
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from telethon import events, functions
from telethon.tl.types import ChatBannedRights
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.functions.messages import (
    EditChatDefaultBannedRightsRequest, EditChatTitleRequest, SendReactionRequest
)
from telethon.tl.functions.channels import EditBannedRequest, EditTitleRequest
from telethon.tl.types import ReactionEmoji

# ─── PATHS ───────────────────────────────────────────────────────
DATA_DIR         = Path("nexus_data");       DATA_DIR.mkdir(exist_ok=True)
SESS_DIR         = Path("sessions");         SESS_DIR.mkdir(exist_ok=True)
PHOTOS_DIR       = DATA_DIR / "original_photos"; PHOTOS_DIR.mkdir(exist_ok=True)
GHOST_PHOTOS_DIR = DATA_DIR / "ghost_photos";    GHOST_PHOTOS_DIR.mkdir(exist_ok=True)

log = logging.getLogger("NEXUS_USERBOT")

# ─── MONGODB ─────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_mongo    = AsyncIOMotorClient(MONGO_URI)
_mdb      = _mongo["nexus_userbot"]
_col      = _mdb["state"]

# ─── DATABASE ────────────────────────────────────────────────────
class BotDB:
    def __init__(self):
        self.owner_id                 = 0
        self.admins                   = []
        self.ghost_data               = {}
        self.ghost_on                 = False
        self.auto_react_dm_enabled    = False
        self.auto_react_group_enabled = False
        self.auto_react_emojis        = [
            '🥰','😭','😂','🤗','🌝','😇','🤩','😎','🫡','🤝',
            '👍','❤️','❤️‍🔥','🔥','🙈','👀','😁','🤨','🕊','🎉',
            '👏','⚡️','🤷','👌','🧑‍💻','💔','✍️','👻','🤔'
        ]
        self.auto_reply   = {"enabled": False, "text": "Owner is currently offline. Please wait!"}
        self.pmguard      = {"enabled": False}
        self.pm_warnings  = {}
        self.approved     = []
        self.blacklist    = []
        self.muted        = {}
        self.gmuted       = {}
        self.blocklist    = []
        self.locked_groups = []
        self.clone_data   = {}
        # in-memory only (not persisted — reset on restart, intentional)
        self.reply_raid   = {}
        self.rr_raid      = {}
        self.flag_raid    = {}
        self.heart_raid   = {}
        self.spam_running = False
        # persisted
        self.fast_gc_data = {}
        self.start_time   = datetime.now()

    def _to_dict(self) -> dict:
        return {
            'owner_id':                 self.owner_id,
            'admins':                   self.admins,
            'ghost_data':               self.ghost_data,
            'ghost_on':                 self.ghost_on,
            'auto_react_dm_enabled':    self.auto_react_dm_enabled,
            'auto_react_group_enabled': self.auto_react_group_enabled,
            'auto_react_emojis':        self.auto_react_emojis,
            'auto_reply':               self.auto_reply,
            'pmguard':                  self.pmguard,
            'pm_warnings':              self.pm_warnings,
            'approved':                 self.approved,
            'blacklist':                self.blacklist,
            'muted':                    self.muted,
            'gmuted':                   self.gmuted,
            'blocklist':                self.blocklist,
            'locked_groups':            self.locked_groups,
            'clone_data':               self.clone_data,
            'fast_gc_data':             self.fast_gc_data,
        }

    async def load(self):
        doc = await _col.find_one({"_id": "state"})
        if doc:
            for k, v in doc.items():
                if k != "_id" and hasattr(self, k):
                    setattr(self, k, v)
        log.info("✅ DB loaded from MongoDB")

    async def save(self):
        await _col.update_one(
            {"_id": "state"},
            {"$set": self._to_dict()},
            upsert=True
        )

db = BotDB()

# ─── CLIENT REFERENCE (injected by setup()) ──────────────────────
_client = None

# ─── HANDLER REGISTRIES ──────────────────────────────────────────
_cmd_handlers: list = []   # (pattern, owner_only, delete_cmd, func)
_raw_handlers: list = []   # (event_builder, func)

# ─── CONSTANTS ───────────────────────────────────────────────────
RAID_MESSAGES = [
    "🚀 Raid #{n}!", "💥 Spam #{n}!", "🔥 Attack #{n}!",
    "⚡ Raid #{n}", "🌀 Flood #{n}"
]
REPLY_MESSAGES = [
    "💬 Spam reply!", "🤖 Bot attack!", "📢 Hey there!",
    "⚠️ Raid mode!", "🌀 Flooding!"
]
SPAM_EMOJIS        = ['🌟','✨','🔥','💥','⚡','🌀','🚀','🎯','💫','⭐']
DEFAULT_SPAM_TEXTS = [
    "🔥 Spam attack!","🚀 Flooding the chat!","💥 Here comes the spam!",
    "🌀 Spam mode activated!","⚡ Lightning spam!","🎯 Target acquired!",
    "💫 Spam spam spam!","🌟 Spam with style!","✨ Spam is life!",
    "🔥 Spam till you drop!"
]

# ─── HELPERS ─────────────────────────────────────────────────────
def get_args(msg) -> str:
    t = msg.text or ''
    for m in re.finditer(r'\.\w+\s*(.*)', t):
        return m.group(1).strip()
    return ''

def is_owner(uid: int) -> bool: return uid == db.owner_id
def is_admin(uid: int) -> bool: return uid == db.owner_id or uid in db.admins
def user_link(uid: int) -> str: return f"[User](tg://user?id={uid})"

async def resolve_user(msg, text: str):
    if msg.is_reply:
        return (await msg.get_reply_message()).sender_id
    if text:
        m = re.search(r'@(\w+)', text)
        if m:
            try: return (await _client.get_entity(m.group(1))).id
            except: return None
        try: return (await _client.get_entity(int(text))).id
        except: return None
    return None

async def delete_command_message(event, delay: int = 2):
    try:
        await asyncio.sleep(delay)
        await event.delete()
    except: pass

async def raid_loop(event, target_id, raid_dict: dict, message_func, delay: float = 1):
    chat_id = event.chat_id
    raid_dict.setdefault(chat_id, set()).add(target_id)
    while target_id in raid_dict.get(chat_id, set()):
        try:
            await message_func()
            await asyncio.sleep(delay)
        except: break

# ─── DECORATORS ──────────────────────────────────────────────────
def cmd(pattern: str, owner_only: bool = False, delete_cmd: bool = True):
    def wrapper(func):
        _cmd_handlers.append((pattern, owner_only, delete_cmd, func))
        return func
    return wrapper

def raw(event_builder):
    def wrapper(func):
        _raw_handlers.append((event_builder, func))
        return func
    return wrapper

# ════════════════════════════════════════════════════════════════
#  COMMANDS
# ════════════════════════════════════════════════════════════════

@cmd('help', delete_cmd=False)
async def help_cmd(event):
    await event.reply("""**⚙️ NEXUS USERBOT ⚙️**

**🔹 Basic**
`.help` `.alive` `.profile` `.id` `.info`

**🔹 Profile**
`.clone @user` `.reclone` `.ghost` `.unghost`

**🔹 Admin**
`.admins` `.addadmin` `.deladmin`

**🔹 Mute & Restrict**
`.mute` `.unmute` `.gmute` `.gunmute` `.lock` `.unlock`

**🔹 Group Mod**
`.tagall text` `.raid count` `.purge` `.throw` `.stop`

**🔹 Auto System**
`.autoreact on/off` `.autoreactg on/off`
`.autoreply set/on/off` `.pmguard on/off`
`.approve` `.disapprove` `.blacklist` `.removeblacklist`

**🔹 Raid Engine**
`.reply` `.sreply` `.rr` `.srr`
`.flag` `.sflag` `.hrr` `.shrr`

**🔹 Spam**
`.spray [text]` `.dspray`
`.fastgc set text` `.fastgc stop`

**🔹 Fun**
`.ping` `.flip` `.dice`
""", parse_mode='markdown')


@cmd('alive', delete_cmd=False)
async def alive_cmd(event):
    h, r = divmod(int((datetime.now() - db.start_time).total_seconds()), 3600)
    m, s = divmod(r, 60)
    await event.reply(f"✅ **ALIVE**\n⏱ {h}h {m}m {s}s", parse_mode='markdown')


@cmd('profile', delete_cmd=False)
async def profile_cmd(event):
    me = await _client.get_me()
    await event.reply(
        f"**👤 Profile**\nName: {me.first_name}\nID: `{me.id}`\nUsername: @{me.username or 'None'}",
        parse_mode='markdown'
    )


@cmd('id', delete_cmd=False)
async def id_cmd(event):
    if event.is_reply:
        msg = await event.get_reply_message()
        await event.reply(f"**User ID:** `{msg.sender_id}`", parse_mode='markdown')
    else:
        await event.reply(f"**Chat ID:** `{event.chat_id}`", parse_mode='markdown')


@cmd('info', delete_cmd=False)
async def info_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ User not found.")
    try:
        u = await _client.get_entity(uid)
        await event.reply(
            f"**👤 User Info**\nName: {u.first_name}\nID: `{u.id}`\nUsername: @{u.username or 'None'}",
            parse_mode='markdown'
        )
    except:
        await event.reply("❌ Could not fetch user.")


# ─── PROFILE SYSTEM ──────────────────────────────────────────────

@cmd('clone')
async def clone_cmd(event):
    if not is_owner(event.sender_id): return
    target_id = await resolve_user(event.message, get_args(event.message))
    if not target_id: return await event.reply("❌ Target not found.")
    me = await _client.get_me()
    db.clone_data = {
        'orig_first_name': me.first_name or '',
        'orig_last_name':  me.last_name  or '',
        'orig_bio':        '',
    }
    try:
        fm = await _client(functions.users.GetFullUserRequest(id=me.id))
        db.clone_data['orig_bio'] = fm.full_user.about or ''
    except: pass
    try:
        shutil.rmtree(PHOTOS_DIR); PHOTOS_DIR.mkdir(exist_ok=True)
        ap = await _client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
        saved = []
        for i, photo in enumerate(reversed(ap.photos)):
            p = str(PHOTOS_DIR / f"orig_{i:03d}.jpg")
            if await _client.download_media(photo, p): saved.append(p)
        db.clone_data['orig_photo_paths'] = saved
    except:
        db.clone_data['orig_photo_paths'] = []
    try:
        target = await _client.get_entity(target_id)
        await _client(UpdateProfileRequest(
            first_name=getattr(target, 'first_name', '') or '',
            last_name=getattr(target, 'last_name', '')  or ''
        ))
        try:
            ft  = await _client(functions.users.GetFullUserRequest(id=target_id))
            bio = getattr(ft.full_user, 'about', '') or ''
            if bio: await _client(functions.account.UpdateProfileRequest(about=bio))
        except: pass
        try:
            tp = await _client(functions.photos.GetUserPhotosRequest(user_id=target_id, offset=0, max_id=0, limit=1))
            if tp.photos:
                pp = str(DATA_DIR / "clone_temp.jpg")
                await _client.download_media(tp.photos[0], pp)
                if os.path.exists(pp):
                    await _client(UploadProfilePhotoRequest(file=await _client.upload_file(pp)))
                    os.remove(pp)
        except: pass
        await db.save()
        await event.reply("✅ **Profile Cloned!**")
    except Exception as e:
        await event.reply(f"❌ Clone failed: {str(e)[:100]}")


@cmd('reclone')
async def reclone_cmd(event):
    if not is_owner(event.sender_id): return
    cd = db.clone_data
    if not cd.get('orig_first_name'):
        return await event.reply("❌ No saved data. Clone first.")
    try:
        await _client(UpdateProfileRequest(
            first_name=cd.get('orig_first_name', ''),
            last_name=cd.get('orig_last_name', '')
        ))
        if cd.get('orig_bio'):
            await _client(functions.account.UpdateProfileRequest(about=cd['orig_bio']))
        me  = await _client.get_me()
        cur = await _client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
        if cur.photos: await _client(DeletePhotosRequest(id=cur.photos))
        for p in sorted(cd.get('orig_photo_paths', [])):
            if os.path.exists(p):
                await _client(UploadProfilePhotoRequest(file=await _client.upload_file(p)))
                await asyncio.sleep(0.5)
        db.clone_data = {}
        await db.save()
        await event.reply("✅ **Profile Restored!**")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {str(e)[:100]}")


# ─── GHOST MODE ──────────────────────────────────────────────────

@cmd('ghost')
async def ghost_cmd(event):
    if not is_owner(event.sender_id): return
    if db.ghost_on: return await event.reply("👻 Ghost mode already active!")
    me = await _client.get_me()
    db.ghost_data = {'first_name': me.first_name or '', 'last_name': me.last_name or '', 'bio': '', 'photo_paths': []}
    try:
        fm = await _client(functions.users.GetFullUserRequest(id=me.id))
        db.ghost_data['bio'] = fm.full_user.about or ''
    except: pass
    try:
        shutil.rmtree(GHOST_PHOTOS_DIR, ignore_errors=True); GHOST_PHOTOS_DIR.mkdir(exist_ok=True)
        ap = await _client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
        paths = []
        for i, photo in enumerate(reversed(ap.photos)):
            p = str(GHOST_PHOTOS_DIR / f"ghost_{i:03d}.jpg")
            if await _client.download_media(photo, p): paths.append(p)
        db.ghost_data['photo_paths'] = paths
    except Exception as e:
        log.warning(f"Ghost photo save: {e}")
    try:
        await _client(UpdateProfileRequest(first_name="Deleted", last_name="Account"))
        await _client(functions.account.UpdateProfileRequest(about="This account has been deleted"))
        cur = await _client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
        if cur.photos: await _client(DeletePhotosRequest(id=cur.photos))
        db.ghost_on = True
        await db.save()
        await event.reply("👻 Ghost Mode Activated!")
    except Exception as e:
        await event.reply(f"❌ Ghost failed: {str(e)[:100]}")


@cmd('unghost')
async def unghost_cmd(event):
    if not is_owner(event.sender_id): return
    if not db.ghost_on: return await event.reply("❌ Ghost mode not active.")
    gd = db.ghost_data
    try:
        await _client(UpdateProfileRequest(
            first_name=gd.get('first_name', ''),
            last_name=gd.get('last_name', '')
        ))
        await _client(functions.account.UpdateProfileRequest(about=gd.get('bio', '')))
        paths = gd.get('photo_paths', [])
        if paths:
            me  = await _client.get_me()
            cur = await _client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            if cur.photos: await _client(DeletePhotosRequest(id=cur.photos))
            for p in paths:
                if os.path.exists(p):
                    try:
                        await _client(UploadProfilePhotoRequest(file=await _client.upload_file(p)))
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        log.warning(f"Restore photo: {e}")
        db.ghost_on   = False
        db.ghost_data = {}
        await db.save()
        shutil.rmtree(GHOST_PHOTOS_DIR, ignore_errors=True); GHOST_PHOTOS_DIR.mkdir(exist_ok=True)
        await event.reply("✅ Ghost Mode Deactivated!")
    except Exception as e:
        await event.reply(f"❌ Unghost failed: {str(e)[:100]}")


# ─── ADMIN ───────────────────────────────────────────────────────

@cmd('admins')
async def admins_cmd(event):
    await event.reply(f"👑 Owner: `{db.owner_id}`\n**Admins:** {db.admins}", parse_mode='markdown')


@cmd('addadmin')
async def addadmin_cmd(event):
    if not is_owner(event.sender_id): return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    if uid in db.admins: return await event.reply("⏺️ Already admin.")
    db.admins.append(uid); await db.save()
    await event.reply("✅ **Admin Added!**")


@cmd('deladmin')
async def deladmin_cmd(event):
    if not is_owner(event.sender_id): return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    if uid in db.admins: db.admins.remove(uid)
    await db.save()
    await event.reply("✅ **Admin Removed!**")


# ─── MUTE & RESTRICT ─────────────────────────────────────────────

@cmd('mute', delete_cmd=False)
async def mute_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    try:
        await _client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, send_messages=True)))
        await event.reply("🔇 **Muted!**")
    except Exception as e: await event.reply(f"❌ {str(e)[:100]}")


@cmd('unmute', delete_cmd=False)
async def unmute_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    try:
        await _client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, send_messages=False)))
        await event.reply("🔊 **Unmuted!**")
    except Exception as e: await event.reply(f"❌ {str(e)[:100]}")


@cmd('gmute')
async def gmute_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    db.gmuted[str(uid)] = True; await db.save()
    await event.reply("🌐 **Globally Muted!**")


@cmd('gunmute')
async def gunmute_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    db.gmuted.pop(str(uid), None); await db.save()
    await event.reply("🌐 **Globally Unmuted!**")


def _base_banned():
    return ChatBannedRights(
        until_date=None,
        send_messages=False, send_media=False,
        send_stickers=True,  send_gifs=True,  send_games=False,
        send_inline=False,   send_polls=False, embed_links=True,
        send_photos=False,   send_videos=False, send_audios=False,
        send_voices=False,   send_docs=False,
        pin_messages=True,   change_info=True, invite_users=False,
    )

_LOCK_ATTRS = ('send_messages','send_media','send_photos','send_videos',
               'send_audios','send_voices','send_docs','send_polls','send_games','send_inline')

@cmd('lock')
async def lock_cmd(event):
    if not event.is_group: return await event.reply("❌ Not a group.")
    try:
        r = _base_banned()
        for a in _LOCK_ATTRS: setattr(r, a, True)
        await _client(EditChatDefaultBannedRightsRequest(peer=event.chat_id, banned_rights=r))
        await event.reply("🔒 Group Locked!")
    except Exception as e: await event.reply(f"❌ {str(e)[:100]}")


@cmd('unlock')
async def unlock_cmd(event):
    if not event.is_group: return await event.reply("❌ Not a group.")
    try:
        r = _base_banned()
        for a in _LOCK_ATTRS: setattr(r, a, False)
        await _client(EditChatDefaultBannedRightsRequest(peer=event.chat_id, banned_rights=r))
        await event.reply("🔓 Group Unlocked!")
    except Exception as e: await event.reply(f"❌ {str(e)[:100]}")


# ─── GROUP MOD ───────────────────────────────────────────────────

@cmd('tagall')
async def tagall_cmd(event):
    if not event.is_group: return await event.reply("❌ Not a group.")
    text, users = get_args(event.message), []
    try:
        async for u in _client.iter_participants(event.chat_id):
            if not u.deleted and not u.bot: users.append(u)
    except Exception as e: return await event.reply(f"❌ {str(e)[:100]}")
    if not users: return await event.reply("❌ No members.")
    for i in range(0, len(users), 50):
        chunk    = users[i:i+50]
        mentions = [
            f'<a href="tg://user?id={u.id}">'
            f'{(u.first_name or "User").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}'
            f'</a>' for u in chunk
        ]
        msg = (f"{text}\n" if (i == 0 and text) else "") + ' '.join(mentions)
        await event.respond(msg, parse_mode='html')
        await asyncio.sleep(1.5)


@cmd('raid')
async def raid_cmd(event):
    if not event.is_reply: return await event.reply("❌ Reply to a user.")
    try: count = min(int(get_args(event.message) or 10), 50)
    except: count = 10
    reply = await event.get_reply_message()
    for i in range(count):
        try:
            await reply.reply(random.choice(RAID_MESSAGES).replace('{n}', str(i+1)))
            await asyncio.sleep(0.3)
        except: break


@cmd('purge', delete_cmd=True)
async def purge_cmd(event):
    if not event.is_reply: return await event.reply("❌ Reply to a message.")
    reply_msg = await event.get_reply_message()
    if not reply_msg: return await event.reply("❌ Could not fetch reply.")
    chat_id, all_ids, last_id = event.chat_id, [], reply_msg.id
    while True:
        try:
            msgs = await _client.get_messages(chat_id, min_id=last_id, limit=100, reverse=False)
            if not msgs: break
            ids = [m.id for m in msgs]
            all_ids.extend(ids); last_id = min(ids)
            if len(msgs) < 100: break
        except Exception as e: return await event.reply(f"❌ Fetch error: {str(e)[:100]}")
    if not all_ids: return await event.reply("✅ Nothing to purge.")
    total = 0
    for i in range(0, len(all_ids), 100):
        try:
            await _client.delete_messages(chat_id, all_ids[i:i+100])
            total += len(all_ids[i:i+100]); await asyncio.sleep(0.5)
        except Exception as e: return await event.reply(f"❌ Delete error: {str(e)[:100]}")
    await event.reply(f"✅ Purged **{total}** messages.")


@cmd('throw')
async def throw_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    try:
        await _client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=True)))
        await asyncio.sleep(0.5)
        await _client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=False)))
        await event.reply("👢 **Kicked!**")
    except Exception as e: await event.reply(f"❌ {str(e)[:100]}")


@cmd('stop')
async def stop_cmd(event):
    db.reply_raid.clear(); db.rr_raid.clear()
    db.flag_raid.clear();  db.heart_raid.clear()
    db.spam_running = False; db.fast_gc_data.clear()
    await event.reply("🛑 **All loops stopped!**")


# ─── FAST GC ─────────────────────────────────────────────────────

async def fast_gc_loop(chat_id: str):
    while True:
        # MongoDB returns string keys, so always str(chat_id)
        data = db.fast_gc_data.get(str(chat_id))
        if not data or not data.get('enabled'): break
        counter   = data.get('counter', 0) + 1
        new_title = f"{data['template']} [{counter}]"
        try:
            entity = await _client.get_entity(int(chat_id))
            if hasattr(entity, 'broadcast') and entity.broadcast:
                await _client(EditTitleRequest(channel=entity, title=new_title))
            else:
                await _client(EditChatTitleRequest(chat_id=int(chat_id), title=new_title))
            data['counter'] = counter
            await db.save()
        except Exception as e:
            log.warning(f"FastGC error: {e}")
            if str(chat_id) in db.fast_gc_data:
                db.fast_gc_data[str(chat_id)]['enabled'] = False
            await db.save(); break
        await asyncio.sleep(1.5)


@cmd('fastgc')
async def fastgc_cmd(event):
    args    = get_args(event.message).split()
    chat_id = str(event.chat_id)   # always string — MongoDB safe
    if not args: return await event.reply("Usage: `.fastgc set <text>` | `.fastgc stop`")
    action = args[0].lower()
    if action == 'set' and len(args) > 1:
        template = ' '.join(args[1:])
        db.fast_gc_data[chat_id] = {'enabled': True, 'template': template, 'counter': 0}
        await db.save()
        asyncio.create_task(fast_gc_loop(chat_id))
        await event.reply(f"⚡ **Fast GC started!** → '{template} [X]'")
    elif action == 'stop':
        if chat_id in db.fast_gc_data:
            db.fast_gc_data[chat_id]['enabled'] = False
            await db.save(); await event.reply("🛑 **Fast GC stopped!**")
        else:
            await event.reply("❌ No fast GC running here.")
    else:
        await event.reply("Usage: `.fastgc set <text>` | `.fastgc stop`")


# ─── AUTO SYSTEM ─────────────────────────────────────────────────

@cmd('autoreact')
async def autoreact_cmd(event):
    arg = get_args(event.message).lower()
    if   arg == 'on':  db.auto_react_dm_enabled = True;  await db.save(); await event.reply("✅ **DM Auto‑React ON**")
    elif arg == 'off': db.auto_react_dm_enabled = False; await db.save(); await event.reply("✅ **DM Auto‑React OFF**")
    else: await event.reply("Usage: `.autoreact on/off`")


@cmd('autoreactg')
async def autoreactg_cmd(event):
    arg = get_args(event.message).lower()
    if   arg == 'on':  db.auto_react_group_enabled = True;  await db.save(); await event.reply("✅ **Group Auto‑React ON**")
    elif arg == 'off': db.auto_react_group_enabled = False; await db.save(); await event.reply("✅ **Group Auto‑React OFF**")
    else: await event.reply("Usage: `.autoreactg on/off`")


@cmd('autoreply')
async def autoreply_cmd(event):
    parts = get_args(event.message).split(' ', 1)
    if not parts: return await event.reply("Usage: `.autoreply set <text>` | on | off")
    action = parts[0].lower()
    if action == 'set' and len(parts) > 1:
        db.auto_reply = {'enabled': True, 'text': parts[1]}
        await db.save(); await event.reply(f"✅ Auto reply set:\n{parts[1]}")
    elif action == 'on':
        db.auto_reply['enabled'] = True; await db.save(); await event.reply("✅ **Auto Reply ON**")
    elif action == 'off':
        db.auto_reply['enabled'] = False; await db.save(); await event.reply("✅ **Auto Reply OFF**")
    else: await event.reply("Usage: `.autoreply set <text>` | on | off")


@cmd('pmguard')
async def pmguard_cmd(event):
    arg = get_args(event.message).lower()
    if   arg == 'on':  db.pmguard['enabled'] = True;  await db.save(); await event.reply("🛡️ **PM Guard ON**")
    elif arg == 'off': db.pmguard['enabled'] = False; await db.save(); await event.reply("🛡️ **PM Guard OFF**")
    else: await event.reply("Usage: `.pmguard on/off`")


@cmd('approve')
async def approve_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    if uid in db.approved: return await event.reply("✅ Already approved.")
    db.approved.append(uid); db.pm_warnings.pop(str(uid), None)
    try: await _client(UnblockRequest(id=uid))
    except: pass
    await db.save(); await event.reply("✅ **Approved!**")


@cmd('disapprove')
async def disapprove_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    if uid in db.approved: db.approved.remove(uid)
    await db.save(); await event.reply("✅ **Disapproved!**")


@cmd('blacklist')
async def blacklist_add_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    if uid not in db.blacklist: db.blacklist.append(uid)
    await db.save(); await event.reply("🚫 **Blacklisted!**")


@cmd('removeblacklist')
async def removeblacklist_cmd(event):
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    if uid in db.blacklist: db.blacklist.remove(uid)
    await db.save(); await event.reply("✅ **Removed from Blacklist!**")


# ─── RAID ENGINE ─────────────────────────────────────────────────

@cmd('reply')
async def reply_raid_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    async def _s(): await event.reply(f"@{uid} {random.choice(REPLY_MESSAGES)}")
    asyncio.create_task(raid_loop(event, uid, db.reply_raid, _s, 1.5))
    await event.reply("⚔️ **Reply raid started!**")

@cmd('sreply')
async def sreply_cmd(event):
    db.reply_raid.get(event.chat_id, set()).clear()
    await event.reply("🛑 **Reply raid stopped!**")

@cmd('rr')
async def rr_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    async def _s(): await event.reply(f"🤣 {user_link(uid)}", parse_mode='markdown')
    asyncio.create_task(raid_loop(event, uid, db.rr_raid, _s, 1))
    await event.reply("🤣 **RR raid started!**")

@cmd('srr')
async def srr_cmd(event):
    db.rr_raid.get(event.chat_id, set()).clear()
    await event.reply("🛑 **RR raid stopped!**")

@cmd('flag')
async def flag_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    flags = ['🇮🇳','🇺🇸','🇬🇧','🇩🇪','🇫🇷','🇯🇵','🇨🇳','🇷🇺','🇧🇷','🇦🇪']
    async def _s(): await event.reply(f"{random.choice(flags)} {user_link(uid)}", parse_mode='markdown')
    asyncio.create_task(raid_loop(event, uid, db.flag_raid, _s, 1.2))
    await event.reply("🚩 **Flag raid started!**")

@cmd('sflag')
async def sflag_cmd(event):
    db.flag_raid.get(event.chat_id, set()).clear()
    await event.reply("🛑 **Flag raid stopped!**")

@cmd('hrr')
async def hrr_cmd(event):
    if not event.is_group: return
    uid = await resolve_user(event.message, get_args(event.message))
    if not uid: return await event.reply("❌ Not found.")
    hearts = ['❤️','🧡','💛','💚','💙','💜','🖤','🤍','🤎','💗']
    async def _s(): await event.reply(f"{random.choice(hearts)} {user_link(uid)}", parse_mode='markdown')
    asyncio.create_task(raid_loop(event, uid, db.heart_raid, _s, 1))
    await event.reply("💖 **Heart raid started!**")

@cmd('shrr')
async def shrr_cmd(event):
    db.heart_raid.get(event.chat_id, set()).clear()
    await event.reply("🛑 **Heart raid stopped!**")


# ─── SPAM ────────────────────────────────────────────────────────

@cmd('spray')
async def spray_cmd(event):
    text = get_args(event.message)
    db.spam_running = True
    async def _loop():
        count = 0
        while db.spam_running and count < 100:
            try:
                msg   = text or random.choice(DEFAULT_SPAM_TEXTS)
                emoji = random.choice(SPAM_EMOJIS)
                await event.reply(f"{msg} {emoji}")
                count += 1
                await asyncio.sleep(0.5 if text else 0.8)
            except: break
        db.spam_running = False
    asyncio.create_task(_loop())
    await event.reply("💣 **Spam started!**")

@cmd('dspray')
async def dspray_cmd(event):
    db.spam_running = False
    await event.reply("🛑 **Spam stopped!**")


# ─── FUN ─────────────────────────────────────────────────────────

@cmd('ping', delete_cmd=False)
async def ping_cmd(event):
    s   = time.time()
    msg = await event.reply("🏓 Pong!")
    await msg.edit(f"🏓 **Pong!** `{round((time.time()-s)*1000,2)}ms`", parse_mode='markdown')

@cmd('flip', delete_cmd=False)
async def flip_cmd(event):
    await event.reply(random.choice(["**Heads** 🪙","**Tails** 🪙"]), parse_mode='markdown')

@cmd('dice', delete_cmd=False)
async def dice_cmd(event):
    await event.reply(f"🎲 **Dice:** `{random.randint(1,6)}`", parse_mode='markdown')


# ════════════════════════════════════════════════════════════════
#  RAW EVENT HANDLERS
# ════════════════════════════════════════════════════════════════

@raw(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def pm_guard_handler(event):
    uid = event.sender_id
    if uid == db.owner_id: return
    if not db.pmguard.get('enabled'): return
    if uid in db.approved: return
    if uid in db.blacklist:
        await _client(BlockRequest(id=uid)); return
    w = db.pm_warnings.get(str(uid), 0) + 1
    db.pm_warnings[str(uid)] = w
    await db.save()
    if w < 5:
        await event.reply(
            f"**🔒 SECURITY ALERT**\n\nOwner is busy/offline. Don't spam!\n\n"
            f"⚠️ Warning: {w}/5\n🚫 5th = Auto Block"
        )
    else:
        await event.reply("🚫 **Blocked for spamming!**")
        try:
            await _client(BlockRequest(id=uid))
            if uid not in db.blocklist:
                db.blocklist.append(uid); await db.save()
        except: pass


@raw(events.NewMessage())
async def gmute_filter(event):
    if event.out: return
    if str(event.sender_id) in db.gmuted:
        try:
            await event.delete()
            if event.is_private:
                await _client(BlockRequest(id=event.sender_id))
        except: pass


@raw(events.NewMessage())
async def auto_react_handler(event):
    if event.out: return
    should = (event.is_private and db.auto_react_dm_enabled) or \
             (not event.is_private and db.auto_react_group_enabled)
    if not should: return
    try:
        await _client(SendReactionRequest(
            peer=event.chat_id,
            msg_id=event.message.id,
            reaction=[ReactionEmoji(emoticon=random.choice(db.auto_react_emojis))]
        ))
    except: pass


@raw(events.NewMessage(func=lambda e: e.is_private and not e.out))
async def auto_reply_handler(event):
    if event.sender_id == db.owner_id: return
    if not db.auto_reply.get('enabled'): return
    await asyncio.sleep(0.5)
    await event.reply(db.auto_reply.get('text', 'Owner is offline. Please wait!'))


# ════════════════════════════════════════════════════════════════
#  SETUP — called once from bot.py
# ════════════════════════════════════════════════════════════════

def setup(client):
    global _client
    _client = client

    for pattern, owner_only, delete_cmd, func in _cmd_handlers:
        async def make_handler(event, f=func, ow=owner_only, dc=delete_cmd, p=pattern):
            if ow and not is_owner(event.sender_id): return
            if not ow and not is_owner(event.sender_id) and not is_admin(event.sender_id): return
            try:
                if dc: asyncio.create_task(delete_command_message(event, 2))
                await f(event)
            except FloodWaitError as e:
                log.warning(f"Flood {e.seconds}s")
                await event.reply(f"⏳ Flood wait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                log.error(f".{p} error: {e}")
                await event.reply(f"❌ {str(e)[:200]}")

        client.add_event_handler(
            make_handler,
            events.NewMessage(pattern=rf'^\.{pattern}(\s.*)?$')
        )

    for event_builder, func in _raw_handlers:
        client.add_event_handler(func, event_builder)

    log.info(f"✅ {len(_cmd_handlers)} cmds + {len(_raw_handlers)} raw handlers live")
