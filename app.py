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
        # Existing tables are deliberately preserved.
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
                id SERIAL PRIMARY KEY,
                reset_month TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS expense_group_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                group_id TEXT NOT NULL,
                group_name TEXT NOT NULL
            )
        """)

        # Additive ledger used for edits, duplicate protection, /remove,
        # and future-month /recalculate. It does NOT import old balances.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transaction_ledger (
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
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_ledger_message
            ON transaction_ledger (group_id, telegram_message_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_ledger_month
            ON transaction_ledger (month_key, is_active, transaction_type)
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


def month_is_valid(value):
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value))


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
    return (
        configured_id is not None and configured_id == group_id
    ) or is_expenses_group(group_name)


# ============================================================
# MESSAGE PARSING
# ============================================================


def extract_price(message_text):
    match = re.search(
        r"(?im)^\s*price\s*:\s*(?:rm\s*)?(\d+(?:\.\d+)?)"
        r"\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    return int(float(match.group(1))) if match else None


def extract_refund(message_text):
    match = re.search(
        r"(?im)^\s*refund\s*:\s*(?:rm\s*)?(\d+(?:\.\d+)?)"
        r"\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    return int(float(match.group(1))) if match else None


def extract_expense(message_text):
    # "advance" messages are not expenses.
    if re.search(r"(?i)\badvance(?:d)?\b", message_text):
        return None

    # Preferred structured format:
    # Amount: 50
    match = re.search(
        r"(?im)^\s*amount\s*:\s*(?:rm\s*)?"
        r"(\d+(?:\.\d+)?)\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    if match:
        return int(float(match.group(1)))

    # Legacy/free-form format:
    # Only explicitly marked RM values count.
    # A bare line such as "6964" is deliberately ignored because it can
    # be a vehicle number.
    amounts = []

    for line in message_text.splitlines():
        line = line.strip()

        if not line or re.fullmatch(r"\d+(?:\.\d+)?", line):
            continue

        for match in re.finditer(
            r"(?i)(?:\brm\s*(\d+(?:\.\d+)?)\b|"
            r"(\d+(?:\.\d+)?)\s*rm\b)",
            line,
        ):
            value = match.group(1) or match.group(2)
            if value:
                amounts.append(int(float(value)))

    return sum(amounts) if amounts else None


def extract_expense_name(message_text):
    # Structured expense details.
    car = re.search(
        r"(?im)^\s*car\s*name\s*:\s*(.+?)\s*$",
        message_text,
    )
    amount = re.search(
        r"(?im)^\s*amount\s*:\s*(?:rm\s*)?"
        r"\d+(?:\.\d+)?\s*(?:rm|cash|online)?\s*$",
        message_text,
    )
    paid = re.search(
        r"(?im)^\s*paid\s*to\s*:\s*(.+?)\s*$",
        message_text,
    )
    details = re.search(
        r"(?im)^\s*details\s*:\s*(.+?)\s*$",
        message_text,
    )

    if car or paid or details:
        parts = []
        if car:
            parts.append(car.group(1).strip())
        if details:
            parts.append(details.group(1).strip())
        if paid:
            parts.append(f"Paid to {paid.group(1).strip()}")
        return " - ".join(parts) if parts else "Expense"

    # Free-form expense name: first line, with money removed.
    lines = [line.strip() for line in message_text.splitlines() if line.strip()]
    if not lines:
        return "Expense"

    name = lines[0]
    name = re.sub(
        r"(?i)\brm\s*\d+(?:\.\d+)?\b|"
        r"\b\d+(?:\.\d+)?\s*rm\b",
        "",
        name,
    )
    name = re.sub(r"(?i)\b(?:cash|online)\b", "", name)
    name = re.sub(r"\s+", " ", name).strip(" :-–—")
    return name if name else "Expense"


# ============================================================
# EXISTING TOTAL TABLE HELPERS
# ============================================================


def get_existing_total(group_id):
    row = db_execute(
        "SELECT total FROM group_totals WHERE group_id = %s",
        (group_id,),
        fetchone=True,
    )
    return int(row["total"]) if row else 0


def get_existing_expense(group_id):
    row = db_execute(
        "SELECT total FROM group_expenses WHERE group_id = %s",
        (group_id,),
        fetchone=True,
    )
    return int(row["total"]) if row else 0


def save_total(group_id, group_name, total):
    db_execute(
        """
        INSERT INTO group_totals (group_id, group_name, total)
        VALUES (%s, %s, %s)
        ON CONFLICT(group_id)
        DO UPDATE SET
            group_name = EXCLUDED.group_name,
            total = EXCLUDED.total
        """,
        (group_id, group_name, int(total)),
    )


def save_expense(group_id, group_name, total):
    db_execute(
        """
        INSERT INTO group_expenses (group_id, group_name, total)
        VALUES (%s, %s, %s)
        ON CONFLICT(group_id)
        DO UPDATE SET
            group_name = EXCLUDED.group_name,
            total = EXCLUDED.total
        """,
        (group_id, group_name, int(total)),
    )


def get_all_groups():
    return db_execute(
        """
        SELECT group_id, group_name, total
        FROM group_totals
        WHERE group_id != %s
        ORDER BY group_name
        """,
        (str(ACCOUNTS_GROUP_ID),),
        fetchall=True,
    )


def get_all_expenses():
    return db_execute(
        """
        SELECT group_id, group_name, total
        FROM group_expenses
        WHERE group_id != %s
        ORDER BY group_name
        """,
        (str(ACCOUNTS_GROUP_ID),),
        fetchall=True,
    )


# ============================================================
# LEDGER HELPERS
# ============================================================


def ledger_get(group_id, message_id):
    return db_execute(
        """
        SELECT *
        FROM transaction_ledger
        WHERE group_id = %s
          AND telegram_message_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (group_id, message_id),
        fetchone=True,
    )


def ledger_insert(
    group_id,
    group_name,
    message_id,
    transaction_type,
    amount,
    description,
):
    # Explicit existence check means we do not depend on a UNIQUE
    # constraint in an already-existing Supabase table.
    if ledger_get(group_id, message_id):
        return None

    return db_execute(
        """
        INSERT INTO transaction_ledger (
            group_id,
            group_name,
            telegram_message_id,
            transaction_type,
            amount,
            description,
            month_key
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            group_id,
            group_name,
            int(message_id),
            transaction_type,
            int(amount),
            description,
            current_month_key(),
        ),
        fetchone=True,
    )["id"]


def ledger_update(row_id, transaction_type, amount, description, group_name):
    db_execute(
        """
        UPDATE transaction_ledger
        SET transaction_type = %s,
            amount = %s,
            description = %s,
            group_name = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            transaction_type,
            int(amount),
            description,
            group_name,
            row_id,
        ),
    )


def ledger_remove_amount(group_id, amount):
    row = db_execute(
        """
        SELECT *
        FROM transaction_ledger
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
          AND transaction_type = 'expense'
          AND amount = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (group_id, current_month_key(), int(amount)),
        fetchone=True,
    )

    if not row:
        return None

    db_execute(
        """
        UPDATE transaction_ledger
        SET is_active = FALSE,
            updated_at = NOW()
        WHERE id = %s
        """,
        (row["id"],),
    )

    return row


def ledger_month_rows(month):
    return db_execute(
        """
        SELECT
            group_id,
            MAX(group_name) AS group_name,
            COALESCE(
                SUM(amount) FILTER (
                    WHERE transaction_type IN ('income', 'refund')
                ), 0
            ) AS income,
            COALESCE(
                SUM(amount) FILTER (
                    WHERE transaction_type = 'expense'
                ), 0
            ) AS expense
        FROM transaction_ledger
        WHERE month_key = %s
          AND is_active = TRUE
          AND group_id != %s
        GROUP BY group_id
        ORDER BY group_name
        """,
        (month, str(ACCOUNTS_GROUP_ID)),
        fetchall=True,
    )


# ============================================================
# ACCOUNTS DISPLAY
# ============================================================


def build_accounts_message(action_line):
    groups = get_all_groups()
    expenses = get_all_expenses()

    total_income = sum(int(row["total"]) for row in groups)
    total_expenses = sum(int(row["total"]) for row in expenses)

    lines = [action_line, "", "Cars Income:"]

    for row in groups:
        lines.append(f'{row["group_name"]} : {int(row["total"])}')

    lines.extend([
        "",
        f"Total Income: {total_income}",
        f"Total Expenses: {total_expenses}",
        f"Net Total : {total_income - total_expenses}",
    ])

    return "\n".join(lines)


# ============================================================
# ADMIN
# ============================================================


async def is_admin(update, context):
    if not update.effective_chat or not update.effective_user:
        return False

    admins = await context.bot.get_chat_administrators(
        update.effective_chat.id
    )
    return update.effective_user.id in {
        admin.user.id for admin in admins
    }


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

    # ========================================================
    # DETERMINE TRANSACTION
    # ========================================================

    if expense_group:
        # Expenses must not also look like income/refund.
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

    # ========================================================
    # NEW MESSAGE
    # ========================================================

    if not is_edited:
        ledger_id = ledger_insert(
            source_group_id,
            source_group_name,
            message_id,
            transaction_type,
            amount,
            description,
        )

        # Same Telegram message already processed.
        if ledger_id is None:
            return

        if transaction_type == "income":
            old_total = get_existing_total(source_group_id)
            new_total = old_total + amount
            save_total(
                source_group_id,
                source_group_name,
                new_total,
            )

            action_line = (
                f"{source_group_name} "
                f"{old_total} + {amount} = {new_total}"
            )

        elif transaction_type == "refund":
            old_total = get_existing_total(source_group_id)
            new_total = old_total + amount
            save_total(
                source_group_id,
                source_group_name,
                new_total,
            )

            action_line = (
                f"{source_group_name} "
                f"{old_total} + Refund: {amount} = {new_total}"
            )

        else:
            old_expense = get_existing_expense(source_group_id)
            new_expense = old_expense + amount
            save_expense(
                source_group_id,
                source_group_name,
                new_expense,
            )

            action_line = (
                f"Cars Expenses\n"
                f"{description} : "
                f"{old_expense} + {amount} = {new_expense}"
            )

        await context.bot.send_message(
            chat_id=ACCOUNTS_GROUP_ID,
            text=build_accounts_message(action_line),
        )
        return

    # ========================================================
    # EDITED MESSAGE
    # ========================================================

    existing = ledger_get(source_group_id, message_id)

    # The new bot only recalculates edits for messages it recorded.
    if not existing:
        return

    # Do not allow an old month's edit to alter the current month.
    if existing["month_key"] != current_month_key():
        return

    old_amount = int(existing["amount"])
    adjustment = int(amount) - old_amount

    ledger_update(
        existing["id"],
        transaction_type,
        amount,
        description,
        source_group_name,
    )

    if transaction_type in ("income", "refund"):
        old_total = get_existing_total(source_group_id)
        new_total = old_total + adjustment

        save_total(
            source_group_id,
            source_group_name,
            new_total,
        )

        action_line = (
            f"{source_group_name} "
            f"{old_total} + Edit: {adjustment:+d} = {new_total}"
        )

    else:
        old_expense_total = get_existing_expense(source_group_id)
        new_expense_total = old_expense_total + adjustment

        save_expense(
            source_group_id,
            source_group_name,
            new_expense_total,
        )

        action_line = (
            f"Cars Expenses\n"
            f"{description} : "
            f"{old_expense_total} + Edit: {adjustment:+d} "
            f"= {new_expense_total}"
        )

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=build_accounts_message(action_line),
    )


async def handle_message(update, context):
    await process_message(update, context, is_edited=False)


async def handle_edited_message(update, context):
    await process_message(update, context, is_edited=True)


# ============================================================
# /TOTAL
# ============================================================


async def total_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        groups = get_all_groups()
        expenses = get_all_expenses()

        if not groups and not expenses:
            await update.message.reply_text(
                "No group totals recorded yet."
            )
            return

        total_income = sum(int(row["total"]) for row in groups)
        total_expenses = sum(int(row["total"]) for row in expenses)

        lines = ["Cars Income:"]

        for row in groups:
            lines.append(
                f'{row["group_name"]} : {int(row["total"])}'
            )

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

    income = get_existing_total(group_id)
    expenses = get_existing_expense(group_id)

    await update.message.reply_text(
        f"{group_name} total: {income}\n"
        f"{group_name} expenses: {expenses}\n"
        f"Net total: {income - expenses}"
    )


# ============================================================
# /HELP
# ============================================================


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Here's what I can do:\n\n"
        "Income:\n"
        "Price: 200\n"
        "Refund: 50\n\n"
        "Expenses:\n"
        "RM 15 / RM50 / 50 RM\n"
        "or:\n"
        "Car name: GENERAL\n"
        "Amount: 50\n"
        "Paid to: GOOGLE\n"
        "Details: GOOGLE ADS\n\n"
        "Commands:\n"
        "/total — show current totals\n"
        "/recalculate — recalculate the current month\n"
        "/recalculate YYYY-MM — recalculate a specific month\n"
        "/remove AMOUNT — remove the most recent matching expense\n"
        "/reset — reset this group's current totals (admin)\n"
        "/setexpenses — register this group as Cars Expenses (admin)\n"
        "/resetall — reset all current totals (Accounts admin)\n"
        "/help — show these commands\n\n"
        "Edited Price, Refund, and Expense messages are automatically "
        "recalculated.\n"
        "Deleted messages are not processed."
    )


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

    old_income = get_existing_total(chat_id)
    old_expense = get_existing_expense(chat_id)

    save_total(chat_id, group_name, 0)
    save_expense(chat_id, group_name, 0)

    # Close active ledger entries for the current month.
    db_execute(
        """
        UPDATE transaction_ledger
        SET is_active = FALSE, updated_at = NOW()
        WHERE group_id = %s
          AND month_key = %s
          AND is_active = TRUE
        """,
        (chat_id, current_month_key()),
    )

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


async def setexpenses_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
        ON CONFLICT(id)
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
# /REMOVE AMOUNT
# ============================================================


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        await update.message.reply_text(
            "Use /remove inside the Cars Expenses group."
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can remove expenses."
        )
        return

    if (
        not context.args
        or not re.fullmatch(r"\d+(?:\.\d+)?", context.args[0])
    ):
        await update.message.reply_text(
            "Use /remove AMOUNT\n"
            "Example: /remove 50"
        )
        return

    amount = int(float(context.args[0]))
    chat_id = str(update.effective_chat.id)
    group_name = update.effective_chat.title or "Cars Expenses"

    row = ledger_remove_amount(chat_id, amount)

    if not row:
        await update.message.reply_text(
            f"No active RM{amount} expense was found for this month."
        )
        return

    old_expense = get_existing_expense(chat_id)
    new_expense = max(0, old_expense - amount)

    save_expense(chat_id, group_name, new_expense)

    await update.message.reply_text(
        f"Removed RM{amount} expense:\n"
        f"{row.get('description') or 'Expense'}"
    )

    action_line = (
        f"Cars Expenses\n"
        f"Removed: {row.get('description') or 'Expense'} "
        f"- {amount}"
    )

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=build_accounts_message(action_line),
    )


# ============================================================
# /RESETALL
# ============================================================


async def resetall_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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

    groups = get_all_groups()
    expenses = get_all_expenses()

    if not groups and not expenses:
        await update.message.reply_text("No groups to reset.")
        return

    lines = ["All groups manually reset:"]

    for row in groups:
        lines.append(
            f'{row["group_name"]} : {int(row["total"])} → 0'
        )

    for row in expenses:
        lines.append(
            f'{row["group_name"]} expenses : '
            f'{int(row["total"])} → 0'
        )

    db_execute("UPDATE group_totals SET total = 0")
    db_execute("UPDATE group_expenses SET total = 0")

    db_execute(
        """
        UPDATE transaction_ledger
        SET is_active = FALSE, updated_at = NOW()
        WHERE month_key = %s AND is_active = TRUE
        """,
        (current_month_key(),),
    )

    lines.append("\nAll totals are now 0.")
    await update.message.reply_text("\n".join(lines))


# ============================================================
# /RECALCULATE
# ============================================================


async def recalculate_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.id != ACCOUNTS_GROUP_ID:
        await update.message.reply_text(
            "This command can only be used in the Accounts group."
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "Only group admins can recalculate totals."
        )
        return

    requested_month = current_month_key()

    if context.args:
        requested_month = context.args[0].strip()

        if not month_is_valid(requested_month):
            await update.message.reply_text(
                "Use /recalculate or /recalculate YYYY-MM\n"
                "Example: /recalculate 2026-09"
            )
            return

    rows = ledger_month_rows(requested_month)

    total_income = sum(int(row["income"]) for row in rows)
    total_expenses = sum(int(row["expense"]) for row in rows)

    lines = [
        f"Recalculated: {requested_month}",
        "",
        "Cars Income:",
    ]

    for row in rows:
        income = int(row["income"])
        if income:
            lines.append(
                f'{row["group_name"]} : {income}'
            )

    lines.extend([
        "",
        "Cars Expenses",
    ])

    expense_rows = [
        row for row in rows if int(row["expense"])
    ]

    if expense_rows:
        for row in expense_rows:
            lines.append(
                f'{row["group_name"]} : {int(row["expense"])}'
            )
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
        """
        SELECT id
        FROM reset_log
        WHERE reset_month = %s
        LIMIT 1
        """,
        (current_month_key(),),
        fetchone=True,
    )
    return row is not None


def mark_reset_this_month():
    if not has_reset_this_month():
        db_execute(
            """
            INSERT INTO reset_log (reset_month)
            VALUES (%s)
            """,
            (current_month_key(),),
        )


def reset_current_month_totals():
    db_execute("UPDATE group_totals SET total = 0")
    db_execute("UPDATE group_expenses SET total = 0")

    db_execute(
        """
        UPDATE transaction_ledger
        SET is_active = FALSE, updated_at = NOW()
        WHERE month_key = %s
          AND is_active = TRUE
        """,
        (current_month_key(),),
    )


async def monthly_reset(context: ContextTypes.DEFAULT_TYPE):
    if has_reset_this_month():
        return

    groups = get_all_groups()
    expenses = get_all_expenses()

    # Mark the month first so a duplicate job cannot reset it twice.
    mark_reset_this_month()

    if not groups and not expenses:
        return

    lines = [f"Monthly reset — {current_month_label()}"]

    for row in groups:
        lines.append(
            f'{row["group_name"]} : {int(row["total"])} → 0'
        )

    for row in expenses:
        lines.append(
            f'{row["group_name"]} expenses : '
            f'{int(row["total"])} → 0'
        )

    reset_current_month_totals()

    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text="\n".join(lines),
    )


async def startup_check(context: ContextTypes.DEFAULT_TYPE):
    # If Render restarts after midnight on the 1st, catch the missed reset.
    if has_reset_this_month():
        return

    groups = get_all_groups()
    expenses = get_all_expenses()

    if groups or expenses:
        await monthly_reset(context)
    else:
        # September 2026 and other genuinely empty months start clean.
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
    port = int(os.environ.get("PORT", 10000))
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

# Normal messages only.
app.add_handler(
    MessageHandler(
        filters.TEXT
        & ~filters.COMMAND
        & filters.UpdateType.MESSAGE,
        handle_message,
    )
)

# Edited messages only.
app.add_handler(
    MessageHandler(
        filters.TEXT
        & filters.UpdateType.EDITED_MESSAGE,
        handle_edited_message,
    )
)

# Catch a Render restart that happens after midnight on the 1st.
app.job_queue.run_once(startup_check, when=5)

# Automatic monthly reset at 00:00 Kuala Lumpur time on day 1.
app.job_queue.run_monthly(
    monthly_reset,
    when=time(0, 0, 0, tzinfo=KL_TZ),
    day=1,
)

threading.Thread(
    target=run_health_server,
    daemon=True,
).start()

print("Carentza Cars Bot is running...")
app.run_polling()
