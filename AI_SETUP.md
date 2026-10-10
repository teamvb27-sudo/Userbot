# AI Auto-Reply — Setup Guide

This adds a Gemini-primary / Groq-backup AI auto-reply system to `nexus_core.py`.
Nothing in the existing project was removed — ghost mode, mod commands, auto-react,
auto-reply (static text), pmguard, the factory bot, etc. all work exactly as before.

## 1. Environment variables (set in Render → your service → Environment)

| Variable         | Required? | Notes                                                        |
|------------------|-----------|----------------------------------------------------------------|
| `GEMINI_API_KEY` | Yes (primary) | From Google AI Studio. If missing, Gemini calls are skipped and Groq is used directly. |
| `GROQ_API_KEY`   | Yes (backup)  | From console.groq.com. If missing, Groq calls are skipped.   |
| `GEMINI_MODEL`   | No        | Defaults to `gemini-2.0-flash`. Override if you want a different Gemini model. |
| `GROQ_MODEL`     | No        | Defaults to `llama-3.3-70b-versatile`. Override for a different Groq model. |

If **both** keys are missing, `.aion` still works (no crash), but every AI reply attempt
fails silently (logged, not sent) — `.aistatus` will show both providers as "missing" so
you can see why.

These are read once at process start from `os.environ`, same as every other secret in
this project — never hardcoded, never printed, never included in `.aistatus` or any
other command output.

### Factory / multi-clone note
`nexus_factory_bot.py` launches each clone with `subprocess.Popen(...)` **without** an
explicit `env=`, which means every clone subprocess inherits the factory process's full
environment — so `GEMINI_API_KEY` / `GROQ_API_KEY` set once in Render apply to all clones
automatically. No per-clone config changes needed. If you want different keys per clone,
you'd need to extend each clone's `config.json` and read from there instead — not done
here since it wasn't asked for and would be a bigger change to the clone format.

## 2. Commands

| Command | Scope | What it does |
|---|---|---|
| `.aion` | global | Turns the main AI switch ON (enables DM AI; groups still need `.aitg on` individually) |
| `.aioff` | global | Turns the main AI switch OFF everywhere (DMs + all groups) |
| `.aitg on` | current group only | Enables AI replies in *this* group (requires `.aion` also being on) |
| `.aitg off` | current group only | Disables AI replies in *this* group only |
| `.aitg` | current group only | Shows this group's AI status. Returns an error if used in a DM. |
| `.aistatus` | any chat | Shows main switch state, this chat's state, which providers are configured (never shows the keys), personality, memory usage |
| `.aipersonality <adaptive\|friendly\|professional\|funny>` | per-chat | Sets the AI tone **for the current chat**. With no argument, shows the current value. |
| `.aimemory <10-20>` | global | Sets how many recent messages (combined user+AI) are kept per chat. With no argument, shows the current value. |
| `.aiclear` | current chat | Wipes the AI conversation memory for this chat only |

Permissions follow the existing `@cmd(...)` decorator: owner + admins only, same as the
rest of the moderation commands.

## 3. Design decisions (so nothing was silently guessed)

The brief said AI should reply to "eligible" group messages while explicitly worrying
about flooding. Replying to *every* message in an active group would almost certainly
flood the chat and burn through free-tier API quota fast, so this implementation only
replies in groups when the incoming message is:
- a reply to one of the owner's own messages, **or**
- an `@mention` of the owner's username

DMs have no such restriction — every eligible private message gets a reply when the
main switch and that chat aren't blacklisted/muted.

Other concrete behaviors:
- **Delay**: a random 2–5s `asyncio.sleep` happens before each AI call, so replies never
  look instant.
- **Duplicate protection**: a per-chat `asyncio.Lock` + "last handled message id" map
  means the same incoming message is never answered twice, even under rapid-fire input.
- **No reply to bots**: in groups, the sender is checked with `get_sender()` and
  skipped if `sender.bot` is true.
- **Commands never trigger AI**: any message starting with `.` is ignored by the AI
  handler so your existing commands keep working untouched.
- **Unsupported media**: messages with no text (`event.raw_text` empty — stickers,
  photos without captions, etc.) are silently ignored, as asked.
- **Memory isolation**: `db.ai_memory` is keyed by `str(chat_id)`, so each DM/group has
  its own independent history; `.aiclear` only touches the current key.
- **Personality**: `.aipersonality` is **per-chat** (stored in `db.ai_chat_personality`),
  falling back to the global default (`adaptive`) when a chat has no override — this
  matches the "jahan practical ho, alag configure ho sake" requirement.
- **Memory limit**: kept **global** (`db.ai_memory_limit`, 10–20), since the brief only
  asked for one `.aimemory` command, not a per-chat one.
- **Persistence**: everything (`ai_enabled`, `ai_personality`, `ai_memory_limit`,
  `ai_group_enabled`, `ai_chat_personality`, `ai_memory`) is persisted through the
  existing `BotDB.save()` → same `DATA_FILE` JSON used for every other setting, so it
  survives restarts exactly like `pmguard`, `blacklist`, etc. already do.
- **Failure handling**: Gemini is tried first (up to 2 quick attempts), then Groq (up to
  2 attempts); if a key is simply missing, that provider is skipped immediately rather
  than retried. If both fail, the handler logs the error and returns — it never raises,
  so it can't crash the userbot process or the whole clone.

## 4. Testing performed

Done without live Telegram/API credentials (none were provided), so this is static/
offline verification only:

- ✅ `python3 -m py_compile nexus_core.py` — passes
- ✅ `python3 -m py_compile nexus_factory_bot.py` — passes (unchanged, confirmed untouched)
- ✅ Checked for duplicate `@cmd(...)` names across the whole file — none found
- ✅ Checked the regex each command compiles to (`^\.{name}(\s.*)?$`) — confirmed `.aitg`,
  `.aion`, `.aioff`, `.aistatus`, `.aipersonality`, `.aimemory`, `.aiclear` are all
  distinct, non-overlapping patterns
- ✅ Walked through the logic by hand for: DM AI on/off, group `.aitg on/off`, group
  toggle not affecting DM or other groups, `.aioff` disabling both DM and group AI,
  `.aion` only re-enabling groups that already have `.aitg on`, memory isolation per
  chat, memory trimming at the configured limit, missing-key behavior for each provider,
  Gemini→Groq fallback order

**Not tested (requires a real account + real API keys, neither of which were
available here):**
- ❌ An actual live Telegram login/session
- ❌ A real Gemini or Groq API call succeeding end-to-end
- ❌ Real Telegram `FloodWaitError` behavior under load
- ❌ Actual multi-clone concurrency on Render

Before going live: set both env vars, run `.aion`, DM the account from a second
account, and watch `nexus.log` for `[AI]`-prefixed lines if something doesn't reply.

## 5. Deployment / start instructions (unchanged)

Nothing about how you run or deploy this changed:
```
python nexus_core.py --config /path/to/config.json
```
or, for first-time login only:
```
python nexus_core.py --config /path/to/config.json --login-only
```
The factory bot (`nexus_factory_bot.py`) is started the same way it always was; it
launches clones exactly as before, just now with `GEMINI_API_KEY`/`GROQ_API_KEY`
available to them via inherited environment variables (see §1).
