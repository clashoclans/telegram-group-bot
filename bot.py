import html
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent

def load_dotenv_file(path: Path) -> None:
    """Load simple KEY=VALUE entries from .env without extra dependencies.

    Existing environment variables take precedence over values in .env.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        log.exception("Could not read .env file")


load_dotenv_file(BASE_DIR / ".env")


def configured_path(variable: str, default_name: str) -> Path:
    """Resolve configured data paths relative to this project unless absolute."""
    raw = os.getenv(variable, default_name).strip().strip('"').strip("'")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def parse_admin_ids(raw: str) -> set[int]:
    """Parse comma-separated Telegram user IDs, tolerating accidental quotes."""
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip().strip('"').strip("'")
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError:
            log.warning("Ignoring invalid ADMIN_IDS entry: %r (expected a numeric Telegram user ID)", item)
            continue
        if user_id > 0:
            result.add(user_id)
        else:
            log.warning("Ignoring non-positive ADMIN_IDS entry: %r", item)
    return result


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@jainsahiba18").strip().strip('"').strip("'")
DATA_FILE = configured_path("DATA_FILE", "groups.json")
REQUESTS_FILE = configured_path("REQUESTS_FILE", "requests.json")
EVENTS_FILE = configured_path("EVENTS_FILE", "events.json")
SERVICE_MESSAGES_FILE = configured_path("SERVICE_MESSAGES_FILE", "service_messages.json")

PAYMENT_MESSAGE = (
    "Hello! 👋\n\n"
    "You have requested to join {group}. This is a paid group, and payment is required before access can be granted.\n\n"
    "Your request is currently pending. Please contact admin {admin} for payment instructions. "
    "Once your payment is verified, the admin can approve your request.\n\n"
    "If you have already paid, please share your payment reference with {admin}. Thank you! 🙏"
)

# Pagination size for pending-requests listing
PENDING_PAGE_SIZE = 5


# ---------------------------------------------------------------------------
#  Safe JSON persistence helpers (Permanent storage — never deletes bot logs)
# ---------------------------------------------------------------------------

def _safe_write_json(path: Path, data: Any) -> None:
    """Atomically write *data* as JSON to *path*."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_groups() -> dict[str, dict[str, Any]]:
    if not DATA_FILE.exists():
        return {}
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read %s", DATA_FILE)
        return {}


def save_groups(groups: dict[str, dict[str, Any]]) -> None:
    _safe_write_json(DATA_FILE, groups)


def load_requests() -> dict[str, dict[str, Any]]:
    if not REQUESTS_FILE.exists():
        return {}
    try:
        data = json.loads(REQUESTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read %s", REQUESTS_FILE)
        return {}


def save_requests(requests: dict[str, dict[str, Any]]) -> None:
    _safe_write_json(REQUESTS_FILE, requests)


def load_events() -> list[dict[str, Any]]:
    if not EVENTS_FILE.exists():
        return []
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read %s", EVENTS_FILE)
        return []


def save_events(events: list[dict[str, Any]]) -> None:
    _safe_write_json(EVENTS_FILE, events)


def load_service_messages() -> dict[str, list[int]]:
    """Load tracked in-chat entry/exit service message IDs per group."""
    if not SERVICE_MESSAGES_FILE.exists():
        return {}
    try:
        data = json.loads(SERVICE_MESSAGES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read %s", SERVICE_MESSAGES_FILE)
        return {}


def save_service_messages(service_messages: dict[str, list[int]]) -> None:
    _safe_write_json(SERVICE_MESSAGES_FILE, service_messages)


def track_service_message(chat_id: int, message_id: int) -> None:
    """Track an in-chat service message ID so the admin can delete it on demand."""
    data = load_service_messages()
    key = str(chat_id)
    if key not in data:
        data[key] = []
    if message_id not in data[key]:
        data[key].append(message_id)
    # Keep up to 500 recent messages per group
    data[key] = data[key][-500:]
    try:
        save_service_messages(data)
    except OSError as exc:
        log.warning("Could not track service message: %s", exc)


def record_event(chat_id: int, group_title: str, user_id: int, full_name: str,
                 username: str | None, event_type: str) -> bool:
    """Record a membership event in events.json permanently, avoiding immediate duplicates."""
    events = load_events()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat(timespec="seconds")

    for existing in reversed(events[-20:]):
        if (int(existing.get("chat_id", 0)) == chat_id
                and int(existing.get("user_id", 0)) == user_id
                and existing.get("event") == event_type):
            ts_str = existing.get("timestamp", "")
            if ts_str:
                try:
                    prev_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if (now_dt - prev_dt).total_seconds() < 30:
                        return False
                except Exception:
                    return False

    events.append({
        "chat_id": chat_id,
        "group_title": group_title,
        "user_id": user_id,
        "full_name": full_name,
        "username": username,
        "event": event_type,
        "timestamp": now_iso,
    })
    try:
        save_events(events)
        return True
    except OSError as exc:
        log.warning("Could not save event: %s", exc)
        return False


def is_member_status(member: Any) -> bool:
    status = getattr(member, "status", "")
    if status in ("creator", "administrator", "member"):
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


def is_admin(update: Update) -> bool:
    user = update.effective_user
    allowed = bool(user and user.id in ADMIN_IDS)
    if user and not allowed:
        log.warning(
            "Access denied for Telegram user ID %s. Configured ADMIN_IDS: %s",
            user.id,
            sorted(ADMIN_IDS),
        )
    return allowed


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("You are not authorised to use this bot.")


# ---------------------------------------------------------------------------
#  Sort helper – groups with a leading number sort ascending by that number
# ---------------------------------------------------------------------------

_LEADING_NUMBER_RE = re.compile(r"^\s*(\d+)\s*[.\-)\s]")


def _group_sort_key(title: str) -> tuple[int, int, str]:
    """Return a sort key that orders groups by their leading number (ascending).

    Titles like "3. English premium" → (0, 3, "3. english premium")
    Titles without a number         → (1, 0, "some title")
    """
    m = _LEADING_NUMBER_RE.match(title)
    if m:
        return (0, int(m.group(1)), title.lower())
    return (1, 0, title.lower())


# ---------------------------------------------------------------------------
#  Menus
# ---------------------------------------------------------------------------

def main_menu() -> InlineKeyboardMarkup:
    # Count all pending requests for the button label
    requests = load_requests()
    pending_total = sum(
        1 for r in requests.values()
        if r.get("status") in ("requested", "payment_pending", "payment_message_sent")
    )
    pending_label = f"📩 All pending requests ({pending_total})" if pending_total else "📩 All pending requests"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add current group", callback_data="help_add")],
        [InlineKeyboardButton("🔗 Get invite link", callback_data="list_links")],
        [InlineKeyboardButton("📋 Manage groups", callback_data="list_groups")],
        [InlineKeyboardButton(pending_label, callback_data="all_pending:0")],
        [InlineKeyboardButton("📊 Membership logs", callback_data="history_groups")],
        [InlineKeyboardButton("📦 Backup data files", callback_data="backup")],
        [InlineKeyboardButton("📖 Help & Commands", callback_data="help_commands")],
    ])


# ---------------------------------------------------------------------------
#  /start and /help
# ---------------------------------------------------------------------------

def get_help_text() -> str:
    """Return a comprehensive help message explaining all available commands and uses."""
    admin_display = html.escape(ADMIN_CONTACT)
    return (
        "📖 <b>Telegram Group Manager — Help & Available Commands</b>\n\n"
        f"👤 <b>Configured Admin Contact:</b> {admin_display}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>ALL AVAILABLE COMMANDS & THEIR USES:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1. /start</b>\n"
        "• <i>Usage:</i> <code>/start</code> in private chat.\n"
        "• <i>Use:</i> Opens the main administrator dashboard with buttons to manage groups, get invite links, view pending requests, and check logs.\n"
        "• <i>Deep-link:</i> <code>/start pay_&lt;token&gt;</code> — Used by applicants to view payment instructions when marked Payment Pending.\n\n"
        "<b>2. /help</b>\n"
        "• <i>Usage:</i> <code>/help</code>\n"
        "• <i>Use:</i> Displays this complete help guide with all commands, syntax, and features.\n\n"
        "<b>3. /addgroup</b>\n"
        "• <i>Usage:</i> <code>/addgroup</code> (run inside a Telegram group).\n"
        "• <i>Use:</i> Registers the group with the bot. Ensure the bot is already an administrator in the group with permissions: Invite Users via Link, Manage Join Requests, and Delete Messages.\n\n"
        "<b>4. /groups</b>\n"
        "• <i>Usage:</i> <code>/groups</code> in private chat.\n"
        "• <i>Use:</i> Displays all registered groups and opens group settings.\n\n"
        "<b>5. /links</b>\n"
        "• <i>Usage:</i> <code>/links</code> in private chat.\n"
        "• <i>Use:</i> Displays persistent invite links for all registered groups in ascending order, with export and single-group options.\n\n"
        "<b>6. /logs</b>\n"
        "• <i>Usage:</i> <code>/logs</code> in private chat.\n"
        "• <i>Use:</i> Displays registered groups to view permanent entry/exit membership logs.\n\n"
        "<b>7. /addrequest &lt;chat_id&gt; &lt;user_id&gt; [Full Name]</b>\n"
        "• <i>Usage in private:</i> <code>/addrequest -1001234567890 987654321 John Doe</code>\n"
        "• <i>Usage in group:</i> <code>/addrequest 987654321 John Doe</code>\n"
        "• <i>Use:</i> Manually imports a pending join request that arrived before the bot joined the group. Once added, it appears in 'View Pending Requests' where you can Accept, Reject, or mark Payment Pending.\n\n"
        "<b>8. /addlog &lt;chat_id&gt; &lt;user_id&gt; &lt;join|leave&gt; [Full Name]</b>\n"
        "• <i>Usage in private:</i> <code>/addlog -1001234567890 987654321 join Alice</code>\n"
        "• <i>Usage in group:</i> <code>/addlog 987654321 join Alice</code>\n"
        "• <i>Use:</i> Manually backfills a historical join or leave event into the bot's permanent event history (events.json).\n\n"
        "<b>9. /del</b>\n"
        "• <i>Usage:</i> Reply to any message in a group with <code>/del</code>.\n"
        "• <i>Use:</i> Immediately deletes the replied-to message and the /del command from the group chat.\n\n"
        "<b>10. /delmessages</b>\n"
        "• <i>Usage:</i> <code>/delmessages</code> (run inside a group chat).\n"
        "• <i>Use:</i> Deletes all pending in-chat join/leave service messages ('User joined', 'User was accepted', 'User left') from that group.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔘 <b>ADMIN MENU BUTTONS & FEATURES:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "• <b>➕ Add current group:</b> Shows instructions on how to add and register a new group.\n"
        "• <b>🔗 Get invite link:</b> View persistent join-request invite links for all groups at once or pick an individual group.\n"
        "• <b>📋 Manage groups:</b> Select any registered group to view settings, inspect pending request counts, clean in-chat messages, view pending requests, or unregister the group.\n"
        "• <b>📩 All pending requests:</b> Global view of all pending requests across every group with Accept (✅), Reject (❌), and Payment Pending (💳) buttons and pagination.\n"
        "• <b>📊 Membership logs:</b> View permanent join/leave logs, inspect the last 10 unique joiners, or clean in-chat entry/exit messages from the group.\n"
        "• <b>📖 Help & Commands:</b> Open this interactive help guide.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>IMPORTANT NOTES:</b>\n"
        "• <b>Permanent bot storage:</b> Stored membership logs in events.json are permanent and are never deleted.\n"
        "• <b>In-chat messages:</b> Join and leave service messages appear in the group as normal. You can delete them whenever you want via the button on the private notification, via Group Settings, or by typing /delmessages in the group."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Applicants use /start pay_<token> to receive the payment-pending message.
    if context.args and context.args[0].startswith("pay_"):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
            return
        token = context.args[0][4:]
        requests = load_requests()
        record = requests.get(token)
        if not record or int(record.get("user_id", 0)) != update.effective_user.id:
            await update.effective_message.reply_text(
                f"This payment link is invalid or belongs to another Telegram account. "
                f"Please contact admin {ADMIN_CONTACT}."
            )
            return
        if record.get("status") not in ("payment_pending", "requested", "payment_message_sent"):
            await update.effective_message.reply_text(
                f"This join request is no longer pending. Please contact admin {ADMIN_CONTACT} if you need help."
            )
            return
        record["status"] = "payment_message_sent"
        save_requests(requests)
        await update.effective_message.reply_text(
            PAYMENT_MESSAGE.format(
                group=record.get("group_title", "the group"),
                admin=ADMIN_CONTACT,
            )
        )
        return

    if not is_admin(update):
        await deny(update)
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "For privacy, open this bot in a private chat and use /start there. "
            "To register this group, run /addgroup in this group as an authorised admin."
        )
        return
    await update.effective_message.reply_text(
        "Group Manager (admin only)\n\n"
        "• Add the bot to each group as an administrator.\n"
        "• Give it permission to invite users, manage join requests, and delete messages.\n"
        "• Run /addgroup in each group to register it.\n"
        "• Return here and choose Get invite link.",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show usage instructions and all available commands. Accessible to admins only."""
    if not is_admin(update):
        await deny(update)
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]) if (
        update.effective_chat and update.effective_chat.type == ChatType.PRIVATE
    ) else None
    await update.effective_message.reply_text(
        get_help_text(),
        reply_markup=kb,
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
#  /addgroup
# ---------------------------------------------------------------------------

async def addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await deny(update)
        return
    chat = update.effective_chat
    message = update.effective_message
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Run /addgroup inside the group you want to register.")
        return

    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        status = getattr(member, "status", "")
        if status not in ("administrator", "creator"):
            await message.reply_text("Please make the bot an administrator in this group first.")
            return
        groups = load_groups()
        key = str(chat.id)
        already = key in groups
        groups[key] = {
            "chat_id": chat.id,
            "title": chat.title or str(chat.id),
        }
        save_groups(groups)
        if already:
            await message.reply_text(
                f"Updated registration: {chat.title or chat.id}\n"
                "The group was already registered. Title refreshed."
            )
        else:
            await message.reply_text(
                f"Registered: {chat.title or chat.id}\n"
                "You can now request its invite link in the bot's private chat."
            )
    except (TelegramError, OSError) as exc:
        log.warning("Group registration failed: %s", exc)
        await message.reply_text(
            "Could not register this group. Check the bot's admin permissions and storage."
        )


# ---------------------------------------------------------------------------
#  /addrequest (Manual import for requests submitted before bot joined)
# ---------------------------------------------------------------------------

async def addrequest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually register a pending join request that arrived before the bot was added."""
    if not is_admin(update):
        await deny(update)
        return

    chat = update.effective_chat
    args = context.args or []

    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not args:
            await update.effective_message.reply_text(
                "Usage inside group: /addrequest <user_id> [Full Name]\n"
                "Example: /addrequest 123456789 John Doe"
            )
            return
        chat_id = chat.id
        try:
            user_id = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("Invalid user ID. It must be a numeric ID.")
            return
        full_name = " ".join(args[1:]) if len(args) > 1 else f"User {user_id}"
    else:
        if len(args) < 2:
            await update.effective_message.reply_text(
                "Usage in private chat: /addrequest <chat_id> <user_id> [Full Name]\n"
                "Example: /addrequest -1001234567890 987654321 Alice Smith"
            )
            return
        try:
            chat_id = int(args[0])
            user_id = int(args[1])
        except ValueError:
            await update.effective_message.reply_text("Invalid chat ID or user ID. Both must be numeric.")
            return
        full_name = " ".join(args[2:]) if len(args) > 2 else f"User {user_id}"

    groups = load_groups()
    chat_key = str(chat_id)
    group = groups.get(chat_key)
    if not group:
        await update.effective_message.reply_text(
            f"Group {chat_id} is not registered yet. Please register it with /addgroup first."
        )
        return

    requests = load_requests()
    existing_token = next((
        tok for tok, rec in requests.items()
        if int(rec.get("chat_id", 0)) == chat_id
        and int(rec.get("user_id", 0)) == user_id
        and rec.get("status") in ("requested", "payment_pending", "payment_message_sent")
    ), None)

    if existing_token:
        await update.effective_message.reply_text(
            f"A pending request for user {user_id} ({full_name}) in {group.get('title', chat_key)} "
            "already exists in the bot."
        )
        return

    token = secrets.token_urlsafe(9)
    requests[token] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "full_name": full_name,
        "username": None,
        "group_title": group.get("title", str(chat_id)),
        "status": "requested",
        "request_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "manual_import",
    }
    save_requests(requests)

    await update.effective_message.reply_text(
        f"✅ Added pending join request for {full_name} (ID: {user_id}) in {group.get('title', chat_key)}!\n\n"
        "It is now visible in 'View Pending Requests' for that group, where you can "
        "Accept, Reject, or mark Payment Pending directly."
    )


# ---------------------------------------------------------------------------
#  /addlog (Manual backfill for historical entry/exit events)
# ---------------------------------------------------------------------------

async def addlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually add an entry/exit event to backfill historical logs."""
    if not is_admin(update):
        await deny(update)
        return

    chat = update.effective_chat
    args = context.args or []

    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if len(args) < 2:
            await update.effective_message.reply_text(
                "Usage: /addlog <user_id> <join|leave> [Full Name]\n"
                "Example: /addlog 123456789 join John Doe"
            )
            return
        chat_id = chat.id
        try:
            user_id = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("Invalid user ID. It must be numeric.")
            return
        event_type = args[1].lower()
        full_name = " ".join(args[2:]) if len(args) > 2 else f"User {user_id}"
    else:
        if len(args) < 3:
            await update.effective_message.reply_text(
                "Usage: /addlog <chat_id> <user_id> <join|leave> [Full Name]\n"
                "Example: /addlog -1001234567890 987654321 join Alice"
            )
            return
        try:
            chat_id = int(args[0])
            user_id = int(args[1])
        except ValueError:
            await update.effective_message.reply_text("Invalid chat ID or user ID. Both must be numeric.")
            return
        event_type = args[2].lower()
        full_name = " ".join(args[3:]) if len(args) > 3 else f"User {user_id}"

    if event_type not in ("join", "leave"):
        await update.effective_message.reply_text("Event must be either 'join' or 'leave'.")
        return

    groups = load_groups()
    group = groups.get(str(chat_id), {})
    title = group.get("title", str(chat_id))

    record = {
        "chat_id": chat_id,
        "group_title": title,
        "user_id": user_id,
        "full_name": full_name,
        "username": None,
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "manual_import",
    }
    events = load_events()
    events.append(record)
    save_events(events)

    await update.effective_message.reply_text(
        f"✅ Recorded {event_type.upper()} event for {full_name} (ID: {user_id}) in {title}."
    )


# ---------------------------------------------------------------------------
#  In-chat service messages (User joined / User left notifications in chat)
# ---------------------------------------------------------------------------

async def on_service_membership_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle in-chat entry and exit service messages (join/leave/accepted).

    The message is NOT deleted automatically on arrival! It is displayed
    in the group chat as normal so everyone sees it when someone joins.
    The bot records the message_id and notifies the admin privately with a
    'Delete join message from chat' button, so the admin can delete it on demand.
    The bot also permanently records the event in events.json.
    """
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    chat_key = str(chat.id)
    groups = load_groups()
    group = groups.get(chat_key)
    group_title = group.get("title", chat.title or chat_key) if group else (chat.title or chat_key)

    # Track message_id for on-demand deletion
    track_service_message(chat.id, message.message_id)

    bot_id = (await context.bot.get_me()).id

    if message.new_chat_members:
        for user in message.new_chat_members:
            if user.is_bot and user.id == bot_id:
                continue
            full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or f"User {user.id}"
            if group:
                record_event(chat.id, group_title, user.id, full_name, user.username, "join")

            # Notify admins privately with working delete button
            del_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete join message from chat", callback_data=f"delchatmsg:{chat.id}:{message.message_id}")]
            ])
            user_display = f"@{user.username}" if user.username else "none"
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🟢 <b>Member Joined</b>\n\n"
                            f"Group: {group_title}\n"
                            f"User: {full_name} (ID: <code>{user.id}</code>)\n"
                            f"Username: {user_display}\n\n"
                            "Message is currently visible in group. Press below to delete it from the chat:"
                        ),
                        reply_markup=del_kb,
                        parse_mode="HTML",
                    )
                except TelegramError as exc:
                    log.warning("Could not notify admin %s of join: %s", admin_id, exc)

    elif message.left_chat_member:
        user = message.left_chat_member
        if not (user.is_bot and user.id == bot_id):
            full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or f"User {user.id}"
            if group:
                record_event(chat.id, group_title, user.id, full_name, user.username, "leave")

            del_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete leave message from chat", callback_data=f"delchatmsg:{chat.id}:{message.message_id}")]
            ])
            user_display = f"@{user.username}" if user.username else "none"
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🔴 <b>Member Left</b>\n\n"
                            f"Group: {group_title}\n"
                            f"User: {full_name} (ID: <code>{user.id}</code>)\n"
                            f"Username: {user_display}\n\n"
                            "Message is currently visible in group. Press below to delete it from the chat:"
                        ),
                        reply_markup=del_kb,
                        parse_mode="HTML",
                    )
                except TelegramError as exc:
                    log.warning("Could not notify admin %s of leave: %s", admin_id, exc)


async def delete_single_chat_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Delete a single in-chat service message when admin clicks the button in private chat."""
    query = update.callback_query
    if not query:
        return
    parts = data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid request.", show_alert=True)
        return
    try:
        chat_id = int(parts[1])
        msg_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid IDs.", show_alert=True)
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        await query.answer("Deleted message from group chat.")
    except TelegramError as exc:
        log.warning("Could not delete message %s in chat %s: %s", msg_id, chat_id, exc)
        await query.answer("Message already deleted or cannot be deleted.", show_alert=True)

    # Remove from service_messages.json
    service_msgs = load_service_messages()
    chat_key = str(chat_id)
    if chat_key in service_msgs and msg_id in service_msgs[chat_key]:
        service_msgs[chat_key] = [m for m in service_msgs[chat_key] if m != msg_id]
        try:
            save_service_messages(service_msgs)
        except OSError:
            pass

    # Update the admin notification message
    old_text = query.message.text or ""
    try:
        await query.edit_message_text(f"{old_text}\n\n🗑 Deleted from group chat.")
    except TelegramError:
        pass


async def clear_group_chat_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_key: str) -> None:
    """Delete all recorded in-chat entry/exit service messages for a group."""
    query = update.callback_query
    if not query:
        return
    service_msgs = load_service_messages()
    msg_ids = service_msgs.get(str(chat_key), [])
    if not msg_ids:
        await query.answer("No entry/exit messages pending deletion for this group.", show_alert=True)
        return

    chat_id = int(chat_key)
    deleted_count = 0
    failed_count = 0
    for msg_id in list(msg_ids):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted_count += 1
        except TelegramError as exc:
            failed_count += 1
            log.warning("Could not delete message %s in chat %s: %s", msg_id, chat_id, exc)

    service_msgs[str(chat_key)] = []
    try:
        save_service_messages(service_msgs)
    except OSError:
        pass

    msg_text = f"Deleted {deleted_count} message(s) from group chat."
    if failed_count:
        msg_text += f" ({failed_count} already removed or expired)"
    await query.answer(msg_text, show_alert=True)

    # Refresh current menu
    if query.message and query.message.text and "Group settings" in query.message.text:
        await show_group_detail(update, context, chat_key)
    elif query.message and query.message.text and "Recent entry/exit logs" in query.message.text:
        await show_recent_events(update, chat_key)
    else:
        await show_group_history_menu(update, chat_key)


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a specific message in the group by replying to it with /del."""
    if not is_admin(update):
        await deny(update)
        return
    message = update.effective_message
    if not message or not message.reply_to_message:
        await message.reply_text("Reply to any message with /del to delete it.")
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except TelegramError as exc:
        log.warning("Could not delete message via /del: %s", exc)


async def delmessages_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete all recorded entry/exit messages in the group where command is run."""
    if not is_admin(update):
        await deny(update)
        return
    chat = update.effective_chat
    message = update.effective_message
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if message:
            await message.reply_text("Run /delmessages inside a group to delete its entry/exit service messages.")
        return

    chat_key = str(chat.id)
    service_msgs = load_service_messages()
    msg_ids = service_msgs.get(chat_key, [])
    if not msg_ids:
        if message:
            await message.reply_text("No entry/exit service messages pending deletion for this group.")
        return

    deleted_count = 0
    for msg_id in list(msg_ids):
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=msg_id)
            deleted_count += 1
        except TelegramError:
            pass

    service_msgs[chat_key] = []
    try:
        save_service_messages(service_msgs)
    except OSError:
        pass

    try:
        if message:
            await message.reply_text(
                f"🗑 Deleted {deleted_count} entry/exit service message(s) from this group."
            )
            await message.delete()
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
#  Group listing (links mode / manage mode)
# ---------------------------------------------------------------------------

async def show_groups(update: Update, context: ContextTypes.DEFAULT_TYPE, links: bool) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if links:
        await show_all_links(update, context, page=0)
        return
    groups = load_groups()
    if not groups:
        await query.edit_message_text(
            "No groups registered yet. Add the bot as admin to a group, then run /addgroup there.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
        )
        return
    rows = []
    for key, item in sorted(groups.items(), key=lambda pair: _group_sort_key(pair[1].get("title", ""))):
        title = item.get("title", key)
        rows.append([InlineKeyboardButton(title[:60], callback_data=f"detail:{key}")])
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
    await query.edit_message_text(
        "Registered groups:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show registered groups via /groups command."""
    if not is_admin(update):
        await deny(update)
        return
    groups = load_groups()
    if not groups:
        await update.effective_message.reply_text(
            "No groups registered yet. Add the bot as admin to a group, then run /addgroup there.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
        )
        return
    rows = []
    for key, item in sorted(groups.items(), key=lambda pair: _group_sort_key(pair[1].get("title", ""))):
        title = item.get("title", key)
        rows.append([InlineKeyboardButton(title[:60], callback_data=f"detail:{key}")])
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
    await update.effective_message.reply_text(
        "Registered groups:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show invite links for all registered groups via /links command."""
    if not is_admin(update):
        await deny(update)
        return
    await show_all_links(update, context, page=0)


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show membership history menu via /logs command."""
    if not is_admin(update):
        await deny(update)
        return
    groups = load_groups()
    if not groups:
        await update.effective_message.reply_text(
            "No groups registered yet. Add the bot as admin and run /addgroup in each group.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
        )
        return
    rows = [
        [InlineKeyboardButton(item.get("title", key)[:60], callback_data=f"history:{key}")]
        for key, item in sorted(groups.items(), key=lambda pair: _group_sort_key(pair[1].get("title", "")))
    ]
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
    await update.effective_message.reply_text(
        "Choose a group to view membership history:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------------------------------------------------------------------------
#  Invite links handlers (All links & Single group)
# ---------------------------------------------------------------------------

def chunk_group_lines(entries: list[str], max_chars: int = 3800) -> list[list[str]]:
    """Split formatted group link entries into chunks that safely fit within 3,800 characters."""
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_len = 0
    header_overhead = 150  # Reserve safe buffer for header, pagination, and buttons

    for entry in entries:
        entry_len = len(entry) + 2  # +2 for \n\n separator
        if current_chunk and (current_len + entry_len + header_overhead > max_chars):
            chunks.append(current_chunk)
            current_chunk = [entry]
            current_len = entry_len
        else:
            current_chunk.append(entry)
            current_len += entry_len

    if current_chunk:
        chunks.append(current_chunk)

    return chunks or [[]]


async def _get_all_group_entries(context: ContextTypes.DEFAULT_TYPE,
                                 groups: dict[str, dict[str, Any]]) -> list[str]:
    """Return formatted 'title\\nlink' entries, creating persistent links once if missing."""
    entries = []
    updated = False
    for key, item in sorted(groups.items(), key=lambda pair: _group_sort_key(pair[1].get("title", ""))):
        title = item.get("title", key)
        link = item.get("last_invite_link")
        if not link:
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=int(item["chat_id"]),
                    creates_join_request=True,
                    name="Admin bot join requests",
                )
                link = invite.invite_link
                item["last_invite_link"] = link
                updated = True
            except (TelegramError, KeyError, ValueError) as exc:
                log.warning("Invite link creation failed for %s: %s", key, exc)
                link = None

        if link:
            entries.append(f"{title}\n{link}")
        else:
            entries.append(f"{title}\n⚠️ Failed to generate link")

    if updated:
        save_groups(groups)

    return entries


async def show_all_links(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    """Display persistent invite links for ALL registered groups, chunked safely under 3,800 chars."""
    query = update.callback_query
    if query:
        await query.answer()

    groups = load_groups()
    if not groups:
        msg = "No groups registered yet. Add the bot as admin to a group, then run /addgroup there."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]])
        if query:
            await query.edit_message_text(msg, reply_markup=kb)
        elif update.effective_message:
            await update.effective_message.reply_text(msg, reply_markup=kb)
        return

    entries = await _get_all_group_entries(context, groups)
    chunks = chunk_group_lines(entries, max_chars=3800)
    total_pages = len(chunks)
    page = max(0, min(page, total_pages - 1))
    chunk_items = chunks[page]
    body = "\n\n".join(chunk_items)

    if total_pages == 1:
        header = f"🔗 Invite Links (All Groups — {len(entries)} total)\n\n"
    else:
        start_idx = sum(len(c) for c in chunks[:page]) + 1
        end_idx = start_idx + len(chunk_items) - 1
        header = f"🔗 Invite Links (Page {page + 1}/{total_pages} — Groups {start_idx}–{end_idx} of {len(entries)})\n\n"

    buttons = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"all_links:{page - 1}"))
        nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"all_links:{page}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"all_links:{page + 1}"))
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("📌 Single group", callback_data="link_pick"),
        InlineKeyboardButton("📤 Export all to chat", callback_data="export_links"),
    ])
    buttons.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])

    text_to_show = f"{header}{body}"
    markup = InlineKeyboardMarkup(buttons)

    if query:
        try:
            await query.edit_message_text(text_to_show, reply_markup=markup, disable_web_page_preview=True)
        except TelegramError:
            await query.message.reply_text(text_to_show, reply_markup=markup, disable_web_page_preview=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text_to_show, reply_markup=markup, disable_web_page_preview=True)


async def export_all_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send all group links directly as messages into the chat for easy copying."""
    query = update.callback_query
    if query:
        await query.answer("Sending all links to chat… 📨")

    groups = load_groups()
    if not groups:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    entries = await _get_all_group_entries(context, groups)
    chunks = chunk_group_lines(entries, max_chars=3800)
    total_parts = len(chunks)

    for idx, chunk in enumerate(chunks, 1):
        if total_parts > 1:
            header = f"🔗 Invite Links (Part {idx}/{total_parts})\n\n"
        else:
            header = "🔗 Invite Links (All Groups)\n\n"
        body = "\n\n".join(chunk)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{header}{body}",
            disable_web_page_preview=True,
        )


async def show_link_pick(update: Update) -> None:
    """Show sorted group list to pick one for its individual invite link."""
    query = update.callback_query
    await query.answer()
    groups = load_groups()
    if not groups:
        await query.edit_message_text(
            "No groups registered yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
        )
        return
    rows = []
    for key, item in sorted(groups.items(), key=lambda pair: _group_sort_key(pair[1].get("title", ""))):
        title = item.get("title", key)
        rows.append([InlineKeyboardButton(title[:60], callback_data=f"invite:{key}")])
    rows.append([
        InlineKeyboardButton("🔗 All group links", callback_data="list_links"),
        InlineKeyboardButton("⬅️ Menu", callback_data="menu"),
    ])
    await query.edit_message_text("Choose a group to view its invite link:", reply_markup=InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------------------
#  Group settings / detail menu
# ---------------------------------------------------------------------------

async def show_group_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_key: str) -> None:
    """Show the settings menu for one registered group."""
    query = update.callback_query
    await query.answer()
    groups = load_groups()
    item = groups.get(chat_key)
    if not item:
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="list_groups")]]),
        )
        return

    requests = load_requests()
    pending_count = sum(
        1 for r in requests.values()
        if str(r.get("chat_id")) == chat_key and r.get("status") in ("requested", "payment_pending", "payment_message_sent")
    )
    pending_label = f"📩 View pending requests ({pending_count})" if pending_count else "📩 View pending requests"

    service_msgs = load_service_messages()
    pending_msgs = len(service_msgs.get(chat_key, []))
    clean_label = f"🗑 Clean in-chat messages ({pending_msgs})" if pending_msgs else "🗑 Clean in-chat messages"

    await query.edit_message_text(
        f"⚙️ Group settings\n\n"
        f"Group: {item.get('title', chat_key)}\n"
        f"Chat ID: {chat_key}\n"
        f"Pending requests: {pending_count}\n"
        f"In-chat service messages pending delete: {pending_msgs}\n"
        f"Admin Contact: {ADMIN_CONTACT}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Get invite link", callback_data=f"invite:{chat_key}")],
            [InlineKeyboardButton(pending_label, callback_data=f"pending:{chat_key}:0")],
            [InlineKeyboardButton(clean_label, callback_data=f"clearchatmsgs:{chat_key}")],
            [InlineKeyboardButton("🗑 Remove group", callback_data=f"removeask:{chat_key}")],
            [InlineKeyboardButton("⬅️ Groups", callback_data="list_groups")],
        ]),
    )


# ---------------------------------------------------------------------------
#  Remove group
# ---------------------------------------------------------------------------

async def ask_remove_group(update: Update, chat_key: str) -> None:
    query = update.callback_query
    await query.answer()
    groups = load_groups()
    item = groups.get(chat_key)
    if not item:
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="list_groups")]]),
        )
        return
    await query.edit_message_text(
        f"⚠️ Remove group: {item.get('title', chat_key)}?\n\n"
        "This will unregister the group from the bot. The bot will stop processing "
        "join requests and membership events for this group.\n\n"
        "This does NOT remove the bot from the Telegram group or affect existing members.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Yes, remove group", callback_data=f"removeconfirm:{chat_key}")],
            [InlineKeyboardButton("Cancel", callback_data=f"detail:{chat_key}")],
        ]),
    )


async def confirm_remove_group(update: Update, chat_key: str) -> None:
    query = update.callback_query
    groups = load_groups()
    item = groups.pop(chat_key, None)
    if not item:
        await query.answer("Group was already removed.")
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="list_groups")]]),
        )
        return
    try:
        save_groups(groups)
    except OSError as exc:
        log.warning("Could not save after removing group %s: %s", chat_key, exc)
        await query.answer("Could not save. Check storage.", show_alert=True)
        return
    await query.answer("Group removed.")
    await query.edit_message_text(
        f"Removed: {item.get('title', chat_key)}\n\n"
        "The group has been unregistered. Existing membership logs and pending requests "
        "for this group are preserved in the data files.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Groups", callback_data="list_groups")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]),
    )


# ---------------------------------------------------------------------------
#  View Pending Requests
# ---------------------------------------------------------------------------

def _pending_for_group(chat_key: str) -> list[tuple[str, dict[str, Any]]]:
    """Return (token, record) pairs for pending requests in *chat_key*, newest first."""
    requests = load_requests()
    pending = [
        (tok, rec) for tok, rec in requests.items()
        if str(rec.get("chat_id")) == chat_key
        and rec.get("status") in ("requested", "payment_pending", "payment_message_sent")
    ]
    pending.sort(key=lambda pair: pair[1].get("request_time", ""), reverse=True)
    return pending


async def show_pending_requests(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                chat_key: str, page: int) -> None:
    """Display paginated pending join requests for a group."""
    query = update.callback_query
    await query.answer()

    groups = load_groups()
    group = groups.get(chat_key)
    if not group:
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="list_groups")]]),
        )
        return

    pending = _pending_for_group(chat_key)
    total = len(pending)

    if total == 0:
        await query.edit_message_text(
            f"📩 Pending requests — {group.get('title', chat_key)}\n\n"
            "No pending join requests recorded for this group.\n\n"
            "• New requests arrive here automatically via the bot's invite link.\n"
            f"• To import requests submitted before the bot joined, use:\n"
            f"  `/addrequest {chat_key} <user_id> [Name]`\n\n"
            f"Admin Contact: {ADMIN_CONTACT}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"pending:{chat_key}:0")],
                [InlineKeyboardButton("⬅️ Group settings", callback_data=f"detail:{chat_key}")],
            ]),
        )
        return

    total_pages = (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * PENDING_PAGE_SIZE
    page_items = pending[start:start + PENDING_PAGE_SIZE]
    num_badges = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    lines = []
    for idx, (token, rec) in enumerate(page_items, start=1):
        badge = num_badges[idx - 1] if idx <= len(num_badges) else f"#{idx}"
        name = rec.get("full_name") or "Unknown"
        username = f" (@{rec['username']})" if rec.get("username") else ""
        uid = rec.get("user_id", "?")
        status = rec.get("status", "requested")
        status_label = {
            "requested": "⏳ Awaiting decision",
            "payment_pending": "💳 Payment pending",
            "payment_message_sent": "💳 Payment message sent",
        }.get(status, status)
        req_time = rec.get("request_time", "unknown")
        if req_time != "unknown":
            req_time = req_time.replace("T", " ").replace("+00:00", " UTC")
        lines.append(
            f"{badge} {name}{username}\n"
            f"   ID: {uid} | {status_label}\n"
            f"   Requested: {req_time}"
        )

    body = "\n\n".join(lines)
    header = (
        f"📩 Pending requests — {group.get('title', chat_key)}\n"
        f"Showing {start + 1}–{start + len(page_items)} of {total}"
    )
    if total_pages > 1:
        header += f" (page {page + 1}/{total_pages})"

    buttons = []
    for idx, (token, rec) in enumerate(page_items, start=1):
        badge = num_badges[idx - 1] if idx <= len(num_badges) else f"#{idx}"
        buttons.append([
            InlineKeyboardButton(f"{badge} ✅ Accept", callback_data=f"preq:approve:{token}"),
            InlineKeyboardButton(f"{badge} ❌ Reject", callback_data=f"preq:decline:{token}"),
            InlineKeyboardButton(f"{badge} 💳 Pay", callback_data=f"preq:payment:{token}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pending:{chat_key}:{page - 1}"))
    nav.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"pending:{chat_key}:{page}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"pending:{chat_key}:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Group settings", callback_data=f"detail:{chat_key}")])

    help_hint = "\n\n👇 Numbered buttons below match the requests above in order:"
    await query.edit_message_text(
        f"{header}\n\n{body}{help_hint}\n\nAdmin contact: {ADMIN_CONTACT}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_pending_request_action(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                        data: str) -> None:
    """Handle Accept/Reject/Payment from the View Pending Requests list."""
    query = update.callback_query
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in ("approve", "decline", "payment"):
        await query.answer("Invalid action.", show_alert=True)
        return

    action = parts[1]
    token = parts[2]

    requests = load_requests()
    record = requests.get(token)
    if not record:
        await query.answer("Request record not found. It may have been removed.", show_alert=True)
        return

    chat_id = int(record.get("chat_id", 0))
    user_id = int(record.get("user_id", 0))
    chat_key = str(chat_id)
    group = load_groups().get(chat_key, {})
    full_name = record.get("full_name") or "User"

    if record.get("status") in ("approved", "rejected"):
        await query.answer(
            f"This request was already {record['status']}. Refreshing list.",
            show_alert=True,
        )
        await show_pending_requests(update, context, chat_key, 0)
        return

    try:
        if action == "payment":
            record["status"] = "payment_pending"
            save_requests(requests)
            message = PAYMENT_MESSAGE.format(
                group=record.get("group_title", "the group"),
                admin=ADMIN_CONTACT,
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
                await query.answer(f"Payment message sent to {full_name}.")
            except TelegramError:
                bot = await context.bot.get_me()
                start_link = f"https://t.me/{bot.username}?start=pay_{token}"
                await query.answer(
                    f"Payment status saved. User hasn't opened bot yet. Share link with them.",
                    show_alert=True,
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"💳 Payment pending for {full_name} "
                                f"({group.get('title', chat_key)})\n\n"
                                f"The user hasn't opened the bot yet. "
                                f"Send them this link:\n{start_link}\n\n"
                                f"Admin contact: {ADMIN_CONTACT}"
                            ),
                            disable_web_page_preview=True,
                        )
                    except TelegramError:
                        pass
        elif action == "approve":
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            record["status"] = "approved"
            save_requests(requests)
            # Join event will be recorded automatically by member_update / on_service_membership_message
            await query.answer(f"Approved {full_name}.")
        elif action == "decline":
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            record["status"] = "rejected"
            save_requests(requests)
            await query.answer(f"Rejected {full_name}.")

        await show_pending_requests(update, context, chat_key, 0)

    except TelegramError as exc:
        log.warning("Pending request action %s failed for token %s: %s", action, token, exc)
        err_msg = str(exc)
        if "HIDE_REQUESTER_MISSING" in err_msg or "USER_ALREADY_PARTICIPANT" in err_msg:
            record["status"] = "approved" if action == "approve" else "expired"
            save_requests(requests)
            await query.answer(
                "The join request no longer exists at Telegram (user may already be a member or withdrew).",
                show_alert=True,
            )
        else:
            await query.answer(f"Failed: {err_msg[:180]}", show_alert=True)
        await show_pending_requests(update, context, chat_key, 0)
    except OSError as exc:
        log.warning("Storage error processing pending request %s: %s", token, exc)
        await query.answer("Storage error. Check disk.", show_alert=True)


# ---------------------------------------------------------------------------
#  All Pending Requests (global view across all groups)
# ---------------------------------------------------------------------------

def _all_pending() -> list[tuple[str, dict[str, Any]]]:
    """Return (token, record) pairs for ALL pending requests, newest first."""
    requests = load_requests()
    pending = [
        (tok, rec) for tok, rec in requests.items()
        if rec.get("status") in ("requested", "payment_pending", "payment_message_sent")
    ]
    pending.sort(key=lambda pair: pair[1].get("request_time", ""), reverse=True)
    return pending


async def show_all_pending_requests(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    page: int) -> None:
    """Display paginated pending join requests across ALL groups."""
    query = update.callback_query
    await query.answer()

    pending = _all_pending()
    total = len(pending)

    if total == 0:
        await query.edit_message_text(
            "📩 All Pending Requests\n\n"
            "No pending join requests across any group.\n\n"
            "New requests arrive here automatically when users click the invite link.\n\n"
            f"Admin Contact: {ADMIN_CONTACT}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="all_pending:0")],
                [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
            ]),
        )
        return

    total_pages = (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * PENDING_PAGE_SIZE
    page_items = pending[start:start + PENDING_PAGE_SIZE]
    num_badges = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    groups = load_groups()
    lines = []
    for idx, (token, rec) in enumerate(page_items, start=1):
        badge = num_badges[idx - 1] if idx <= len(num_badges) else f"#{idx}"
        name = rec.get("full_name") or "Unknown"
        username = f" (@{rec['username']})" if rec.get("username") else ""
        uid = rec.get("user_id", "?")
        chat_key = str(rec.get("chat_id", ""))
        group = groups.get(chat_key, {})
        group_title = group.get("title", rec.get("group_title", chat_key))
        status = rec.get("status", "requested")
        status_label = {
            "requested": "⏳ Awaiting decision",
            "payment_pending": "💳 Payment pending",
            "payment_message_sent": "💳 Payment message sent",
        }.get(status, status)
        req_time = rec.get("request_time", "unknown")
        if req_time != "unknown":
            req_time = req_time.replace("T", " ").replace("+00:00", " UTC")
        lines.append(
            f"{badge} {name}{username}\n"
            f"   Group: {group_title}\n"
            f"   ID: {uid} | {status_label}\n"
            f"   Requested: {req_time}"
        )

    body = "\n\n".join(lines)
    header = (
        f"📩 All Pending Requests\n"
        f"Showing {start + 1}–{start + len(page_items)} of {total}"
    )
    if total_pages > 1:
        header += f" (page {page + 1}/{total_pages})"

    buttons = []
    for idx, (token, rec) in enumerate(page_items, start=1):
        badge = num_badges[idx - 1] if idx <= len(num_badges) else f"#{idx}"
        buttons.append([
            InlineKeyboardButton(f"{badge} ✅ Accept", callback_data=f"apreq:approve:{token}:{page}"),
            InlineKeyboardButton(f"{badge} ❌ Reject", callback_data=f"apreq:decline:{token}:{page}"),
            InlineKeyboardButton(f"{badge} 💳 Pay", callback_data=f"apreq:payment:{token}:{page}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"all_pending:{page - 1}"))
    nav.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"all_pending:{page}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"all_pending:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])

    help_hint = "\n\n👇 Numbered buttons below match the requests above in order:"
    await query.edit_message_text(
        f"{header}\n\n{body}{help_hint}\n\nAdmin contact: {ADMIN_CONTACT}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_all_pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    data: str) -> None:
    """Handle Accept/Reject/Payment from the All Pending Requests list."""
    query = update.callback_query
    parts = data.split(":", 3)
    if len(parts) != 4 or parts[1] not in ("approve", "decline", "payment"):
        await query.answer("Invalid action.", show_alert=True)
        return

    action = parts[1]
    token = parts[2]
    try:
        return_page = int(parts[3])
    except ValueError:
        return_page = 0

    requests = load_requests()
    record = requests.get(token)
    if not record:
        await query.answer("Request record not found. It may have been removed.", show_alert=True)
        return

    chat_id = int(record.get("chat_id", 0))
    user_id = int(record.get("user_id", 0))
    chat_key = str(chat_id)
    group = load_groups().get(chat_key, {})
    full_name = record.get("full_name") or "User"

    if record.get("status") in ("approved", "rejected"):
        await query.answer(
            f"This request was already {record['status']}. Refreshing list.",
            show_alert=True,
        )
        await show_all_pending_requests(update, context, return_page)
        return

    try:
        if action == "payment":
            record["status"] = "payment_pending"
            save_requests(requests)
            message = PAYMENT_MESSAGE.format(
                group=record.get("group_title", "the group"),
                admin=ADMIN_CONTACT,
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
                await query.answer(f"Payment message sent to {full_name}.")
            except TelegramError:
                bot = await context.bot.get_me()
                start_link = f"https://t.me/{bot.username}?start=pay_{token}"
                await query.answer(
                    f"Payment status saved. User hasn't opened bot yet. Share link with them.",
                    show_alert=True,
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"💳 Payment pending for {full_name} "
                                f"({group.get('title', chat_key)})\n\n"
                                f"The user hasn't opened the bot yet. "
                                f"Send them this link:\n{start_link}\n\n"
                                f"Admin contact: {ADMIN_CONTACT}"
                            ),
                            disable_web_page_preview=True,
                        )
                    except TelegramError:
                        pass
        elif action == "approve":
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            record["status"] = "approved"
            save_requests(requests)
            await query.answer(f"Approved {full_name}.")
        elif action == "decline":
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            record["status"] = "rejected"
            save_requests(requests)
            await query.answer(f"Rejected {full_name}.")

        await show_all_pending_requests(update, context, return_page)

    except TelegramError as exc:
        log.warning("All-pending action %s failed for token %s: %s", action, token, exc)
        err_msg = str(exc)
        if "HIDE_REQUESTER_MISSING" in err_msg or "USER_ALREADY_PARTICIPANT" in err_msg:
            record["status"] = "approved" if action == "approve" else "expired"
            save_requests(requests)
            await query.answer(
                "The join request no longer exists at Telegram (user may already be a member or withdrew).",
                show_alert=True,
            )
        else:
            await query.answer(f"Failed: {err_msg[:180]}", show_alert=True)
        await show_all_pending_requests(update, context, return_page)
    except OSError as exc:
        log.warning("Storage error processing pending request %s: %s", token, exc)
        await query.answer("Storage error. Check disk.", show_alert=True)


# ---------------------------------------------------------------------------
#  Membership history (Permanently stored — viewing only)
# ---------------------------------------------------------------------------

async def show_history_groups(update: Update) -> None:
    query = update.callback_query
    await query.answer()
    groups = load_groups()
    if not groups:
        await query.edit_message_text(
            "No groups registered yet. Add the bot as admin and run /addgroup in each group.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
        )
        return
    rows = [
        [InlineKeyboardButton(item.get("title", key)[:60], callback_data=f"history:{key}")]
        for key, item in sorted(groups.items(), key=lambda pair: _group_sort_key(pair[1].get("title", "")))
    ]
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
    await query.edit_message_text("Choose a group to view membership history:", reply_markup=InlineKeyboardMarkup(rows))


async def show_group_history_menu(update: Update, chat_key: str) -> None:
    query = update.callback_query
    groups = load_groups()
    group = groups.get(chat_key)
    await query.answer()
    if not group:
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="history_groups")]]),
        )
        return
    events = [e for e in load_events() if str(e.get("chat_id")) == str(chat_key)]
    joins = sum(1 for e in events if e.get("event") == "join")
    leaves = sum(1 for e in events if e.get("event") == "leave")

    service_msgs = load_service_messages()
    pending_msgs = len(service_msgs.get(str(chat_key), []))
    del_btn_label = f"🗑 Delete entry/exit messages from chat ({pending_msgs})" if pending_msgs else "🗑 Delete entry/exit messages from chat"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Last 10 unique joiners", callback_data=f"recent:{chat_key}")],
        [InlineKeyboardButton("📜 Recent entry/exit logs", callback_data=f"events:{chat_key}")],
        [InlineKeyboardButton(del_btn_label, callback_data=f"clearchatmsgs:{chat_key}")],
        [InlineKeyboardButton("⬅️ Groups", callback_data="history_groups")],
    ])
    await query.edit_message_text(
        f"Membership history: {group.get('title', chat_key)}\n\n"
        f"Recorded joins: {joins}\nRecorded leaves: {leaves}\n\n"
        "📁 All membership history is permanently stored in bot logs.\n"
        f"💬 In-chat entry/exit messages waiting to be deleted: {pending_msgs}\n\n"
        "Press 'Delete entry/exit messages from chat' when you want to remove join/leave bubbles from the Telegram group.",
        reply_markup=keyboard,
    )


async def show_recent_joins(update: Update, chat_key: str) -> None:
    query = update.callback_query
    await query.answer()
    group = load_groups().get(chat_key)
    if not group:
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="history_groups")]]),
        )
        return
    joins = [
        e for e in load_events()
        if str(e.get("chat_id")) == str(chat_key) and e.get("event") == "join"
    ]
    joins.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    unique_joins = []
    seen_user_ids = set()
    for event in joins:
        user_id = event.get("user_id")
        if user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)
        unique_joins.append(event)
        if len(unique_joins) == 10:
            break
    joins = unique_joins
    if not joins:
        body = "No join events have been recorded for this group yet."
    else:
        lines = []
        for index, event in enumerate(joins, 1):
            name = event.get("full_name") or "Unknown user"
            username = f" (@{event['username']})" if event.get("username") else ""
            when = event.get("timestamp", "unknown time").replace("T", " ").replace("+00:00", " UTC")
            lines.append(f"{index}. {name}{username}\n   User ID: {event.get('user_id', '?')}\n   Joined: {when}")
        body = "\n\n".join(lines)
    await query.edit_message_text(
        f"👥 Last {len(joins)} unique joiner(s) — {group.get('title', chat_key)}\n\n{body}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"recent:{chat_key}")],
            [InlineKeyboardButton("⬅️ Group history", callback_data=f"history:{chat_key}")],
        ]),
    )


async def show_recent_events(update: Update, chat_key: str) -> None:
    query = update.callback_query
    await query.answer()
    group = load_groups().get(chat_key)
    if not group:
        await query.edit_message_text(
            "That group is no longer registered.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Groups", callback_data="history_groups")]]),
        )
        return
    events = [e for e in load_events() if str(e.get("chat_id")) == str(chat_key)]
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    events = events[:20]
    if not events:
        body = "No entry/exit events have been recorded yet."
    else:
        lines = []
        for event in events:
            label = "JOINED" if event.get("event") == "join" else "LEFT"
            name = event.get("full_name") or "Unknown user"
            username = f" (@{event['username']})" if event.get("username") else ""
            when = event.get("timestamp", "unknown time").replace("T", " ").replace("+00:00", " UTC")
            lines.append(f"• {label}: {name}{username} — ID {event.get('user_id', '?')}\n  {when}")
        body = "\n".join(lines)

    service_msgs = load_service_messages()
    pending_msgs = len(service_msgs.get(str(chat_key), []))
    del_btn_label = f"🗑 Delete messages from chat ({pending_msgs})" if pending_msgs else "🗑 Delete messages from chat"

    await query.edit_message_text(
        f"📜 Recent entry/exit logs — {group.get('title', chat_key)}\n\n{body}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"events:{chat_key}")],
            [InlineKeyboardButton(del_btn_label, callback_data=f"clearchatmsgs:{chat_key}")],
            [InlineKeyboardButton("⬅️ Group history", callback_data=f"history:{chat_key}")],
        ]),
    )


# ---------------------------------------------------------------------------
#  Membership event handler (ChatMember updates)
# ---------------------------------------------------------------------------

async def member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.chat_member
    if not change:
        return
    chat_id = change.chat.id
    group = load_groups().get(str(chat_id))
    if not group:
        return

    old_member = change.old_chat_member
    new_member = change.new_chat_member

    if new_member.user.is_bot and new_member.user.id == (await context.bot.get_me()).id:
        return

    was_member = is_member_status(old_member)
    now_member = is_member_status(new_member)
    if was_member == now_member:
        return

    user = new_member.user
    event_type = "join" if now_member else "leave"
    full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or f"User {user.id}"
    group_title = group.get("title", change.chat.title or str(chat_id))
    record_event(chat_id, group_title, user.id, full_name, user.username, event_type)


# ---------------------------------------------------------------------------
#  Main callback dispatcher
# ---------------------------------------------------------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not is_admin(update):
        await query.answer("Not authorised.", show_alert=True)
        return

    data = query.data or ""

    if data == "menu":
        await query.answer()
        await query.edit_message_text("Group Manager (admin only)", reply_markup=main_menu())

    elif data == "help_add":
        await query.answer()
        await query.edit_message_text(
            "To register a group:\n"
            "1. Add this bot to the group as an administrator.\n"
            "2. Enable permissions to invite users, manage join requests, and delete messages.\n"
            "3. Run /addgroup inside that group.\n\n"
            "Then return here and choose Get invite link.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
        )

    elif data == "help_commands":
        await query.answer()
        await query.edit_message_text(
            get_help_text(),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
            parse_mode="HTML",
        )

    elif data in ("list_links", "link_combine"):
        await show_all_links(update, context, page=0)

    elif data.startswith("all_links:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await show_all_links(update, context, page=page)

    elif data == "export_links":
        await export_all_links(update, context)

    elif data == "link_pick":
        await show_link_pick(update)

    elif data == "list_groups":
        await show_groups(update, context, links=False)

    elif data.startswith("invite:"):
        await query.answer()
        key = data.split(":", 1)[1]
        groups = load_groups()
        item = groups.get(key)
        if not item:
            await query.edit_message_text(
                "That group is no longer registered.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="list_links")]]),
            )
            return

        link = item.get("last_invite_link")
        if not link:
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=int(item["chat_id"]),
                    creates_join_request=True,
                    name="Admin bot join requests",
                )
                link = invite.invite_link
                item["last_invite_link"] = link
                save_groups(groups)
            except (TelegramError, KeyError, ValueError) as exc:
                log.warning("Invite link creation failed for %s: %s", key, exc)
                await query.edit_message_text(
                    "Could not create an invite link. Check that the bot is an administrator "
                    "with permission to invite users and manage join requests.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔗 All group links", callback_data="list_links")],
                        [InlineKeyboardButton("📌 Single group", callback_data="link_pick")],
                        [InlineKeyboardButton("⬅️ Group settings", callback_data=f"detail:{key}")],
                        [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
                    ]),
                )
                return

        await query.edit_message_text(
            f"Invite link for {item.get('title', key)}:\n\n{link}\n\n"
            "This link requires admin approval for every join request.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 All group links", callback_data="list_links")],
                [InlineKeyboardButton("📌 Single group", callback_data="link_pick")],
                [InlineKeyboardButton("⬅️ Group settings", callback_data=f"detail:{key}")],
                [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
            ]),
            disable_web_page_preview=True,
        )

    elif data.startswith("detail:"):
        await show_group_detail(update, context, data.split(":", 1)[1])

    elif data.startswith("removeask:"):
        await ask_remove_group(update, data.split(":", 1)[1])

    elif data.startswith("removeconfirm:"):
        await confirm_remove_group(update, data.split(":", 1)[1])

    elif data.startswith("pending:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            chat_key = parts[1]
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
            await show_pending_requests(update, context, chat_key, page)
        else:
            await query.answer("Invalid pending request data.", show_alert=True)

    elif data.startswith("preq:"):
        await handle_pending_request_action(update, context, data)

    elif data.startswith("all_pending:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await show_all_pending_requests(update, context, page)

    elif data.startswith("apreq:"):
        await handle_all_pending_action(update, context, data)

    elif data == "history_groups":
        await show_history_groups(update)

    elif data.startswith("history:"):
        await show_group_history_menu(update, data.split(":", 1)[1])

    elif data.startswith("recent:"):
        await show_recent_joins(update, data.split(":", 1)[1])

    elif data.startswith("delchatmsg:"):
        await delete_single_chat_service_message(update, context, data)

    elif data.startswith("clearchatmsgs:"):
        await clear_group_chat_service_messages(update, context, data.split(":", 1)[1])

    elif data.startswith("events:"):
        await show_recent_events(update, data.split(":", 1)[1])

    elif data.startswith("request:"):
        await handle_decision(update, context, data)

    elif data == "backup":
        await query.answer("Sending backup files… 📦")
        await send_backup_to_admins(context)
        try:
            await query.edit_message_text(
                "📦 Backup sent!\n\nAll four data files (groups, requests, events, service_messages) "
                "have been sent to your private chat above.\n\n"
                "A fresh backup is also automatically sent every night at 9:00 PM IST.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="menu")]]),
            )
        except TelegramError:
            pass

    else:
        await query.answer("Unknown action.", show_alert=True)
        log.warning("Unhandled callback_data: %r", data)


# ---------------------------------------------------------------------------
#  Join-request decision from admin notification (Accept / Reject / Payment)
# ---------------------------------------------------------------------------

async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    parts = data.split(":")
    if len(parts) != 4 or parts[1] not in ("approve", "decline", "payment"):
        await query.answer("Invalid request.", show_alert=True)
        return
    action, chat_id_text, user_id_text = parts[1], parts[2], parts[3]
    record: dict[str, Any] | None = None
    requests: dict[str, dict[str, Any]] = {}
    try:
        chat_id, user_id = int(chat_id_text), int(user_id_text)
        requests = load_requests()
        token = next((
            key for key, item in reversed(list(requests.items()))
            if int(item.get("chat_id", 0)) == chat_id and int(item.get("user_id", 0)) == user_id
        ), None)
        record = requests.get(token) if token else None

        if record and record.get("status") in ("approved", "rejected"):
            await query.answer(
                f"This request was already {record['status']}.",
                show_alert=True,
            )
            old = query.message.text or "Join request"
            await query.edit_message_text(f"{old}\n\nℹ️ Already {record['status']}.")
            return

        if action == "payment":
            if not record:
                await query.answer("Request record not found.", show_alert=True)
                return
            record["status"] = "payment_pending"
            save_requests(requests)
            message = PAYMENT_MESSAGE.format(
                group=record.get("group_title", "the group"),
                admin=ADMIN_CONTACT,
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
                result = "Payment pending: message sent directly to applicant."
                await query.answer("Payment message sent.")
                old = query.message.text or "Join request"
                await query.edit_message_text(f"{old}\n\n💳 {result}\nAdmin contact: {ADMIN_CONTACT}")
            except TelegramError:
                bot = await context.bot.get_me()
                start_link = f"https://t.me/{bot.username}?start=pay_{token}"
                result = (
                    f"Payment status recorded for {record.get('full_name', 'applicant')}.\n"
                    f"The applicant has not opened the bot yet, so send them this link to receive instructions:\n"
                    f"{start_link}\n\n"
                    f"Admin contact: {ADMIN_CONTACT}"
                )
                await query.answer("Payment status saved; share the link with applicant.", show_alert=True)
                old = query.message.text or "Join request"
                await query.edit_message_text(
                    f"{old}\n\n💳 Payment Pending\n{result}",
                    disable_web_page_preview=True,
                )
            return

        if action == "approve":
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            result = "Request approved."
            status = "approved"
            if record:
                record["status"] = status
                save_requests(requests)
            # Join event will be recorded automatically by member_update / on_service_membership_message
        else:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            result = "Request rejected."
            status = "rejected"
            if record:
                record["status"] = status
                save_requests(requests)

        await query.answer(result)
        old = query.message.text or "Join request"
        await query.edit_message_text(f"{old}\n\n✅ {result}")
    except TelegramError as exc:
        log.warning("Could not process join request: %s", exc)
        err_msg = str(exc)
        if "HIDE_REQUESTER_MISSING" in err_msg or "USER_ALREADY_PARTICIPANT" in err_msg:
            if record:
                record["status"] = "expired"
                save_requests(requests)
            await query.answer(
                "The join request no longer exists (user may have withdrawn or already joined).",
                show_alert=True,
            )
            old = query.message.text or "Join request"
            await query.edit_message_text(f"{old}\n\nℹ️ Request expired or already handled.")
        else:
            await query.answer(f"Could not process: {err_msg[:180]}", show_alert=True)
    except (OSError, ValueError) as exc:
        log.warning("Could not process join request: %s", exc)
        await query.answer("Could not process request. Check storage.", show_alert=True)


# ---------------------------------------------------------------------------
#  Join-request handler (incoming requests from Telegram)
# ---------------------------------------------------------------------------

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    if not request:
        return
    groups = load_groups()
    group = groups.get(str(request.chat.id))
    if not group:
        return

    user = request.from_user
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part) or f"User {user.id}"
    request_records = load_requests()

    # --- Duplicate check: skip if a pending request already exists for this user+group ---
    existing_token = next((
        tok for tok, rec in request_records.items()
        if int(rec.get("chat_id", 0)) == request.chat.id
        and int(rec.get("user_id", 0)) == user.id
        and rec.get("status") in ("requested", "payment_pending", "payment_message_sent")
    ), None)

    if existing_token:
        # Update name/username in case they changed, but don't create a new record or notify again
        existing_rec = request_records[existing_token]
        existing_rec["full_name"] = full_name
        existing_rec["username"] = user.username
        save_requests(request_records)
        log.info(
            "Duplicate join request from user %s for chat %s — skipped (existing token %s)",
            user.id, request.chat.id, existing_token,
        )
        return

    token = secrets.token_urlsafe(9)
    request_records[token] = {
        "chat_id": request.chat.id,
        "user_id": user.id,
        "full_name": full_name,
        "username": user.username,
        "group_title": group.get("title", request.chat.title or str(request.chat.id)),
        "status": "requested",
        "request_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_requests(request_records)
    username = f"@{user.username}" if user.username else "(no username)"
    invite_name = request.invite_link.name if request.invite_link else None
    text = (
        "📩 New join request\n\n"
        f"Group: {group.get('title', request.chat.title or request.chat.id)}\n"
        f"Applicant: {full_name}\n"
        f"Username: {username}\n"
        f"User ID: {user.id}\n"
        f"Admin Contact: {ADMIN_CONTACT}"
    )
    if invite_name:
        text += f"\nInvite link label: {invite_name}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept", callback_data=f"request:approve:{request.chat.id}:{user.id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"request:decline:{request.chat.id}:{user.id}")],
        [InlineKeyboardButton("💳 Payment Pending", callback_data=f"request:payment:{request.chat.id}:{user.id}")],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
        except TelegramError as exc:
            log.warning("Could not message admin %s: %s", admin_id, exc)


# ---------------------------------------------------------------------------
#  Data backup (on-demand button + scheduled nightly at 9 PM IST)
# ---------------------------------------------------------------------------

async def send_backup_to_admins(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send all four data files as Telegram documents to every configured admin."""
    files = [
        (DATA_FILE,             "groups.json"),
        (REQUESTS_FILE,         "requests.json"),
        (EVENTS_FILE,           "events.json"),
        (SERVICE_MESSAGES_FILE, "service_messages.json"),
    ]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    caption = f"📦 Bot data backup — {now_str}"

    for admin_id in ADMIN_IDS:
        first = True
        for path, name in files:
            if not path.exists():
                continue
            try:
                with open(path, "rb") as fh:
                    await context.bot.send_document(
                        chat_id=admin_id,
                        document=fh,
                        filename=name,
                        caption=caption if first else None,
                    )
                first = False
            except TelegramError as exc:
                log.warning("Could not send backup file %s to admin %s: %s", name, admin_id, exc)


# ---------------------------------------------------------------------------
#  Unknown commands
# ---------------------------------------------------------------------------

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_admin(update) and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Unknown command. Use /start to open the admin menu or /help for instructions."
        )


# ---------------------------------------------------------------------------
#  Application entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            f"Missing BOT_TOKEN. Add it to {BASE_DIR / '.env'} or set it in your environment."
        )
    if not ADMIN_IDS:
        raise RuntimeError(
            f"Missing ADMIN_IDS. Add your numeric Telegram user ID(s) to {BASE_DIR / '.env'}."
        )

    log.info("Configured administrator user IDs: %s", sorted(ADMIN_IDS))
    log.info("Configured admin contact: %s", ADMIN_CONTACT)
    log.info("Project directory: %s", BASE_DIR)
    log.info("Groups data file: %s", DATA_FILE)
    log.info("Join requests data file: %s", REQUESTS_FILE)
    log.info("Membership events file: %s", EVENTS_FILE)
    log.info("Service messages file: %s", SERVICE_MESSAGES_FILE)

    app = Application.builder().token(BOT_TOKEN).build()

    # Schedule nightly backup at 9:00 PM IST (UTC+5:30 = 15:30 UTC)
    IST = timezone(timedelta(hours=5, minutes=30))
    if app.job_queue:
        app.job_queue.run_daily(
            send_backup_to_admins,
            time=dt_time(hour=21, minute=0, tzinfo=IST),
            name="nightly_backup",
        )
        log.info("Nightly backup scheduled at 21:00 IST.")
    else:
        log.warning("JobQueue not available — nightly backup will not run.")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("groups", groups_command))
    app.add_handler(CommandHandler("links", links_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("addgroup", addgroup))
    app.add_handler(CommandHandler("addrequest", addrequest))
    app.add_handler(CommandHandler("addlog", addlog))
    app.add_handler(CommandHandler("del", del_command))
    app.add_handler(CommandHandler("delmessages", delmessages_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        on_service_membership_message
    ))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.run_polling(allowed_updates=["message", "callback_query", "chat_join_request", "chat_member"])


if __name__ == "__main__":
    main()
