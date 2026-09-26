import os
import re
import sqlite3
import asyncio
import threading
import requests

from datetime import datetime, timezone

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PHONE_API_KEY = os.getenv("PHONE_API_KEY")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8243402862"))

PORT = int(os.getenv("PORT", "10000"))

API_URL = "https://phonevalidationapi.com/api/v1/validate"

DB_FILE = "bot.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not PHONE_API_KEY:
    raise RuntimeError("PHONE_API_KEY is missing")


# =========================================================
# FLASK HEALTH SERVER
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "🟢 Telegram Phone Validator Bot is running."


@app.route("/health")
def health():
    return "OK", 200


def run_web():
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def database():

    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_database():

    with db_lock:

        conn = database()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                valid TEXT,
                country TEXT,
                carrier TEXT,
                line_type TEXT,
                confidence TEXT,
                created_at TEXT
            )
        """)

        conn.commit()
        conn.close()


def now():

    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


def save_user(user):

    with db_lock:

        conn = database()

        conn.execute("""
            INSERT INTO users
            (
                user_id,
                username,
                first_name,
                last_name,
                joined_at
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            user.last_name or "",
            now()
        ))

        conn.commit()
        conn.close()


def save_lookup(
    user_id,
    phone,
    valid,
    country,
    carrier,
    line_type,
    confidence
):

    with db_lock:

        conn = database()

        conn.execute("""
            INSERT INTO lookups
            (
                user_id,
                phone,
                valid,
                country,
                carrier,
                line_type,
                confidence,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            phone,
            str(valid),
            country,
            carrier,
            line_type,
            confidence,
            now()
        ))

        conn.commit()
        conn.close()


# =========================================================
# API
# =========================================================

def clean_phone(phone):

    phone = phone.strip()

    phone = re.sub(
        r"[^\d+]",
        "",
        phone
    )

    if "+" in phone[1:]:

        phone = "+" + phone.replace(
            "+",
            ""
        )

    return phone


def call_phone_api(phone):

    headers = {
        "Authorization":
            f"Bearer {PHONE_API_KEY}",

        "Content-Type":
            "application/json",

        "Accept":
            "application/json"
    }

    payload = {
        "phone": phone
    }

    try:

        response = requests.post(
            API_URL,
            headers=headers,
            json=payload,
            timeout=15
        )

        if response.status_code == 200:

            return {
                "success": True,
                "data": response.json()
            }

        if response.status_code == 401:

            return {
                "success": False,
                "error": "auth"
            }

        if response.status_code == 402:

            return {
                "success": False,
                "error": "quota"
            }

        if response.status_code == 429:

            return {
                "success": False,
                "error": "rate"
            }

        return {
            "success": False,
            "error": "api"
        }

    except requests.RequestException:

        return {
            "success": False,
            "error": "connection"
        }


# =========================================================
# KEYBOARDS
# =========================================================

def user_panel():

    keyboard = [

        [
            InlineKeyboardButton(
                "📱 Check Number",
                callback_data="check"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 My Statistics",
                callback_data="mystats"
            ),

            InlineKeyboardButton(
                "🕘 My History",
                callback_data="history"
            )
        ],

        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help"
            )
        ]

    ]

    return InlineKeyboardMarkup(keyboard)


def admin_panel():

    keyboard = [

        [
            InlineKeyboardButton(
                "📊 Dashboard",
                callback_data="admin_dashboard"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 Users",
                callback_data="admin_users"
            ),

            InlineKeyboardButton(
                "🔎 Lookups",
                callback_data="admin_lookups"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="admin_broadcast"
            )
        ],

        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="admin_dashboard"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 User Panel",
                callback_data="user_panel"
            )
        ]

    ]

    return InlineKeyboardMarkup(keyboard)


def back_button():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 Back",
                callback_data="user_panel"
            )
        ]
    ])


def admin_back():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 Admin Panel",
                callback_data="admin_panel"
            )
        ]
    ])


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    save_user(user)

    await update.message.reply_text(

        """
👋 <b>Welcome to Phone Validator</b>

━━━━━━━━━━━━━━━━━━━━

📱 <b>Phone Validation & Lookup</b>

Check a phone number's:

✅ Validity
🌍 Country
📡 Carrier
📞 Line Type
🎯 Confidence
🔢 International format

━━━━━━━━━━━━━━━━━━━━

Choose an option below.
""",

        parse_mode="HTML",

        reply_markup=user_panel()
    )


# =========================================================
# ADMIN COMMAND
# =========================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    await update.message.reply_text(
        "👑 <b>Admin Panel</b>",
        parse_mode="HTML",
        reply_markup=admin_panel()
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    save_user(user)

    data = query.data

    # =====================================================
    # USER PANEL
    # =====================================================

    if data == "user_panel":

        await query.edit_message_text(
            "🏠 <b>User Panel</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=user_panel()
        )

        return

    # =====================================================
    # CHECK NUMBER
    # =====================================================

    if data == "check":

        context.user_data["waiting_number"] = True

        await query.edit_message_text(

            """
📱 <b>Check Phone Number</b>

Send the number in international format.

Example:

<code>+8801712345678</code>

⚠️ Include the country code.
""",

            parse_mode="HTML",

            reply_markup=back_button()
        )

        return

    # =====================================================
    # HELP
    # =====================================================

    if data == "help":

        await query.edit_message_text(

            """
ℹ️ <b>How to use</b>

1️⃣ Press <b>Check Number</b>
2️⃣ Send a number
3️⃣ Wait for API validation
4️⃣ Get the result

Example:

<code>+8801712345678</code>

The bot provides phone metadata such as country,
carrier and line type.
""",

            parse_mode="HTML",

            reply_markup=back_button()
        )

        return

    # =====================================================
    # MY STATS
    # =====================================================

    if data == "mystats":

        conn = database()

        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM lookups
            WHERE user_id=?
            """,
            (user.id,)
        ).fetchone()[0]

        valid = conn.execute(
            """
            SELECT COUNT(*)
            FROM lookups
            WHERE user_id=?
            AND valid='True'
            """,
            (user.id,)
        ).fetchone()[0]

        invalid = total - valid

        conn.close()

        text = f"""
📊 <b>My Statistics</b>

━━━━━━━━━━━━━━━━

🔎 Total Lookups: <b>{total}</b>

🟢 Valid: <b>{valid}</b>

🔴 Invalid: <b>{invalid}</b>

━━━━━━━━━━━━━━━━
"""

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_button()
        )

        return

    # =====================================================
    # HISTORY
    # =====================================================

    if data == "history":

        conn = database()

        rows = conn.execute(
            """
            SELECT *
            FROM lookups
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (user.id,)
        ).fetchall()

        conn.close()

        if not rows:

            text = (
                "🕘 <b>My History</b>\n\n"
                "No lookup history yet."
            )

        else:

            lines = [
                "🕘 <b>My History</b>\n"
            ]

            for row in rows:

                phone = row["phone"]

                # Mask phone
                if len(phone) > 7:

                    masked = (
                        phone[:4]
                        + "••••"
                        + phone[-3:]
                    )

                else:

                    masked = "••••••"

                status = (
                    "🟢"
                    if row["valid"] == "True"
                    else "🔴"
                )

                lines.append(
                    f"{status} <code>{masked}</code> "
                    f"• {row['country'] or 'Unknown'}"
                )

            text = "\n".join(lines)

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_button()
        )

        return

    # =====================================================
    # ADMIN SECURITY
    # =====================================================

    if data.startswith("admin_"):

        if user.id != ADMIN_ID:

            await query.answer(
                "⛔ Admin only.",
                show_alert=True
            )

            return

    # =====================================================
    # ADMIN PANEL
    # =====================================================

    if data == "admin_panel":

        await query.edit_message_text(
            "👑 <b>Admin Panel</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=admin_panel()
        )

        return

    # =====================================================
    # ADMIN DASHBOARD
    # =====================================================

    if data == "admin_dashboard":

        conn = database()

        users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        lookups = conn.execute(
            "SELECT COUNT(*) FROM lookups"
        ).fetchone()[0]

        valid = conn.execute(
            """
            SELECT COUNT(*)
            FROM lookups
            WHERE valid='True'
            """
        ).fetchone()[0]

        invalid = lookups - valid

        conn.close()

        text = f"""
👑 <b>ADMIN DASHBOARD</b>

━━━━━━━━━━━━━━━━━━━━

👥 Total Users:
<b>{users}</b>

🔎 Total Lookups:
<b>{lookups}</b>

🟢 Valid:
<b>{valid}</b>

🔴 Invalid:
<b>{invalid}</b>

📡 Bot:
🟢 ONLINE

━━━━━━━━━━━━━━━━━━━━
"""

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_back()
        )

        return

    # =====================================================
    # ADMIN USERS
    # =====================================================

    if data == "admin_users":

        conn = database()

        rows = conn.execute(
            """
            SELECT *
            FROM users
            ORDER BY joined_at DESC
            LIMIT 15
            """
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        conn.close()

        lines = [
            f"👥 <b>Users: {total}</b>\n"
        ]

        for row in rows:

            name = (
                row["first_name"]
                or "Unknown"
            )

            username = (
                f"@{row['username']}"
                if row["username"]
                else "No username"
            )

            lines.append(
                f"• <code>{row['user_id']}</code> "
                f"{name} — {username}"
            )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=admin_back()
        )

        return

    # =====================================================
    # ADMIN LOOKUPS
    # =====================================================

    if data == "admin_lookups":

        conn = database()

        rows = conn.execute(
            """
            SELECT *
            FROM lookups
            ORDER BY id DESC
            LIMIT 15
            """
        ).fetchall()

        conn.close()

        if not rows:

            text = (
                "🔎 <b>Recent Lookups</b>\n\n"
                "No lookups yet."
            )

        else:

            lines = [
                "🔎 <b>Recent Lookups</b>\n"
            ]

            for row in rows:

                phone = row["phone"]

                if len(phone) > 7:

                    phone = (
                        phone[:4]
                        + "••••"
                        + phone[-3:]
                    )

                status = (
                    "🟢"
                    if row["valid"] == "True"
                    else "🔴"
                )

                lines.append(
                    f"{status} "
                    f"<code>{phone}</code> "
                    f"• {row['country'] or 'Unknown'}"
                )

            text = "\n".join(lines)

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_back()
        )

        return

    # =====================================================
    # BROADCAST
    # =====================================================

    if data == "admin_broadcast":

        context.user_data[
            "waiting_broadcast"
        ] = True

        await query.edit_message_text(

            """
📢 <b>Broadcast</b>

Send the message you want to broadcast.

⚠️ It will be sent to registered bot users.
""",

            parse_mode="HTML",

            reply_markup=admin_back()
        )

        return


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    save_user(user)

    text = update.message.text.strip()

    # =====================================================
    # ADMIN BROADCAST
    # =====================================================

    if context.user_data.get(
        "waiting_broadcast"
    ):

        if user.id != ADMIN_ID:

            context.user_data[
                "waiting_broadcast"
            ] = False

            return

        context.user_data[
            "waiting_broadcast"
        ] = False

        conn = database()

        users = conn.execute(
            "SELECT user_id FROM users"
        ).fetchall()

        conn.close()

        sent = 0
        failed = 0

        for row in users:

            try:

                await context.bot.send_message(
                    chat_id=row["user_id"],
                    text=text,
                    parse_mode="HTML"
                )

                sent += 1

                await asyncio.sleep(
                    0.05
                )

            except Exception:

                failed += 1

        await update.message.reply_text(

            f"""
📢 <b>Broadcast Finished</b>

━━━━━━━━━━━━━━━━

🟢 Sent: <b>{sent}</b>
🔴 Failed: <b>{failed}</b>
""",

            parse_mode="HTML",

            reply_markup=admin_panel()
        )

        return

    # =====================================================
    # PHONE NUMBER
    # =====================================================

    if not context.user_data.get(
        "waiting_number"
    ):

        await update.message.reply_text(
            "Please use the buttons below.",
            reply_markup=user_panel()
        )

        return

    context.user_data[
        "waiting_number"
    ] = False

    phone = clean_phone(text)

    if not phone.startswith("+"):

        await update.message.reply_text(

            """
❌ <b>Invalid format</b>

Use international format:

<code>+8801712345678</code>
""",

            parse_mode="HTML",

            reply_markup=user_panel()
        )

        return

    digits = phone[1:]

    if (
        not digits.isdigit()
        or len(digits) < 7
        or len(digits) > 15
    ):

        await update.message.reply_text(
            "❌ Invalid phone number.",
            reply_markup=user_panel()
        )

        return

    checking = await update.message.reply_text(
        "🔍 <b>Checking...</b>",
        parse_mode="HTML"
    )

    result = await asyncio.to_thread(
        call_phone_api,
        phone
    )

    if not result["success"]:

        error = result["error"]

        if error == "quota":

            message = (
                "⚠️ <b>API quota exhausted.</b>\n\n"
                "Please try again after your API "
                "quota resets."
            )

        elif error == "auth":

            message = (
                "🔐 <b>API authentication failed.</b>"
            )

        elif error == "rate":

            message = (
                "⏳ <b>Too many requests.</b>\n\n"
                "Please wait and try again."
            )

        else:

            message = (
                "❌ <b>API temporarily unavailable.</b>"
            )

        await checking.edit_text(
            message,
            parse_mode="HTML",
            reply_markup=user_panel()
        )

        return

    data = result["data"]

    # =====================================================
    # PARSE API RESPONSE
    # =====================================================

    valid = data.get(
        "valid",
        False
    )

    confidence = data.get(
        "confidence",
        "unknown"
    )

    score = data.get(
        "score"
    )

    reason = data.get(
        "reason",
        "unknown"
    )

    country = data.get(
        "country",
        {}
    ) or {}

    formatted = data.get(
        "formatted",
        {}
    ) or {}

    region = data.get(
        "region",
        "Unknown"
    )

    carrier = data.get(
        "carrier",
        "Unknown"
    )

    line_type = data.get(
        "line_type",
        "Unknown"
    )

    disposable = data.get(
        "is_disposable",
        False
    )

    possible = data.get(
        "is_possible",
        False
    )

    iso2 = country.get(
        "iso2",
        "Unknown"
    )

    country_name = country.get(
        "name",
        "Unknown"
    )

    dial_code = country.get(
        "code",
        "Unknown"
    )

    e164 = formatted.get(
        "e164",
        phone
    )

    national = formatted.get(
        "national",
        "Unknown"
    )

    international = formatted.get(
        "international",
        "Unknown"
    )

    status = (
        "🟢 VALID"
        if valid
        else "🔴 INVALID"
    )

    if score is not None:

        try:

            score_text = (
                f"{float(score) * 100:.0f}%"
            )

        except Exception:

            score_text = str(score)

    else:

        score_text = "Unknown"

    disposable_text = (
        "⚠️ Yes"
        if disposable
        else "✅ No"
    )

    possible_text = (
        "✅ Yes"
        if possible
        else "❌ No"
    )

    # =====================================================
    # SAVE
    # =====================================================

    save_lookup(
        user.id,
        phone,
        valid,
        iso2,
        carrier,
        line_type,
        str(confidence)
    )

    # =====================================================
    # RESULT
    # =====================================================

    output = f"""
📱 <b>PHONE RESULT</b>

━━━━━━━━━━━━━━━━━━━━

📞 <b>Number</b>
<code>{phone}</code>

📌 <b>Status:</b>
{status}

🎯 <b>Confidence:</b>
{confidence}

📊 <b>Score:</b>
{score_text}

📝 <b>Reason:</b>
{reason}

━━━━━━━━━━━━━━━━━━━━

🌍 <b>Country:</b>
{country_name}

🔤 <b>ISO:</b>
{iso2}

☎️ <b>Dial Code:</b>
+{dial_code}

📍 <b>Region:</b>
{region}

📡 <b>Carrier:</b>
{carrier}

📲 <b>Line Type:</b>
{line_type}

🔥 <b>Disposable:</b>
{disposable_text}

🔎 <b>Possible:</b>
{possible_text}

━━━━━━━━━━━━━━━━━━━━

🔢 <b>E.164:</b>
<code>{e164}</code>

🏠 <b>National:</b>
<code>{national}</code>

🌐 <b>International:</b>
<code>{international}</code>

━━━━━━━━━━━━━━━━━━━━
"""

    await checking.edit_text(
        output,
        parse_mode="HTML",
        reply_markup=user_panel()
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    print(
        "Telegram error:",
        context.error
    )


# =========================================================
# RUN BOT
# =========================================================

def run_bot():

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "🤖 Bot started successfully."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    init_database()

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    run_bot()
