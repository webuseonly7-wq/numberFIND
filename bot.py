import os
import re
import time
import sqlite3
import threading
import requests

from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, redirect, url_for, session, render_template_string

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# ENVIRONMENT
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PHONE_API_KEY = os.getenv("PHONE_API_KEY")

ADMIN_ID = os.getenv("ADMIN_ID", "8243402862")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

FLASK_SECRET = os.getenv("FLASK_SECRET")

PORT = int(os.getenv("PORT", "10000"))

API_URL = "https://phonevalidationapi.com/api/v1/validate"

DB_FILE = "bot.db"


# =========================================================
# BASIC CHECKS
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not PHONE_API_KEY:
    raise RuntimeError("PHONE_API_KEY is missing")

if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is missing")

if not FLASK_SECRET:
    raise RuntimeError("FLASK_SECRET is missing")


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)
app.secret_key = FLASK_SECRET


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_database():

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                valid TEXT,
                confidence TEXT,
                country TEXT,
                carrier TEXT,
                line_type TEXT,
                created_at TEXT
            )
        """)

        conn.commit()
        conn.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_user(user):

    with db_lock:

        conn = get_db()

        conn.execute("""
            INSERT INTO users
            (user_id, username, first_name, last_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            user.last_name or "",
            utc_now()
        ))

        conn.commit()
        conn.close()


def save_lookup(
    user_id,
    phone,
    valid,
    confidence,
    country,
    carrier,
    line_type
):

    with db_lock:

        conn = get_db()

        conn.execute("""
            INSERT INTO lookups
            (
                user_id,
                phone,
                valid,
                confidence,
                country,
                carrier,
                line_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            phone,
            str(valid),
            confidence,
            country,
            carrier,
            line_type,
            utc_now()
        ))

        conn.commit()
        conn.close()


# =========================================================
# PHONE HELPERS
# =========================================================

def clean_phone(value):

    value = value.strip()

    # Keep only digits and leading +
    value = re.sub(r"[^\d+]", "", value)

    if "+" in value[1:]:
        value = value.replace("+", "")

    return value


def validate_phone(phone):

    headers = {
        "Authorization": f"Bearer {PHONE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "phone": phone,
        "level": "basic"
    }

    last_error = None

    for attempt in range(3):

        try:

            response = requests.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=15
            )

            # Success
            if response.status_code == 200:
                return {
                    "ok": True,
                    "data": response.json()
                }

            # Rate limit
            if response.status_code == 429:

                retry_after = int(
                    response.headers.get(
                        "Retry-After",
                        "2"
                    )
                )

                time.sleep(
                    min(retry_after, 10)
                )

                continue

            # Quota exhausted
            if response.status_code == 402:

                try:
                    error_data = response.json()
                except Exception:
                    error_data = {}

                return {
                    "ok": False,
                    "error": "quota",
                    "details": error_data
                }

            # Auth error
            if response.status_code == 401:

                return {
                    "ok": False,
                    "error": "auth"
                }

            # Temporary server error
            if response.status_code in (500, 503):

                time.sleep(2 ** attempt)
                continue

            # Other API error
            try:
                error_data = response.json()
            except Exception:
                error_data = {}

            return {
                "ok": False,
                "error": "api",
                "status": response.status_code,
                "details": error_data
            }

        except requests.RequestException as exc:

            last_error = str(exc)

            time.sleep(
                2 ** attempt
            )

    return {
        "ok": False,
        "error": "connection",
        "details": last_error
    }


# =========================================================
# TELEGRAM TEXT
# =========================================================

WELCOME = """
👋 <b>Welcome to Phone Validator</b>

━━━━━━━━━━━━━━━━━━━━

📱 Send a phone number and I will check:

✅ Valid / Invalid
🌍 Country
📍 Region
📡 Carrier
📞 Line Type
🎯 Confidence
🔥 Disposable / VoIP status
🔢 International format

━━━━━━━━━━━━━━━━━━━━

<b>Example:</b>

<code>+8801712345678</code>

⚠️ Use international format with country code.
"""


HELP_TEXT = """
<b>📖 How to use</b>

Send a phone number in international format.

<b>Example:</b>
<code>+8801712345678</code>

<b>Available information:</b>

• Validity
• Country
• Region
• Carrier
• Line type
• Confidence
• Disposable/VoIP indicator
• E.164 format

<b>Commands:</b>

/start - Start bot
/help - Help
/status - Bot status
"""


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    save_user(user)

    keyboard = [
        ["📱 Check Number"],
        ["ℹ️ Help", "📊 Status"]
    ]

    markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

    await update.message.reply_text(
        WELCOME,
        parse_mode="HTML",
        reply_markup=markup
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="HTML"
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        """
🟢 <b>Bot Status: ONLINE</b>

━━━━━━━━━━━━━━━━━━━━

🤖 Telegram: Connected
☁️ Hosting: Render
📡 API: Connected
🗄️ Database: SQLite
⚡ Mode: Polling

━━━━━━━━━━━━━━━━━━━━
""",
        parse_mode="HTML"
    )


# =========================================================
# NUMBER HANDLER
# =========================================================

async def handle_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user = update.effective_user

    save_user(user)

    text = update.message.text.strip()

    # Keyboard buttons
    if text == "📱 Check Number":

        await update.message.reply_text(
            "📱 <b>Send a phone number</b>\n\n"
            "Example:\n"
            "<code>+8801712345678</code>",
            parse_mode="HTML"
        )

        return

    if text == "ℹ️ Help":

        await help_command(
            update,
            context
        )

        return

    if text == "📊 Status":

        await status_command(
            update,
            context
        )

        return

    phone = clean_phone(text)

    # Basic format validation
    if not phone.startswith("+"):

        await update.message.reply_text(
            "⚠️ <b>Invalid format</b>\n\n"
            "Please include the country code.\n\n"
            "Example:\n"
            "<code>+8801712345678</code>",
            parse_mode="HTML"
        )

        return

    digits = phone[1:]

    if not digits.isdigit() or not (7 <= len(digits) <= 15):

        await update.message.reply_text(
            "❌ <b>Invalid phone number length.</b>",
            parse_mode="HTML"
        )

        return

    checking = await update.message.reply_text(
        "🔍 <b>Checking number...</b>\n\n"
        "Please wait.",
        parse_mode="HTML"
    )

    result = validate_phone(phone)

    if not result["ok"]:

        error_type = result.get("error")

        if error_type == "quota":

            await checking.edit_text(
                "⚠️ <b>API quota exhausted.</b>\n\n"
                "Your monthly PhoneValidation API credits "
                "have been used.",
                parse_mode="HTML"
            )

            return

        if error_type == "auth":

            await checking.edit_text(
                "🔐 <b>API authentication failed.</b>\n\n"
                "Check your PHONE_API_KEY in Render.",
                parse_mode="HTML"
            )

            return

        if error_type == "connection":

            await checking.edit_text(
                "🌐 <b>API connection failed.</b>\n\n"
                "Please try again later.",
                parse_mode="HTML"
            )

            return

        await checking.edit_text(
            "❌ <b>Validation failed.</b>\n\n"
            "Please try again later.",
            parse_mode="HTML"
        )

        return

    data = result["data"]

    # =====================================================
    # ACTUAL PHONEVALIDATION API RESPONSE
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

    formatted = data.get(
        "formatted",
        {}
    ) or {}

    country_data = data.get(
        "country",
        {}
    ) or {}

    region = data.get(
        "region"
    )

    line_type = data.get(
        "line_type",
        "unknown"
    )

    carrier = data.get(
        "carrier"
    )

    disposable = data.get(
        "is_disposable",
        False
    )

    possible = data.get(
        "is_possible",
        False
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

    iso2 = country_data.get(
        "iso2",
        "Unknown"
    )

    country_code = country_data.get(
        "code",
        "Unknown"
    )

    # =====================================================
    # DISPLAY VALUES
    # =====================================================

    if valid:

        status = "🟢 VALID"

    else:

        status = "🔴 INVALID"

    confidence_map = {
        "verified": "🟢 Verified",
        "likely": "🟡 Likely",
        "uncertain": "🟠 Uncertain",
        "low": "🔴 Low",
        "invalid": "🔴 Invalid"
    }

    confidence_display = confidence_map.get(
        str(confidence).lower(),
        str(confidence)
    )

    disposable_display = (
        "⚠️ Yes"
        if disposable
        else "✅ No"
    )

    carrier_display = (
        carrier
        if carrier
        else "Unknown"
    )

    region_display = (
        region
        if region
        else "Unknown"
    )

    score_display = (
        f"{float(score) * 100:.0f}%"
        if isinstance(score, (int, float))
        else "Unknown"
    )

    # =====================================================
    # SAVE LOOKUP
    # =====================================================

    save_lookup(
        user.id,
        phone,
        valid,
        str(confidence),
        iso2,
        carrier_display,
        line_type
    )

    # =====================================================
    # TELEGRAM RESULT
    # =====================================================

    result_message = f"""
📱 <b>PHONE VALIDATION RESULT</b>

━━━━━━━━━━━━━━━━━━━━

📞 <b>Input</b>
<code>{phone}</code>

📌 <b>Status:</b>
{status}

🎯 <b>Confidence:</b>
{confidence_display}

📊 <b>Score:</b>
{score_display}

📝 <b>Reason:</b>
<code>{reason}</code>

━━━━━━━━━━━━━━━━━━━━

🌍 <b>Country:</b>
{iso2}

☎️ <b>Dial Code:</b>
+{country_code}

📍 <b>Region:</b>
{region_display}

📡 <b>Carrier:</b>
{carrier_display}

📲 <b>Line Type:</b>
{line_type}

━━━━━━━━━━━━━━━━━━━━

🔥 <b>Disposable / VoIP:</b>
{disposable_display}

🔎 <b>Possible:</b>
{"✅ Yes" if possible else "❌ No"}

━━━━━━━━━━━━━━━━━━━━

🔢 <b>E.164:</b>
<code>{e164}</code>

🏠 <b>National:</b>
<code>{national}</code>

🌐 <b>International:</b>
<code>{international}</code>

━━━━━━━━━━━━━━━━━━━━

⚡ <i>Phone Validator Bot</i>
"""

    await checking.edit_text(
        result_message,
        parse_mode="HTML"
    )


# =========================================================
# FLASK ADMIN AUTH
# =========================================================

def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("admin_logged_in"):

            return redirect(
                url_for("admin_login")
            )

        return function(*args, **kwargs)

    return wrapper


# =========================================================
# ADMIN HTML
# =========================================================

ADMIN_HTML = """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Phone Validator Admin</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        Inter,
        Arial,
        sans-serif;

    background:
        radial-gradient(
            circle at top left,
            #25204b,
            #08080f 55%
        );

    color: white;
    min-height: 100vh;
}

.container {
    width: min(1150px, 94%);
    margin: auto;
    padding: 30px 0;
}

.card {
    background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.10);
    border-radius: 22px;
    padding: 24px;
    margin-bottom: 20px;
    backdrop-filter: blur(18px);
    box-shadow:
        0 20px 50px rgba(0,0,0,.25);
}

.login {
    max-width: 420px;
    margin: 100px auto;
}

h1, h2 {
    margin-top: 0;
}

input {
    width: 100%;
    padding: 14px;
    margin: 8px 0;
    border-radius: 12px;
    border: 1px solid #333;
    background: #11111b;
    color: white;
    outline: none;
}

button {
    width: 100%;
    padding: 14px;
    border: 0;
    border-radius: 12px;
    background: #7057ff;
    color: white;
    font-weight: 700;
    cursor: pointer;
}

button:hover {
    opacity: .9;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(210px, 1fr));

    gap: 15px;
    margin-bottom: 20px;
}

.stat {
    background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 18px;
    padding: 22px;
}

.stat-number {
    font-size: 30px;
    font-weight: 800;
    margin-top: 8px;
}

.muted {
    color: #999;
}

.error {
    color: #ff7675;
}

.success {
    color: #55efc4;
}

.logout {
    display: inline-block;
    color: #ff7675;
    text-decoration: none;
    margin-top: 10px;
}

.table-wrap {
    overflow-x: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th,
td {
    padding: 12px;
    border-bottom:
        1px solid rgba(255,255,255,.08);

    text-align: left;
    white-space: nowrap;
}

th {
    color: #aaa;
    font-size: 13px;
}

.badge {
    display: inline-block;
    padding: 5px 9px;
    border-radius: 999px;
    background: rgba(255,255,255,.08);
}

@media(max-width:600px) {

    .container {
        padding: 15px 0;
    }

    .card {
        padding: 18px;
    }

}

</style>

</head>

<body>

<div class="container">

{% if login_page %}

<div class="card login">

<h1>🔐 Admin Login</h1>

<p class="muted">
Phone Validator Bot
</p>

{% if error %}

<p class="error">
{{ error }}
</p>

{% endif %}

<form method="POST">

<input
    name="admin_id"
    placeholder="Admin ID"
    autocomplete="username"
    required
>

<input
    type="password"
    name="password"
    placeholder="Admin Password"
    autocomplete="current-password"
    required
>

<button>
Login
</button>

</form>

</div>

{% else %}

<div class="card">

<h1>📊 Admin Dashboard</h1>

<p class="muted">
Phone Validator Bot Control Panel
</p>

<a class="logout" href="/admin/logout">
Logout →
</a>

</div>


<div class="stats">

<div class="stat">

<div class="muted">
👥 Total Users
</div>

<div class="stat-number">
{{ users_count }}
</div>

</div>


<div class="stat">

<div class="muted">
🔎 Total Lookups
</div>

<div class="stat-number">
{{ lookup_count }}
</div>

</div>


<div class="stat">

<div class="muted">
🟢 Valid Numbers
</div>

<div class="stat-number">
{{ valid_count }}
</div>

</div>


<div class="stat">

<div class="muted">
🔴 Invalid Numbers
</div>

<div class="stat-number">
{{ invalid_count }}
</div>

</div>

</div>


<div class="card">

<h2>👥 Recent Users</h2>

<div class="table-wrap">

<table>

<tr>

<th>ID</th>
<th>Username</th>
<th>Name</th>
<th>Joined</th>

</tr>

{% for u in users %}

<tr>

<td>
{{ u["user_id"] }}
</td>

<td>
{% if u["username"] %}
@{{ u["username"] }}
{% else %}
-
{% endif %}
</td>

<td>
{{ u["first_name"] }}
{{ u["last_name"] }}
</td>

<td>
{{ u["joined_at"][:19] }}
</td>

</tr>

{% endfor %}

</table>

</div>

</div>


<div class="card">

<h2>🔎 Recent Lookups</h2>

<div class="table-wrap">

<table>

<tr>

<th>User</th>
<th>Phone</th>
<th>Status</th>
<th>Confidence</th>
<th>Country</th>
<th>Carrier</th>
<th>Line</th>
<th>Time</th>

</tr>

{% for l in lookups %}

<tr>

<td>
{{ l["user_id"] }}
</td>

<td>
{{ l["phone"] }}
</td>

<td>
{{ l["valid"] }}
</td>

<td>
{{ l["confidence"] }}
</td>

<td>
{{ l["country"] }}
</td>

<td>
{{ l["carrier"] }}
</td>

<td>
{{ l["line_type"] }}
</td>

<td>
{{ l["created_at"][:19] }}
</td>

</tr>

{% endfor %}

</table>

</div>

</div>

{% endif %}

</div>

</body>

</html>
"""


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.route(
    "/admin/login",
    methods=["GET", "POST"]
)
def admin_login():

    if request.method == "POST":

        admin_id = request.form.get(
            "admin_id",
            ""
        )

        password = request.form.get(
            "password",
            ""
        )

        if (
            admin_id == ADMIN_ID
            and password == ADMIN_PASSWORD
        ):

            session["admin_logged_in"] = True

            return redirect(
                url_for("admin_dashboard")
            )

        return render_template_string(
            ADMIN_HTML,
            login_page=True,
            error="Invalid admin credentials."
        )

    return render_template_string(
        ADMIN_HTML,
        login_page=True
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
@admin_required
def admin_dashboard():

    conn = get_db()

    users_count = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    lookup_count = conn.execute(
        "SELECT COUNT(*) FROM lookups"
    ).fetchone()[0]

    valid_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM lookups
        WHERE valid = 'True'
        """
    ).fetchone()[0]

    invalid_count = lookup_count - valid_count

    users = conn.execute(
        """
        SELECT *
        FROM users
        ORDER BY joined_at DESC
        LIMIT 50
        """
    ).fetchall()

    lookups = conn.execute(
        """
        SELECT *
        FROM lookups
        ORDER BY created_at DESC
        LIMIT 50
        """
    ).fetchall()

    conn.close()

    return render_template_string(
        ADMIN_HTML,
        login_page=False,
        users_count=users_count,
        lookup_count=lookup_count,
        valid_count=valid_count,
        invalid_count=invalid_count,
        users=users,
        lookups=lookups
    )


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.route("/admin/logout")
def admin_logout():

    session.clear()

    return redirect(
        url_for("admin_login")
    )


# =========================================================
# RENDER HEALTH CHECK
# =========================================================

@app.route("/")
def home():

    return """
    <html>
    <head>
        <title>Phone Validator Bot</title>
    </head>
    <body>
        <h2>🟢 Phone Validator Bot</h2>
        <p>Service is running.</p>
    </body>
    </html>
    """


@app.route("/health")
def health():

    return "OK", 200


# =========================================================
# FLASK SERVER
# =========================================================

def run_flask():

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )


# =========================================================
# TELEGRAM BOT
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
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_number
        )
    )

    print("Telegram bot started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    init_database()

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    run_bot()
