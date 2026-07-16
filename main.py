"""
🤖 Instagram Coke — Telegram Bot
================================
Framework : python-telegram-bot v21+
Database  : SQLite (database.db)
Deploy    : Railway / any VPS
Author    : Instagram Coke Team
"""

# ─────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────
import os
import sqlite3
import logging
import time
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))          # Your Telegram user ID
DB_PATH    = os.getenv("DB_PATH", "database.db")

# Flood-protection: max messages per window
FLOOD_LIMIT  = 5    # messages
FLOOD_WINDOW = 10   # seconds

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONVERSATION STATES
# ─────────────────────────────────────────────
(
    STEP_USERNAMES,     # waiting for Instagram usernames
    STEP_NOTE,          # waiting for note / request description
    SUPPORT_MSG,        # waiting for support message
    BROADCAST_MSG,      # admin: waiting for broadcast text
    BAN_USER_ID,        # admin: waiting for user-id to ban
    UNBAN_USER_ID,      # admin: waiting for user-id to unban
    DELETE_REQ_ID,      # admin: waiting for request-id to delete
) = range(7)

# ─────────────────────────────────────────────
# MAIN KEYBOARD
# ─────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["🚀 Start", "👤 Profile"], ["📩 Support", "ℹ️ About"]],
    resize_keyboard=True,
)

# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Return a thread-safe SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            -- Users table
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                join_date   TEXT NOT NULL
            );

            -- Requests table
            CREATE TABLE IF NOT EXISTS requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                ig_usernames    TEXT NOT NULL,
                note            TEXT,
                submitted_at    TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            -- Banned users table
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id     INTEGER PRIMARY KEY,
                banned_at   TEXT NOT NULL,
                reason      TEXT
            );
        """)
    logger.info("✅ Database initialised at %s", DB_PATH)


def upsert_user(user_id: int, username: str | None, first_name: str) -> None:
    """Insert or update a user record."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, join_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )


def is_banned(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def save_request(user_id: int, ig_usernames: str, note: str) -> int:
    """Save a request and return its new ID."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO requests (user_id, ig_usernames, note, submitted_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, ig_usernames, note, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return cur.lastrowid


def get_user_profile(user_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def count_user_requests(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM requests WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["cnt"]


def get_stats() -> dict:
    with get_db() as conn:
        total_users    = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        total_requests = conn.execute("SELECT COUNT(*) AS c FROM requests").fetchone()["c"]
        banned         = conn.execute("SELECT COUNT(*) AS c FROM banned_users").fetchone()["c"]
    return {"users": total_users, "requests": total_requests, "banned": banned}


def get_all_users() -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute("SELECT * FROM users ORDER BY join_date DESC").fetchall()


def get_all_requests(limit: int = 20) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT r.*, u.username, u.first_name
            FROM requests r
            LEFT JOIN users u ON r.user_id = u.user_id
            ORDER BY r.submitted_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def ban_user(user_id: int, reason: str = "Admin decision") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO banned_users (user_id, banned_at, reason) VALUES (?, ?, ?)",
            (user_id, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), reason),
        )


def unban_user(user_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))


def delete_request(req_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM requests WHERE id = ?", (req_id,))
    return cur.rowcount > 0

# ─────────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────────

def admin_only(func):
    """Decorator — restrict handler to ADMIN_ID."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user is None or user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Access denied.")
            return ConversationHandler.END
        return await func(update, ctx, *args, **kwargs)
    return wrapper


def ban_check(func):
    """Decorator — block banned users."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user and is_banned(user.id):
            await update.effective_message.reply_text(
                "🚫 You have been banned from using this bot."
            )
            return ConversationHandler.END
        return await func(update, ctx, *args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────
# FLOOD PROTECTION
# ─────────────────────────────────────────────
_flood_data: dict[int, list[float]] = {}

def flood_check(user_id: int) -> bool:
    """Return True if the user is flooding."""
    now   = time.monotonic()
    times = _flood_data.get(user_id, [])
    times = [t for t in times if now - t < FLOOD_WINDOW]
    times.append(now)
    _flood_data[user_id] = times
    return len(times) > FLOOD_LIMIT

# ─────────────────────────────────────────────
# /start COMMAND
# ─────────────────────────────────────────────

@ban_check
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — register user and show welcome."""
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)
    logger.info("User %s (%d) started the bot.", user.username, user.id)

    text = (
        "🤖 *Instagram Coke*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Welcome to *Instagram Coke* Utility Bot.\n\n"
        "This bot helps collect Instagram usernames for requests "
        "and provides admin support.\n\n"
        "Press the button below to get started."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_KEYBOARD,
    )

# ─────────────────────────────────────────────
# 🚀 START FLOW — ConversationHandler
# ─────────────────────────────────────────────

@ban_check
async def flow_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: ask for Instagram usernames."""
    user = update.effective_user
    if flood_check(user.id):
        await update.message.reply_text("⚠️ Slow down! Please wait a moment.")
        return ConversationHandler.END

    text = (
        "📝 *Step 1 / 2*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Send Instagram username(s)*\n\n"
        "Single:\n`username123`\n\n"
        "Multiple:\n`user1\nuser2\nuser3`\n\n"
        "You can send one or many usernames."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    return STEP_USERNAMES


async def flow_receive_usernames(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Save usernames and ask for note."""
    raw        = update.message.text.strip()
    usernames  = [u.strip() for u in raw.splitlines() if u.strip()]

    if not usernames:
        await update.message.reply_text("❌ No usernames found. Please try again.")
        return STEP_USERNAMES

    # Store temporarily in user_data
    ctx.user_data["ig_usernames"] = usernames
    logger.info("User %d submitted %d username(s).", update.effective_user.id, len(usernames))

    text = (
        "📝 *Step 2 / 2*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send any *note or request* related to these usernames.\n\n"
        "_Example:_\n"
        "• Account review\n"
        "• Support request\n"
        "• Username availability"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return STEP_NOTE


async def flow_receive_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Save note, persist request, send confirmation."""
    note      = update.message.text.strip()
    user      = update.effective_user
    usernames = ctx.user_data.get("ig_usernames", [])

    ig_str = "\n".join(usernames)
    req_id = save_request(user.id, ig_str, note)
    logger.info("Request #%d saved for user %d.", req_id, user.id)

    # Notify admin
    admin_text = (
        f"📨 *New Request #{req_id}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: [{user.first_name}](tg://user?id={user.id}) (`{user.id}`)\n"
        f"🔗 @{user.username or 'N/A'}\n\n"
        f"📋 *Usernames:*\n`{ig_str}`\n\n"
        f"📝 *Note:* {note}\n"
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    try:
        await ctx.bot.send_message(
            ADMIN_ID, admin_text, parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as e:
        logger.warning("Could not notify admin: %s", e)

    # Reply to user
    await update.message.reply_text(
        "✅ *Request Submitted Successfully*\n\n"
        "Your request has been recorded.\n\nThank you. 🙏",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_KEYBOARD,
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def flow_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the active conversation."""
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.", reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────
# 👤 PROFILE
# ─────────────────────────────────────────────

@ban_check
async def show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user    = update.effective_user
    profile = get_user_profile(user.id)
    total   = count_user_requests(user.id)

    if not profile:
        upsert_user(user.id, user.username, user.first_name)
        profile = get_user_profile(user.id)

    text = (
        "👤 *Your Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 *User ID:* `{user.id}`\n"
        f"📅 *Join Date:* `{profile['join_date']}`\n"
        f"📨 *Total Requests:* `{total}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────
# 📩 SUPPORT — ConversationHandler
# ─────────────────────────────────────────────

@ban_check
async def support_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📩 *Support*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send your message and we will forward it to the admin.\n\n"
        "Type /cancel to go back.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    return SUPPORT_MSG


async def support_receive(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    msg  = update.message.text.strip()

    admin_text = (
        f"📩 *Support Message*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 [{user.first_name}](tg://user?id={user.id}) (`{user.id}`)\n"
        f"🔗 @{user.username or 'N/A'}\n\n"
        f"💬 {msg}"
    )
    try:
        await ctx.bot.send_message(
            ADMIN_ID, admin_text, parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            "✅ Your message has been forwarded to the admin.\n\nWe'll get back to you soon!",
            reply_markup=MAIN_KEYBOARD,
        )
    except TelegramError as e:
        logger.warning("Support forward failed: %s", e)
        await update.message.reply_text(
            "⚠️ Could not reach admin right now. Please try later.",
            reply_markup=MAIN_KEYBOARD,
        )
    return ConversationHandler.END

# ─────────────────────────────────────────────
# ℹ️ ABOUT
# ─────────────────────────────────────────────

@ban_check
async def show_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ *About Instagram Coke Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 *Bot:* Instagram Coke\n"
        "🛠 *Version:* 1.0.0\n"
        "📦 *Framework:* python-telegram-bot v21+\n"
        "🗄 *Database:* SQLite\n\n"
        "This bot helps you submit Instagram usernames "
        "for review, account support, and availability checks.\n\n"
        "💡 Use 🚀 *Start* to submit a request.\n"
        "📩 Use *Support* to contact admin."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────
# 🎉 WELCOME NEW MEMBERS
# ─────────────────────────────────────────────

async def welcome_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Greet every new member that joins the group."""
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        upsert_user(member.id, member.username, member.first_name)
        await update.message.reply_text(
            f"🎉 *Welcome {member.first_name}!*\n\n"
            "Welcome to *Instagram Coke* Community. 🥤",
            parse_mode=ParseMode.MARKDOWN,
        )

# ─────────────────────────────────────────────
# 🔐 ADMIN PANEL
# ─────────────────────────────────────────────

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Statistics", callback_data="adm:stats"),
            InlineKeyboardButton("👥 Users",      callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton("📋 Requests",   callback_data="adm:requests"),
            InlineKeyboardButton("📨 Broadcast",  callback_data="adm:broadcast"),
        ],
        [
            InlineKeyboardButton("🚫 Ban User",   callback_data="adm:ban"),
            InlineKeyboardButton("✅ Unban User",  callback_data="adm:unban"),
        ],
        [
            InlineKeyboardButton("🗑 Delete Request", callback_data="adm:delreq"),
        ],
    ])


@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔐 *Admin Panel*\n━━━━━━━━━━━━━━━━━━━━\nChoose an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard(),
    )


async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Route admin inline-button presses."""
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ Access denied.")
        return

    data = query.data  # e.g. "adm:stats"

    # ── Statistics ──────────────────────────
    if data == "adm:stats":
        s = get_stats()
        text = (
            "📊 *Statistics*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 *Total Users:* `{s['users']}`\n"
            f"📨 *Total Requests:* `{s['requests']}`\n"
            f"🚫 *Banned Users:* `{s['banned']}`"
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="adm:back")]]
            ),
        )

    # ── Users list ──────────────────────────
    elif data == "adm:users":
        users = get_all_users()
        lines = [f"👥 *Users* ({len(users)} total)\n━━━━━━━━━━━━━━━━━━━━\n"]
        for u in users[:15]:  # cap at 15 to avoid message length errors
            uname = f"@{u['username']}" if u["username"] else "—"
            lines.append(f"• `{u['user_id']}` {u['first_name']} {uname}")
        if len(users) > 15:
            lines.append(f"\n_... and {len(users) - 15} more._")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="adm:back")]]
            ),
        )

    # ── Requests list ───────────────────────
    elif data == "adm:requests":
        reqs  = get_all_requests(10)
        lines = [f"📋 *Latest Requests* ({len(reqs)})\n━━━━━━━━━━━━━━━━━━━━\n"]
        for r in reqs:
            uname = f"@{r['username']}" if r["username"] else "—"
            ig    = r["ig_usernames"].replace("\n", ", ")
            lines.append(
                f"*#{r['id']}* | `{r['user_id']}` {uname}\n"
                f"  📋 `{ig[:40]}{'...' if len(ig) > 40 else ''}`\n"
                f"  📝 {(r['note'] or '—')[:40]}\n"
                f"  🕐 {r['submitted_at']}\n"
            )
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="adm:back")]]
            ),
        )

    # ── Broadcast — prompt via conversation ─
    elif data == "adm:broadcast":
        await query.edit_message_text(
            "📨 *Broadcast*\n\nSend the message you want to broadcast to all users.\n\n"
            "Type /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BROADCAST_MSG   # enter conversation state

    # ── Ban — prompt ─────────────────────────
    elif data == "adm:ban":
        await query.edit_message_text(
            "🚫 *Ban User*\n\nSend the *User ID* to ban.\n\nType /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BAN_USER_ID

    # ── Unban — prompt ───────────────────────
    elif data == "adm:unban":
        await query.edit_message_text(
            "✅ *Unban User*\n\nSend the *User ID* to unban.\n\nType /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return UNBAN_USER_ID

    # ── Delete Request — prompt ──────────────
    elif data == "adm:delreq":
        await query.edit_message_text(
            "🗑 *Delete Request*\n\nSend the *Request ID* to delete.\n\nType /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DELETE_REQ_ID

    # ── Back ─────────────────────────────────
    elif data == "adm:back":
        await query.edit_message_text(
            "🔐 *Admin Panel*\n━━━━━━━━━━━━━━━━━━━━\nChoose an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_keyboard(),
        )

# ─────────────────────────────────────────────
# ADMIN CONVERSATION STEPS
# ─────────────────────────────────────────────

async def admin_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Send broadcast message to all users."""
    text  = update.message.text.strip()
    users = get_all_users()
    sent  = failed = 0

    for u in users:
        try:
            await ctx.bot.send_message(u["user_id"], text)
            sent += 1
        except TelegramError:
            failed += 1

    await update.message.reply_text(
        f"📨 *Broadcast complete*\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def admin_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Ban a user by ID."""
    try:
        uid = int(update.message.text.strip())
        ban_user(uid)
        await update.message.reply_text(f"🚫 User `{uid}` has been banned.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
    return ConversationHandler.END


async def admin_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Unban a user by ID."""
    try:
        uid = int(update.message.text.strip())
        unban_user(uid)
        await update.message.reply_text(f"✅ User `{uid}` has been unbanned.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
    return ConversationHandler.END


async def admin_delete_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete a request by ID."""
    try:
        rid     = int(update.message.text.strip())
        deleted = delete_request(rid)
        if deleted:
            await update.message.reply_text(f"🗑 Request `#{rid}` deleted.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"⚠️ Request `#{rid}` not found.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Invalid request ID.")
    return ConversationHandler.END

# ─────────────────────────────────────────────
# GLOBAL ERROR HANDLER
# ─────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update:", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An unexpected error occurred. Please try again later."
        )

# ─────────────────────────────────────────────
# UNKNOWN MESSAGES
# ─────────────────────────────────────────────

async def unknown_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❓ Unknown command. Use the menu buttons below.",
        reply_markup=MAIN_KEYBOARD,
    )

# ─────────────────────────────────────────────
# BUILD APPLICATION
# ─────────────────────────────────────────────

def build_app() -> Application:
    """Assemble all handlers and return the Application."""
    app = Application.builder().token(BOT_TOKEN).build()

    # ── /start ──────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))

    # ── Main request flow ───────────────────
    request_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🚀 Start$"), flow_start)],
        states={
            STEP_USERNAMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, flow_receive_usernames)],
            STEP_NOTE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, flow_receive_note)],
        },
        fallbacks=[CommandHandler("cancel", flow_cancel)],
        allow_reentry=True,
    )
    app.add_handler(request_conv)

    # ── Support flow ─────────────────────────
    support_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📩 Support$"), support_start)],
        states={
            SUPPORT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, support_receive)],
        },
        fallbacks=[CommandHandler("cancel", flow_cancel)],
        allow_reentry=True,
    )
    app.add_handler(support_conv)

    # ── Admin panel — ConversationHandler ────
    # The inline buttons can transition into conversation states
    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(admin_callback, pattern="^adm:"),
        ],
        states={
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast)],
            BAN_USER_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban)],
            UNBAN_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban)],
            DELETE_REQ_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_delete_request)],
            # Keep routing inline buttons while inside admin panel
            ConversationHandler.WAITING: [CallbackQueryHandler(admin_callback, pattern="^adm:")],
        },
        fallbacks=[CommandHandler("cancel", flow_cancel)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(admin_conv)

    # ── Menu buttons (profile / about) ───────
    app.add_handler(MessageHandler(filters.Regex("^👤 Profile$"), show_profile))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ About$"),   show_about))

    # ── Welcome new group members ─────────────
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # ── Global error handler ──────────────────
    app.add_error_handler(error_handler)

    # ── Catch-all ────────────────────────────
    app.add_handler(MessageHandler(filters.ALL, unknown_handler))

    return app

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ BOT_TOKEN is not set. Add it to your .env file.")
        return

    init_db()
    logger.info("🚀 Instagram Coke Bot is starting…")

    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
