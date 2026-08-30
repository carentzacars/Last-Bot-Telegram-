import os
import re
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
ACCOUNTS_GROUP_ID = int(os.environ["ACCOUNTS_GROUP_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

EXPENSES_GROUP_NAME = "cars expenses"
KL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

# ============================================================
# DATABASE
# ============================================================

conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = False


def db_execute(sql, params=(), fetchone=False, fetchall=False):
    """Execute a query safely and commit when appropriate."""
    global conn

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            result = cur.fetchone() if fetchone else cur.fetchall() if fetchall else None
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def init_database():
    with conn.cursor() as cur:
        # Configuration for the one designated expenses group.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS expense_group_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                group_id TEXT NOT NULL,
                group_name TEXT NOT NULL
            )
        """)

        # Every accounting Telegram message is stored separately.
        # message_id is unique per Telegram chat, so (group_id, message_id)
        # is the natural key for edit detection and duplicate protection.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id BIGSERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                group_name TEXT NOT NULL,
                telegram_message_id BIGINT NOT NULL,
                transaction_type TEXT NOT NULL
                    CHECK (transaction_type IN ('income', 'refund', 'expense')),
                amount INTEGER NOT NULL,
                description TEXT,
                month_key TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (group_id, telegram_message_id)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_current
            ON transactions (month_key, is_active, group_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_message
            ON transactions (group_id, telegram_message_id)
        """)

        # Keeps a record that the monthly reset has already happened.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reset_log (
                id BIGSERIAL PRIMARY KEY,
                reset_month TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

    conn.commit()


init_database()

# ============================================================
# HELPERS
# ============================================================


def current_month_key():
    return datetime.now(KL_TZ).strftime("%Y-%m")


def current_month_label():
    return datetime.now(KL_TZ).strftime("%B %Y")


def normalize_group_name(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def is_expenses_group(group_name):
    return normalize_group_name(group_name) == normalize_group_name(
        EXPENSES_GROUP_NAME
    )


def is_configured_expenses_group(group_id, group_name):
    row = db_execute(
        "SELECT group_id FROM expense_group_config WHERE id = 1",
        fetchone=True,
    )
    configured_id = row["group_id"] if row else None
    return (configured_id is not None and configured_id == group_id) or is_expenses_group(
        group_name
    )


def extract_price(message_text):
    match = re.search(
        r"(?im)^\s*price\s*:\s*(?:rm\s*)?(\d+)\s*(?:rm|cash|online)?\b",
        message_text,
    )
    return int(match.group(1)) if match else None


def extract_refund(message_text):
    match = re.search(
        r"(?im)^\s*refund\s*:\s*(?:rm\s*)?(\d+)\s*(?:rm|cash|online)?\b",
        message_text,
    )
    return int(match.group(1)) if match else None


def extract_expense(message_text):
    # Keep the existing rule: messages mentioning advance/advanced
    # are not treated as expenses.
    if re.search(r"(?i)\badvance(?:d)?\b", message_text):
        return None

    matches = re.finditer(
        r"(?i)(?:\brm\s*(\d+)\b|(\d+)\s*rm\b|\bamount\s*:\s*(?:rm\s*)?(\d+)\b)",
        message_text,
    )

    amounts = []
    for match in matches:
        value = match.group(1) or match.group(2) or match.group(3)
        if value:
            amounts.append(int(value))

    return sum(amounts) if amounts else None


def extract_expense_name(message_text):
    """
    Produces a readable expense label for the Accounts message.

    Examples:
        'Car Wash RM 20'       -> 'Car Wash'
        'RM 20 petrol'         -> 'petrol'
        'Brake change 50 RM'   -> 'Brake change'
    """
    lines = [line.strip() for line in message_text.splitlines() if line.strip()]

    if not lines:
        return "Expense"

    # Prefer the first line because Telegram expense entries commonly
    # put the description before/alongside the amount.
    name = lines[0]

    name = re.sub(
        r"(?i)\brm\s*\d+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s*rm\b",
        "",
        name,
    )
    name = re.sub(
        r"(?i)\bamount\s*:\s*(?:rm\s*)?\d+(?:\.\d+)?\b",
        "",
        name,
    )
    name = re.sub(r"(?i)\b(?:cash|online)\b", "", name)
    name = re.sub(r"(?i)\bamount\b", "", name)
    name = re.sub(r"\s+", " ", name).strip(" :-–—")

    return name if name else "Expense"


def get_transaction(group_id, message_id):
    return db_execute(
        """
        SELECT *
        FROM transactions
        WHERE group_id = %s AND telegram_message_id = %s
        """,
        (group_id, message_id),
        fetchone=True,
    )


def get_income_groups():
    return db_execute(
        """
        SELECT
            group_id,
            MAX(group_name) AS group_name,
            COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE month_key = %s
          AND is_active = TRUE
          AND transaction_type IN ('income', 'refund')
          AND group_id != %s
          AND NOT EXISTS (
              SELECT 1
              FROM expense_group_config egc
              WHERE egc.id = 1 AND egc.group_id = transactions.group_id
          )
          AND LOWER(group_name) != LOWER(%s)
        GROUP BY group_id
        ORDER BY group_name
        """,
        (current_month_key(), str(ACCOUNTS_GROUP_ID), EXPENSES_GROUP_NAME),
        fetchall=True,
    )


def get_expense_groups():
    return db_execute(
        """
        SELECT
            group_id,
            MAX(group_name) AS group_name,
            COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE month_key = %s
          AND is_active = TRUE
          AND transaction_type = 'expense'
          AND group_id != %s
        GROUP BY group_id
        ORDER BY group_name
        """,
        (current_month_key(), str(ACCOUNTS_GROUP_ID)),
        fetchall=True,
    )


def get_totals():
    income_groups = get_income_groups()
    expense_groups = get_expense_groups()

    total_income = sum(int(row["total"]) for row in income_groups)
    total_expenses = sum(int(row["total"]) for row in expense_groups)

    return income_groups, expense_groups, total_income, total_expenses


def build_accounts_message(action_line):
    income_groups, expense_groups, total_income, total_expenses = get_totals()

    lines = [action_line, "", "Cars Income:"]

    for row in income_groups:
        lines.append(f'{row["group_name"]} : {int(row["total"])}')

    lines.extend(
        [
            "",
            f"Total Income: {total_income}",
            f"Total Expenses: {total_expenses}",
            f"Net Total : {total_income - total_expenses}",
        ]
    )

    return "\n".join(lines)


# ============================================================
# TRANSACTION WRITES
# ============================================================


def insert_transaction(
    group_id,
    group_name,
    message_id,
    transaction_type,
    amount,
    description,
):
    month_key = current_month_key()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (
                group_id,
                group_name,
                telegram_message_id,
                transaction_type,
                amount,
                description,
                month_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (group_id, telegram_message_id) DO NOTHING
            RETURNING id
            """,
            (
                group_id,
                group_name,
                message_id,
                transaction_type,
                int(amount),
                description,
                month_key,
            ),
        )
        row = cur.fetchone()

    conn.commit()
    return row[0] if row else None


def update_transaction(
    transaction_id,
    transaction_type,
    amount,
    description,
    group_name,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET transaction_type = %s,
                amount = %s,
                description = %s,
                group_name = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (
                transaction_type,
                int(amount),
                description,
                group_name,
                transaction_id,
            ),
        )
        row = cur.fetchone()

    conn.commit()
    return row


def deactivate_group_transactions(group_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET is_active = FALSE, updated_at = NOW()
            WHERE group_id = %s
              AND month_key = %s
              AND is_active = TRUE
            """,
            (group_id, current_month_key()),
        )
    conn.commit()


def deactivate_all_current_transactions():
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET is_active = FALSE, updated_at = NOW()
            WHERE month_key = %s AND is_active = TRUE
            """,
            (current_month_key(),),
        )
    conn.commit()


# ============================================================
# MESSAGE PROCESSING
# ============================================================


async def process_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    is_edited=False,
):
    msg = update.edited_message if is_edited else update.message

    if not msg or not msg.text or not update.effective_chat:
        return

    source_group_id = str(update.effective_chat.id)
    source_group_name = update.effective_chat.title or "Unknown Group"
    message_id = msg.message_id

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        return

    expense_group = is_configured_expenses_group(
        source_group_id,
        source_group_name,
    )

    price = extract_price(msg.text)
    refund = extract_refund(msg.text)
    expense = extract_expense(msg.text) if expense_group else None

    # A message is one accounting transaction.
    if expense_group:
        if expense is None or price is not None or refund is not None:
            return

        transaction_type = "expense"
        amount = expense
        description = extract_expense_name(msg.text)
    else:
        if price is not None and refund is not None:
            return

        if price is not None:
            transaction_type = "income"
            amount = price
            description = "Price"
        elif refund is not None:
            transaction_type = "refund"
            amount = -refund
            description = "Refund"
        else:
            return

    # --------------------------------------------------------
    # NEW MESSAGE
    # --------------------------------------------------------
    if not is_edited:
        transaction_id = insert_transaction(
            source_group_id,
            source_group_name,
            message_id,
            transaction_type,
            amount,
            description,
        )

        # Duplicate Telegram delivery: do nothing.
        if transaction_id is None:
            return

        if transaction_type == "income":
            previous = get_group_previous_total(source_group_id, transaction_id)
            action_line = (
                f"{source_group_name} "
                f"(Previous stored income: {previous}) + "
                f"({amount}) = {previous + amount}"
            )
        elif transaction_type == "refund":
            previous = get_group_previous_total(source_group_id, transaction_id)
            refund_value = abs(amount)
            action_line = (
                f"{source_group_name} "
                f"(Previous stored income: {previous}) - "
                f"({refund_value}) = {previous + amount}"
            )
        else:
            previous = get_group_previous_expense(source_group_id, transaction_id)
            action_line = (
                f"Cars Expenses\n"
                f"{description} : {previous} + {amount} = {previous + amount}"
            )

    # --------------------------------------------------------
    # EDITED MESSAGE
    # --------------------------------------------------------
    else:
        existing = get_transaction(source_group_id, message_id)

        # If the bot did not record the original message, don't invent
        # a transaction from the edited version.
        if not existing:
            return

        # Do not let an edit from a previous accounting month alter the
        # current month's figures.
        if existing["month_key"] != current_month_key():
            return

        old_type = existing["transaction_type"]
        old_amount = int(existing["amount"])

        update_transaction(
            existing["id"],
            transaction_type,
            amount,
            description,
            source_group_name,
        )

        if transaction_type == "income":
            old_contribution = old_amount
            adjustment = amount - old_amount
            new_group_total = get_group_current_total(source_group_id)

            action_line = (
                f"{source_group_name} "
                f"(Previous stored income: {new_group_total - adjustment}) + "
                f"(Edit adjustment: {adjustment:+d}) = "
                f"{new_group_total}"
            )

        elif transaction_type == "refund":
            old_contribution = old_amount
            adjustment = amount - old_amount
            new_group_total = get_group_current_total(source_group_id)

            action_line = (
                f"{source_group_name} "
                f"(Previous stored income: {new_group_total - adjustment}) + "
                f"(Edit adjustment: {adjustment:+d}) = "
                f"{new_group_total}"
            )

        else:
            adjustment = amount - old_amount
            new_expense_total = get_group_current_expense(source_group_id)

            action_line = (
                f"Cars Expenses\n"
                f"{description} : "
                f"{new_expense_total - adjustment} + "
                f"{amount} = {new_expense_total}"
            )

    # --------------------------------------------------------
    # ACCOUNTS GROUP MESSAGE
    # --------------------------------------------------------

    if transaction_type == "expense":
        # Keep the exact compact expense style requested.
        message = build_accounts_message(action_line)
    else:
        message = build_accounts_message(action_line)

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=message,
    )


def get_group_current_total(group_id):
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type IN ('income', 'refund')
        """,
        (group_id, current_month_key()),
        fetchone=True,
    )
    return int(row["total"])


def get_group_previous_total(group_id, transaction_id):
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type IN ('income', 'refund')
          AND id != %s
        """,
        (group_id, current_month_key(), transaction_id),
        fetchone=True,
    )
    return int(row["total"])


def get_group_current_expense(group_id):
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type = 'expense'
        """,
        (group_id, current_month_key()),
        fetchone=True,
    )
    return int(row["total"])


def get_group_previous_expense(group_id, transaction_id):
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type = 'expense'
          AND id != %s
        """,
        (group_id, current_month_key(), transaction_id),
        fetchone=True,
    )
    return int(row["total"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_message(update, context, is_edited=False)


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_message(update, context, is_edited=True)


# ============================================================
# /TOTAL
# ============================================================


async def total_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        income_groups, expense_groups, total_income, total_expenses = get_totals()

        if not income_groups and not expense_groups:
            await update.message.reply_text("No group totals recorded yet.")
            return

        lines = ["Cars Income:"]

        for row in income_groups:
            lines.append(f'{row["group_name"]} : {int(row["total"])}')

        lines.extend(
            [
                "",
                f"Total Income: {total_income}",
                f"Total Expenses: {total_expenses}",
                f"Net Total : {total_income - total_expenses}",
            ]
        )

        await update.message.reply_text("\n".join(lines))
        return

    group_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "This group"

    total = get_group_current_total(group_id)
    expenses = get_group_current_expense(group_id)

    await update.message.reply_text(
        f"{group_name} total: {total}\n"
        f"{group_name} expenses: {expenses}\n"
        f"Net total: {total - expenses}"
    )


# ============================================================
# /HELP
# ============================================================


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Here's what I can do:\n\n"
        "Price: 200 — add amount to this group's income\n"
        "Refund: 50 — deduct amount from this group's income\n"
        "Expenses — send a message containing RM 15, RM50, or 50 RM\n\n"
        "Commands:\n"
        "/total — show current totals\n"
        "/reset — reset this group's current month totals (admins only)\n"
        "/setexpenses — register this group as Car Expenses (admin only)\n"
        "/resetall — reset all current month totals (admins only, Accounts group only)\n"
        "/remove — remove this group's current month transactions (admins only)\n\n"
        "Edited Price, Refund, and Expense messages are automatically recalculated."
    )


# ============================================================
# ADMIN CHECK
# ============================================================


async def is_admin(update, context):
    if not update.effective_chat or not update.effective_user:
        return False

    admins = await context.bot.get_chat_administrators(
        update.effective_chat.id
    )
    admin_ids = {admin.user.id for admin in admins}
    return update.effective_user.id in admin_ids


# ============================================================
# /RESET
# ============================================================


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can reset the total."
        )
        return

    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "This group"

    old_income = get_group_current_total(chat_id)
    old_expense = get_group_current_expense(chat_id)

    deactivate_group_transactions(chat_id)

    await update.message.reply_text(
        f"{group_name} reset:\n"
        f"Income: {old_income} → 0\n"
        f"Expenses: {old_expense} → 0"
    )

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=(
            f"{group_name} was manually reset by an admin:\n"
            f"Income: {old_income} → 0\n"
            f"Expenses: {old_expense} → 0"
        ),
    )


# ============================================================
# /SETEXPENSES
# ============================================================


async def setexpenses_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        await update.message.reply_text(
            "Use /setexpenses inside the Cars Expenses group."
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can set the expenses group."
        )
        return

    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "Cars Expenses"

    db_execute(
        """
        INSERT INTO expense_group_config (id, group_id, group_name)
        VALUES (1, %s, %s)
        ON CONFLICT (id)
        DO UPDATE SET
            group_id = EXCLUDED.group_id,
            group_name = EXCLUDED.group_name
        """,
        (chat_id, group_name),
    )

    await update.message.reply_text(
        f"{group_name} is now registered as the expenses group."
    )


# ============================================================
# /REMOVE
# ============================================================


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        await update.message.reply_text(
            "Use /remove inside the group you want to remove from tracking."
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can remove a group from tracking."
        )
        return

    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "This group"

    deactivate_group_transactions(chat_id)

    await update.message.reply_text(
        f"{group_name} has been removed from current month tracking."
    )

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=f"{group_name} was removed from tracking by an admin.",
    )


# ============================================================
# /RESETALL
# ============================================================


async def resetall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id != ACCOUNTS_GROUP_ID:
        await update.message.reply_text(
            "This command can only be used in the Accounts group."
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can reset all totals."
        )
        return

    income_groups, expense_groups, total_income, total_expenses = get_totals()

    if not income_groups and not expense_groups:
        await update.message.reply_text("No groups to reset.")
        return

    lines = ["All groups manually reset:"]

    for row in income_groups:
        lines.append(
            f'{row["group_name"]} : {int(row["total"])} → 0'
        )

    for row in expense_groups:
        lines.append(
            f'{row["group_name"]} expenses : {int(row["total"])} → 0'
        )

    deactivate_all_current_transactions()

    lines.append("\nAll totals are now 0.")

    await update.message.reply_text("\n".join(lines))


# ============================================================
# MONTHLY RESET
# ============================================================


def has_reset_this_month():
    row = db_execute(
        "SELECT id FROM reset_log WHERE reset_month = %s",
        (current_month_key(),),
        fetchone=True,
    )
    return row is not None


def mark_reset_this_month():
    # UNIQUE(reset_month) makes this safe against duplicate reset attempts.
    db_execute(
        """
        INSERT INTO reset_log (reset_month)
        VALUES (%s)
        ON CONFLICT (reset_month) DO NOTHING
        """,
        (current_month_key(),),
    )


async def monthly_reset(context: ContextTypes.DEFAULT_TYPE):
    month_key = current_month_key()

    if has_reset_this_month():
        return

    # Mark first. The UNIQUE constraint prevents a second reset for the
    # same month even if the job is accidentally triggered twice.
    mark_reset_this_month()

    income_groups, expense_groups, total_income, total_expenses = get_totals()

    if not income_groups and not expense_groups:
        return

    lines = [f"Monthly reset — {current_month_label()}"]

    for row in income_groups:
        lines.append(
            f'{row["group_name"]} : {int(row["total"])} → 0'
        )

    for row in expense_groups:
        lines.append(
            f'{row["group_name"]} expenses : {int(row["total"])} → 0'
        )

    deactivate_all_current_transactions()

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text="\n".join(lines),
    )


async def startup_check(context: ContextTypes.DEFAULT_TYPE):
    # The scheduled monthly job handles the normal 1st-of-month reset.
    # This startup check catches a bot restart after midnight on the 1st
    # and also avoids resetting an empty database.
    if has_reset_this_month():
        return

    income_groups, expense_groups, _, _ = get_totals()

    if income_groups or expense_groups:
        await monthly_reset(context)
    else:
        mark_reset_this_month()


# ============================================================
# RENDER HEALTH CHECK
# ============================================================


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Carentza Bot is alive!")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# ============================================================
# BOT STARTUP
# ============================================================

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("total", total_command))
app.add_handler(CommandHandler("reset", reset_command))
app.add_handler(CommandHandler("setexpenses", setexpenses_command))
app.add_handler(CommandHandler("resetall", resetall_command))
app.add_handler(CommandHandler("remove", remove_command))

# Normal messages ONLY.
# filters.TEXT by itself also matches edited messages, so the update type
# must be explicitly restricted to MESSAGE.
app.add_handler(
    MessageHandler(
        filters.TEXT
        & ~filters.COMMAND
        & filters.UpdateType.MESSAGE,
        handle_message,
    )
)

# Edited messages ONLY.
# This ensures an edited message cannot be consumed by the normal handler.
app.add_handler(
    MessageHandler(
        filters.TEXT
        & filters.UpdateType.EDITED_MESSAGE,
        handle_edited_message,
    )
)

# Catch a restart after the scheduled midnight job was missed.
app.job_queue.run_once(startup_check, when=5)

# Monthly reset at midnight on the 1st, Kuala Lumpur time.
app.job_queue.run_monthly(
    monthly_reset,
    when=time(0, 0, 0, tzinfo=KL_TZ),
    day=1,
)

threading.Thread(
    target=run_health_server,
    daemon=True,
).start()

print("Carentza Bot is running...")
app.run_polling()
