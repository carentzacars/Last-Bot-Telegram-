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
    global conn
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            else:
                result = None
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def init_database():
    with conn.cursor() as cur:
        # Legacy tables are kept so an existing deployment does not break.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_totals (
                group_id TEXT PRIMARY KEY,
                group_name TEXT,
                total INTEGER DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_expenses (
                group_id TEXT PRIMARY KEY,
                group_name TEXT,
                total INTEGER DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reset_log (
                id BIGSERIAL PRIMARY KEY,
                reset_month TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS expense_group_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                group_id TEXT NOT NULL,
                group_name TEXT NOT NULL
            )
        """)

        # One row per Telegram accounting message.
        # This is what makes edits, refunds, recalculation and duplicate
        # protection reliable.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id BIGSERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                group_name TEXT NOT NULL,
                telegram_message_id BIGINT NOT NULL,
                transaction_type TEXT NOT NULL
                    CHECK (transaction_type IN ('income', 'refund', 'expense', 'opening')),
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
            CREATE INDEX IF NOT EXISTS idx_transactions_month
            ON transactions (month_key, is_active, group_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_message
            ON transactions (group_id, telegram_message_id)
        """)

    conn.commit()


init_database()

# ============================================================
# DATE / GROUP HELPERS
# ============================================================


def current_month_key():
    return datetime.now(KL_TZ).strftime("%Y-%m")


def current_month_label():
    return datetime.now(KL_TZ).strftime("%B %Y")


def normalize_group_name(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def is_expenses_group(group_name):
    return normalize_group_name(group_name) == normalize_group_name(EXPENSES_GROUP_NAME)


def is_configured_expenses_group(group_id, group_name):
    row = db_execute(
        "SELECT group_id FROM expense_group_config WHERE id = 1",
        fetchone=True,
    )
    configured_id = row["group_id"] if row else None
    return (configured_id is not None and configured_id == group_id) or is_expenses_group(group_name)

# ============================================================
# MESSAGE PARSING
# ============================================================


def extract_price(message_text):
    match = re.search(
        r"(?im)^\s*price\s*:\s*(?:rm\s*)?(\d+)\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    return int(match.group(1)) if match else None


def extract_refund(message_text):
    match = re.search(
        r"(?im)^\s*refund\s*:\s*(?:rm\s*)?(\d+)\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    return int(match.group(1)) if match else None


def extract_structured_expense(message_text):
    """Read the explicit Amount field used by structured expenses."""
    match = re.search(
        r"(?im)^\s*amount\s*:\s*(?:rm\s*)?(\d+(?:\.\d+)?)\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    if not match:
        return None
    return int(float(match.group(1)))


def extract_expense(message_text):
    """
    Expense amounts must be explicitly marked with RM / rm or an Amount field.

    This deliberately does NOT treat bare numbers as money. Therefore:
        Car wash myvi black
        6964
        Rm 10
    records RM10, not RM6964.
    """
    if re.search(r"(?i)\badvance(?:d)?\b", message_text):
        return None

    structured = extract_structured_expense(message_text)
    if structured is not None:
        return structured

    # If there is an Amount field but it did not parse, do not fall back to
    # unrelated numbers elsewhere in the message.
    if re.search(r"(?im)^\s*amount\s*:", message_text):
        return None

    matches = re.finditer(
        r"(?i)(?:\brm\s*(\d+(?:\.\d+)?)\b|(\d+(?:\.\d+)?)\s*rm\b)",
        message_text,
    )

    amounts = []
    for match in matches:
        value = match.group(1) or match.group(2)
        if value:
            amounts.append(int(float(value)))

    return sum(amounts) if amounts else None


def extract_structured_expense_details(message_text):
    def field(name):
        pattern = rf"(?im)^\s*{re.escape(name)}\s*:\s*(.*?)\s*$"
        match = re.search(pattern, message_text)
        return match.group(1).strip() if match else ""

    car_name = field("Car name") or "GENERAL"
    paid_to = field("Paid to")
    details = field("Details")

    if paid_to and details:
        description = f"{car_name} — {details} (Paid to: {paid_to})"
    elif details:
        description = f"{car_name} — {details}"
    elif paid_to:
        description = f"{car_name} — Paid to: {paid_to}"
    else:
        description = car_name

    return description


def extract_expense_name(message_text):
    if re.search(r"(?im)^\s*car\s+name\s*:", message_text):
        return extract_structured_expense_details(message_text)

    lines = [line.strip() for line in message_text.splitlines() if line.strip()]
    if not lines:
        return "Expense"

    name = lines[0]
    name = re.sub(r"(?i)\brm\s*\d+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s*rm\b", "", name)
    name = re.sub(r"(?i)\bamount\s*:\s*(?:rm\s*)?\d+(?:\.\d+)?\b", "", name)
    name = re.sub(r"(?i)\b(?:cash|online)\b", "", name)
    name = re.sub(r"\s+", " ", name).strip(" :-–—")
    return name if name else "Expense"

# ============================================================
# TRANSACTION HELPERS
# ============================================================


def get_transaction(group_id, message_id):
    return db_execute(
        """
        SELECT * FROM transactions
        WHERE group_id = %s AND telegram_message_id = %s
        """,
        (group_id, message_id),
        fetchone=True,
    )


def insert_transaction(group_id, group_name, message_id, transaction_type, amount, description):
    row = db_execute(
        """
        INSERT INTO transactions (
            group_id, group_name, telegram_message_id,
            transaction_type, amount, description, month_key
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
            current_month_key(),
        ),
        fetchone=True,
    )
    return row["id"] if row else None


def update_transaction(transaction_id, transaction_type, amount, description, group_name):
    return db_execute(
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
        (transaction_type, int(amount), description, group_name, transaction_id),
        fetchone=True,
    )


def deactivate_transaction(transaction_id):
    db_execute(
        """
        UPDATE transactions
        SET is_active = FALSE, updated_at = NOW()
        WHERE id = %s
        """,
        (transaction_id,),
    )


def get_group_current_total(group_id, month=None):
    month = month or current_month_key()
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type IN ('income', 'refund', 'opening')
        """,
        (group_id, month),
        fetchone=True,
    )
    return int(row["total"])


def get_group_previous_income(group_id, transaction_id):
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type IN ('income', 'refund', 'opening')
          AND id != %s
        """,
        (group_id, current_month_key(), transaction_id),
        fetchone=True,
    )
    return int(row["total"])


def get_group_current_expense(group_id, month=None):
    month = month or current_month_key()
    row = db_execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type = 'expense'
        """,
        (group_id, month),
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


def get_income_groups(month=None):
    month = month or current_month_key()
    return db_execute(
        """
        SELECT group_id, MAX(group_name) AS group_name,
               COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE month_key = %s
          AND is_active = TRUE
          AND transaction_type IN ('income', 'refund', 'opening')
          AND group_id != %s
          AND NOT EXISTS (
              SELECT 1 FROM expense_group_config egc
              WHERE egc.id = 1 AND egc.group_id = transactions.group_id
          )
          AND LOWER(group_name) != LOWER(%s)
        GROUP BY group_id
        ORDER BY group_name
        """,
        (month, str(ACCOUNTS_GROUP_ID), EXPENSES_GROUP_NAME),
        fetchall=True,
    )


def get_expense_groups(month=None):
    month = month or current_month_key()
    return db_execute(
        """
        SELECT group_id, MAX(group_name) AS group_name,
               COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE month_key = %s
          AND is_active = TRUE
          AND transaction_type = 'expense'
          AND group_id != %s
        GROUP BY group_id
        ORDER BY group_name
        """,
        (month, str(ACCOUNTS_GROUP_ID)),
        fetchall=True,
    )


def get_totals(month=None):
    income_groups = get_income_groups(month)
    expense_groups = get_expense_groups(month)
    total_income = sum(int(row["total"]) for row in income_groups)
    total_expenses = sum(int(row["total"]) for row in expense_groups)
    return income_groups, expense_groups, total_income, total_expenses

# ============================================================
# LEGACY DATABASE MIGRATION
# ============================================================


def migrate_legacy_totals_once():
    """
    If the current Supabase database was created by the older bot, preserve
    its current totals as opening balances. From that point onward every new
    Telegram message is stored individually in transactions.

    Opening balances have no real Telegram message, so they use negative
    synthetic message IDs and cannot be edited/removed by Telegram message ID.
    """
    marker = db_execute(
        """
        SELECT 1 FROM transactions
        WHERE transaction_type = 'opening'
        LIMIT 1
        """,
        fetchone=True,
    )
    if marker:
        return

    legacy_income = db_execute(
        "SELECT group_id, group_name, total FROM group_totals WHERE total <> 0",
        fetchall=True,
    )
    legacy_expenses = db_execute(
        "SELECT group_id, group_name, total FROM group_expenses WHERE total <> 0",
        fetchall=True,
    )

    synthetic_id = -1000000000000
    for row in legacy_income:
        exists = get_transaction(str(row["group_id"]), synthetic_id)
        if not exists:
            db_execute(
                """
                INSERT INTO transactions (
                    group_id, group_name, telegram_message_id,
                    transaction_type, amount, description, month_key
                ) VALUES (%s, %s, %s, 'opening', %s, 'Legacy opening balance', %s)
                ON CONFLICT (group_id, telegram_message_id) DO NOTHING
                """,
                (
                    str(row["group_id"]),
                    row["group_name"] or "Unknown Group",
                    synthetic_id,
                    int(row["total"]),
                    current_month_key(),
                ),
            )
        synthetic_id -= 1

    for row in legacy_expenses:
        exists = get_transaction(str(row["group_id"]), synthetic_id)
        if not exists:
            db_execute(
                """
                INSERT INTO transactions (
                    group_id, group_name, telegram_message_id,
                    transaction_type, amount, description, month_key
                ) VALUES (%s, %s, %s, 'expense', %s, 'Legacy expense opening balance', %s)
                ON CONFLICT (group_id, telegram_message_id) DO NOTHING
                """,
                (
                    str(row["group_id"]),
                    row["group_name"] or "Unknown Group",
                    synthetic_id,
                    int(row["total"]),
                    current_month_key(),
                ),
            )
        synthetic_id -= 1

    conn.commit()


migrate_legacy_totals_once()

# ============================================================
# ACCOUNTS MESSAGE
# ============================================================


def build_accounts_message(action_line, month=None):
    income_groups, expense_groups, total_income, total_expenses = get_totals(month)

    lines = [action_line, "", "Cars Income:"]
    for row in income_groups:
        lines.append(f'{row["group_name"]} : {int(row["total"])}')

    # Expenses are intentionally shown as a total section only, matching
    # the requested Accounts layout.
    lines.extend([
        "",
        f"Total Income: {total_income}",
        f"Total Expenses: {total_expenses}",
        f"Net Total : {total_income - total_expenses}",
    ])
    return "\n".join(lines)

# ============================================================
# MESSAGE PROCESSING
# ============================================================


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, is_edited=False):
    msg = update.edited_message if is_edited else update.message
    if not msg or not msg.text or not update.effective_chat:
        return

    source_group_id = str(update.effective_chat.id)
    source_group_name = update.effective_chat.title or "Unknown Group"
    message_id = msg.message_id

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        return

    expense_group = is_configured_expenses_group(source_group_id, source_group_name)
    price = extract_price(msg.text)
    refund = extract_refund(msg.text)
    expense = extract_expense(msg.text) if expense_group else None

    # Expense messages cannot also contain Price/Refund.
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

        # Telegram may redeliver the same message/update. The unique key
        # means it is counted only once.
        if transaction_id is None:
            return

        if transaction_type == "income":
            previous = get_group_previous_income(source_group_id, transaction_id)
            action_line = f"{source_group_name} {previous} + {amount} = {previous + amount}"
        elif transaction_type == "refund":
            previous = get_group_previous_income(source_group_id, transaction_id)
            action_line = f"{source_group_name} {previous} + Refund: {amount} = {previous + amount}"
        else:
            previous = get_group_previous_expense(source_group_id, transaction_id)
            action_line = (
                "Cars Expenses\n"
                f"{description} : {previous} + {amount} = {previous + amount}"
            )

    # --------------------------------------------------------
    # EDITED MESSAGE
    # --------------------------------------------------------
    else:
        existing = get_transaction(source_group_id, message_id)
        if not existing:
            # The bot only edits transactions it originally recorded.
            return

        if existing["month_key"] != current_month_key():
            return

        old_amount = int(existing["amount"])
        old_type = existing["transaction_type"]

        update_transaction(
            existing["id"],
            transaction_type,
            amount,
            description,
            source_group_name,
        )

        adjustment = amount - old_amount

        if transaction_type == "income":
            new_group_total = get_group_current_total(source_group_id)
            previous = new_group_total - amount
            action_line = (
                f"{source_group_name} {previous} + Edit: {adjustment:+d} = {new_group_total}"
            )
        elif transaction_type == "refund":
            new_group_total = get_group_current_total(source_group_id)
            previous = new_group_total - amount
            action_line = (
                f"{source_group_name} {previous} + Refund: {adjustment:+d} = {new_group_total}"
            )
        else:
            new_expense_total = get_group_current_expense(source_group_id)
            previous = new_expense_total - amount
            action_line = (
                "Cars Expenses\n"
                f"{description} : {previous} + Edit: {adjustment:+d} = {new_expense_total}"
            )

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=build_accounts_message(action_line),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_message(update, context, is_edited=False)


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_message(update, context, is_edited=True)

# ============================================================
# ADMIN CHECK
# ============================================================


async def is_admin(update, context):
    if not update.effective_chat or not update.effective_user:
        return False
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    return update.effective_user.id in {admin.user.id for admin in admins}

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
        lines.extend([
            "",
            f"Total Income: {total_income}",
            f"Total Expenses: {total_expenses}",
            f"Net Total : {total_income - total_expenses}",
        ])
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
        "Income:\n"
        "Price: 200 — add 200 to this group's income\n"
        "Refund: 50 — deduct 50 from this group's income\n\n"
        "Expenses:\n"
        "Car wash myvi black\n"
        "6964\n"
        "Rm 10\n\n"
        "Or use:\n"
        "Car name: GENERAL\n"
        "Amount: 50\n"
        "Paid to: GOOGLE\n"
        "Details: GOOGLE ADS\n\n"
        "Commands:\n"
        "/help — show this command list\n"
        "/total — show current totals\n"
        "/recalculate — recalculate this month\n"
        "/recalculate YYYY-MM — recalculate a specific month\n"
        "/reset — reset this group's current month totals (admins only)\n"
        "/resetall — reset all current month totals (Accounts admins only)\n"
        "/setexpenses — register this group as Car Expenses (admin only)\n"
        "/remove MESSAGE_ID — remove one wrongly recorded transaction (admin only)\n\n"
        "Edited Price, Refund and Expense messages are automatically recalculated.\n"
        "Deleted messages are left unchanged."
    )

# ============================================================
# /RESET
# ============================================================


async def deactivate_group_current_month(group_id):
    db_execute(
        """
        UPDATE transactions
        SET is_active = FALSE, updated_at = NOW()
        WHERE group_id = %s AND month_key = %s AND is_active = TRUE
        """,
        (group_id, current_month_key()),
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can reset the total.")
        return

    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "This group"
    old_income = get_group_current_total(chat_id)
    old_expense = get_group_current_expense(chat_id)
    await deactivate_group_current_month(chat_id)

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
        await update.message.reply_text("Use /setexpenses inside the Cars Expenses group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can set the expenses group.")
        return

    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "Cars Expenses"
    db_execute(
        """
        INSERT INTO expense_group_config (id, group_id, group_name)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            group_id = EXCLUDED.group_id,
            group_name = EXCLUDED.group_name
        """,
        (chat_id, group_name),
    )
    await update.message.reply_text(f"{group_name} is now registered as the expenses group.")

# ============================================================
# /REMOVE
# ============================================================


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can use /remove.")
        return

    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "This group"

    # Preferred use: /remove 4533
    if context.args:
        if len(context.args) != 1 or not re.fullmatch(r"\d+", context.args[0]):
            await update.message.reply_text("Use /remove MESSAGE_ID\nExample: /remove 4533")
            return

        message_id = int(context.args[0])
        transaction = get_transaction(chat_id, message_id)

        if not transaction:
            await update.message.reply_text(
                f"I couldn't find a recorded transaction with message ID {message_id}."
            )
            return

        if transaction["transaction_type"] == "opening":
            await update.message.reply_text("That is an opening balance and cannot be removed by message ID.")
            return

        if not transaction["is_active"]:
            await update.message.reply_text("That transaction has already been removed.")
            return

        old_amount = int(transaction["amount"])
        deactivate_transaction(transaction["id"])

        await update.message.reply_text(
            f"Removed message {message_id}: {transaction['description'] or 'Transaction'} ({old_amount})."
        )

        await context.bot.send_message(
            chat_id=ACCOUNTS_GROUP_ID,
            text=build_accounts_message(
                f"Removed: {group_name} {old_amount} from message {message_id}"
            ),
        )
        return

    # Backward-compatible behavior: /remove with no ID removes the current
    # group's current-month transactions, as the older bot did.
    old_income = get_group_current_total(chat_id)
    old_expense = get_group_current_expense(chat_id)
    await deactivate_group_current_month(chat_id)

    await update.message.reply_text(
        f"{group_name} current month tracking removed:\n"
        f"Income: {old_income}\n"
        f"Expenses: {old_expense}"
    )
    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=f"{group_name} was removed from current month tracking by an admin."
    )

# ============================================================
# /RESETALL
# ============================================================


async def deactivate_all_current_transactions():
    db_execute(
        """
        UPDATE transactions
        SET is_active = FALSE, updated_at = NOW()
        WHERE month_key = %s AND is_active = TRUE
        """,
        (current_month_key(),),
    )


async def resetall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return
    if update.effective_chat.id != ACCOUNTS_GROUP_ID:
        await update.message.reply_text("This command can only be used in the Accounts group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can reset all totals.")
        return

    income_groups, expense_groups, _, _ = get_totals()
    if not income_groups and not expense_groups:
        await update.message.reply_text("No groups to reset.")
        return

    lines = ["All groups manually reset:"]
    for row in income_groups:
        lines.append(f'{row["group_name"]} : {int(row["total"])} → 0')
    for row in expense_groups:
        lines.append(f'{row["group_name"]} expenses : {int(row["total"])} → 0')

    await deactivate_all_current_transactions()
    lines.append("\nAll totals are now 0.")
    await update.message.reply_text("\n".join(lines))

# ============================================================
# /RECALCULATE
# ============================================================


def calculate_month(month):
    income_rows = get_income_groups(month)
    expense_rows = get_expense_groups(month)
    total_income = sum(int(row["total"]) for row in income_rows)
    total_expenses = sum(int(row["total"]) for row in expense_rows)
    return income_rows, expense_rows, total_income, total_expenses


async def recalculate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return
    if update.effective_chat.id != ACCOUNTS_GROUP_ID:
        await update.message.reply_text("This command can only be used in the Accounts group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can recalculate totals.")
        return

    requested_month = current_month_key()
    if context.args:
        value = context.args[0].strip()
        if not re.fullmatch(r"\d{4}-\d{2}", value):
            await update.message.reply_text(
                "Use /recalculate or /recalculate YYYY-MM\n"
                "Example: /recalculate 2026-08"
            )
            return
        requested_month = value

    income_rows, expense_rows, total_income, total_expenses = calculate_month(requested_month)

    lines = [f"Recalculated: {requested_month}", "", "Cars Income:"]
    for row in income_rows:
        lines.append(f'{row["group_name"]} : {int(row["total"])}')

    lines.extend([
        "",
        "Cars Expenses:",
    ])
    if expense_rows:
        for row in expense_rows:
            lines.append(f'{row["group_name"]} : {int(row["total"])}')
    else:
        lines.append("None")

    lines.extend([
        "",
        f"Total Income: {total_income}",
        f"Total Expenses: {total_expenses}",
        f"Net Total : {total_income - total_expenses}",
    ])

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
    db_execute(
        """
        INSERT INTO reset_log (reset_month)
        VALUES (%s)
        ON CONFLICT (reset_month) DO NOTHING
        """,
        (current_month_key(),),
    )


async def monthly_reset(context: ContextTypes.DEFAULT_TYPE):
    if has_reset_this_month():
        return

    income_groups, expense_groups, _, _ = get_totals()
    mark_reset_this_month()

    if not income_groups and not expense_groups:
        return

    lines = [f"Monthly reset — {current_month_label()}"]
    for row in income_groups:
        lines.append(f'{row["group_name"]} : {int(row["total"])} → 0')
    for row in expense_groups:
        lines.append(f'{row["group_name"]} expenses : {int(row["total"])} → 0')

    await deactivate_all_current_transactions()
    await context.bot.send_message(chat_id=ACCOUNTS_GROUP_ID, text="\n".join(lines))
    print(f"Monthly reset done for {current_month_label()}")


async def startup_check(context: ContextTypes.DEFAULT_TYPE):
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
        self.wfile.write(b"Carentza Cars Bot is alive!")

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
app.add_handler(CommandHandler("recalculate", recalculate_command))

# Normal messages only. Explicitly exclude edited messages so an edit is not
# processed once as a new message and again as an edit.
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
        handle_message,
    )
)

# Edited messages only.
app.add_handler(
    MessageHandler(
        filters.TEXT & filters.UpdateType.EDITED_MESSAGE,
        handle_edited_message,
    )
)

# Catch a restart that happens after midnight on the first day of a month.
app.job_queue.run_once(startup_check, when=5)
app.job_queue.run_monthly(
    monthly_reset,
    when=time(0, 0, 0, tzinfo=KL_TZ),
    day=1,
)

threading.Thread(target=run_health_server, daemon=True).start()

print("Carentza Cars Bot is running...")
app.run_polling()
