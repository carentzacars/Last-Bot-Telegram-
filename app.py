import os
import re
import psycopg2
from datetime import datetime, time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ACCOUNTS_GROUP_ID = int(os.environ["ACCOUNTS_GROUP_ID"])
# Your stable cloud pooler network link remains locked right here
DATABASE_URL = "postgresql://postgres.olggemwgtblmwvtwwivv:MSZwxf3055900@://supabase.com"  
EXPENSES_GROUP_NAME = "cars expenses"

conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cursor = conn.cursor()

# Core relational database tables
cursor.execute("""
CREATE TABLE IF NOT EXISTS group_totals (
    group_id TEXT PRIMARY KEY,
    group_name TEXT,
    total INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS reset_log (
    id SERIAL PRIMARY KEY,
    reset_month TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS group_expenses (
    group_id TEXT PRIMARY KEY,
    group_name TEXT,
    total INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS expense_group_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    group_id TEXT NOT NULL,
    group_name TEXT NOT NULL
)
""")

# New Table to track message histories for message editing math
cursor.execute("""
CREATE TABLE IF NOT EXISTS message_history (
    message_id TEXT,
    chat_id TEXT,
    msg_type TEXT, -- 'price', 'refund', or 'expense'
    amount INTEGER DEFAULT 0,
    PRIMARY KEY (message_id, chat_id)
)
""")
conn.commit()

def has_reset_this_month():
    month_key = datetime.now().strftime("%Y-%m")
    cursor.execute("SELECT id FROM reset_log WHERE reset_month = %s", (month_key,))
    return cursor.fetchone() is not None

def log_monthly_reset():
    month_key = datetime.now().strftime("%Y-%m")
    cursor.execute("INSERT INTO reset_log (reset_month) VALUES (%s)", (month_key,))
    conn.commit()

def extract_price(message_text):
    match = re.search(r"(?im)^\s*price\s*:\s*(?:rm\s*)?(\d+)\s*(?:rm|cash|online)?\b", message_text)
    return int(match.group(1)) if match else None

def extract_refund(message_text):
    match = re.search(r"(?im)^\s*refund\s*:\s*(?:rm\s*)?(\d+)\s*(?:rm|cash|online)?\b", message_text)
    return int(match.group(1)) if match else None

def extract_expense(message_text):
    if re.search(r"(?i)\badvance(?:d)?\b", message_text):
        return None
    matches = re.finditer(r"(?i)(?:\brm\s*(\d+)\b|(\d+)\s*rm\b|\bamount\s*:\s*(?:rm\s*)?(\d+)\b)", message_text)
    amounts = [int(match.group(1) or match.group(2) or match.group(3)) for match in matches]
    return sum(amounts) if amounts else None

def is_expenses_group(group_name):
    normalized_name = re.sub(r"[^a-z0-9]", "", group_name.casefold())
    normalized_expenses_name = re.sub(r"[^a-z0-9]", "", EXPENSES_GROUP_NAME.casefold())
    return normalized_name == normalized_expenses_name

def is_configured_expenses_group(group_id, group_name):
    cursor.execute("SELECT group_id FROM expense_group_config WHERE id = 1")
    row = cursor.fetchone()
    db_group_id = row[0] if row else None
    return (db_group_id is not None and db_group_id == group_id) or is_expenses_group(group_name)

def get_existing_total(group_id):
    cursor.execute("SELECT total FROM group_totals WHERE group_id = %s", (group_id,))
    row = cursor.fetchone()
    return int(row[0]) if row else 0

def get_existing_expense(group_id):
    cursor.execute("SELECT total FROM group_expenses WHERE group_id = %s", (group_id,))
    row = cursor.fetchone()
    return int(row[0]) if row else 0

def save_total(group_id, group_name, total):
    cursor.execute("""
    INSERT INTO group_totals (group_id, group_name, total)
    VALUES (%s, %s, %s)
    ON CONFLICT(group_id)
    DO UPDATE SET group_name = EXCLUDED.group_name, total = EXCLUDED.total
    """, (group_id, group_name, int(total)))
    conn.commit()

def save_expense(group_id, group_name, total):
    cursor.execute("""
    INSERT INTO group_expenses (group_id, group_name, total)
    VALUES (%s, %s, %s)
    ON CONFLICT(group_id)
    DO UPDATE SET group_name = EXCLUDED.group_name, total = EXCLUDED.total
    """, (group_id, group_name, int(total)))
    conn.commit()

def get_all_groups():
    cursor.execute("SELECT group_id FROM expense_group_config WHERE id = 1")
    row = cursor.fetchone()
    config_exp_id = row[0] if row else "NONE"

    cursor.execute("""
        SELECT group_id, group_name, total FROM group_totals 
        WHERE group_id != %s AND group_id != %s AND LOWER(group_name) != %s
    """, (str(ACCOUNTS_GROUP_ID), str(config_exp_id), EXPENSES_GROUP_NAME.lower()))
    return cursor.fetchall()

def get_all_expenses():
    cursor.execute("SELECT group_id, group_name, total FROM group_expenses WHERE group_id != %s", (str(ACCOUNTS_GROUP_ID),))
    return cursor.fetchall()

def reset_all_totals():
    cursor.execute("UPDATE group_totals SET total = 0")
    cursor.execute("UPDATE group_expenses SET total = 0")
    cursor.execute("DELETE FROM message_history")
    conn.commit()

def get_msg_history(message_id, chat_id):
    cursor.execute("SELECT msg_type, amount FROM message_history WHERE message_id = %s AND chat_id = %s", (message_id, chat_id))
    return cursor.fetchone()

def save_msg_history(message_id, chat_id, msg_type, amount):
    cursor.execute("""
    INSERT INTO message_history (message_id, chat_id, msg_type, amount)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (message_id, chat_id)
    DO UPDATE SET msg_type = EXCLUDED.msg_type, amount = EXCLUDED.amount
    """, (message_id, chat_id, msg_type, int(amount)))
    conn.commit()

async def process_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE, is_edited=False):
    msg = update.edited_message if is_edited else update.message
    if not msg or not msg.text:
        return

    message_text = msg.text
    source_group_id = str(update.effective_chat.id)
    source_group_name = update.effective_chat.title or "Unknown Group"
    message_id = str(msg.message_id)

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        return

    price = extract_price(message_text)
    refund = extract_refund(message_text)
    expense = extract_expense(message_text) if is_configured_expenses_group(source_group_id, source_group_name) else None

    # Get history if this is an edited message layout
    history = get_msg_history(message_id, source_group_id) if is_edited else None

    # Scenario A: Processing expenses inside the Cars Expenses group chat
    if is_configured_expenses_group(source_group_id, source_group_name):
        current_amount = expense if expense is not None else 0
        old_amount = history[1] if (history and history[0] == 'expense') else 0
        
        if current_amount == 0 and old_amount == 0 and price is None and refund is None:
            return

        diff = current_amount - old_amount
        existing_expense = get_existing_expense(source_group_id)
        new_expense = existing_expense + diff
        save_expense(source_group_id, source_group_name, new_expense)
        save_msg_history(message_id, source_group_id, 'expense', current_amount)

        all_groups = get_all_groups()
        all_expenses = get_all_expenses()
        grand_total = sum(total for _, _, total in all_groups)
        expense_total = sum(total for _, _, total in all_expenses)

        tag = " [EDITED]" if is_edited else ""
        lines = [
            f"{source_group_name} expense{tag}: {existing_expense} + ({diff}) = {new_expense}",
            "",
            "All Groups:",
        ]
        for _, group_name, total in all_groups:
            lines.append(f"{group_name} : {total}")

        lines.extend([f"\nTotal : {grand_total}", "", "Expenses:"])
        for _, group_name, total in all_expenses:
            lines.append(f"{group_name} : {total}")

        lines.extend([
            f"\nExpenses Total : {expense_total}",
            f"Net Total : {grand_total - expense_total}",
        ])

        await context.bot.send_message(chat_id=ACCOUNTS_GROUP_ID, text="\n".join(lines))
        return

    # Scenario B: Processing regular vehicle income (Prices & Refunds)
    if price is None and refund is None:
        return

    # Math computation layout tracking differences
    old_type = history[0] if history else None
    old_amount = history[1] if history else 0

    existing_total = get_existing_total(source_group_id)
    base_total = existing_total
    
    if old_type == 'price':
        base_total -= old_amount
    elif old_type == 'refund':
        base_total += old_amount

    if price is not None:
        new_total = base_total + price
        diff = new_total - existing_total
        save_msg_history(message_id, source_group_id, 'price', price)
        tag = " [EDITED]" if is_edited else ""
        update_line = f"{source_group_name}{tag} : {existing_total} + ({diff}) = {new_total}"
    else:
        new_total = base_total - refund
        diff = new_total - existing_total
        save_msg_history(message_id, source_group_id, 'refund', refund)
        tag = " [EDITED]" if is_edited else ""
        update_line = f"{source_group_name}{tag} : {existing_total} + ({diff}) = {new_total} (refund change)"

    save_total(source_group_id, source_group_name, new_total)

    all_groups = get_all_groups()
    all_expenses = get_all_expenses()
    grand_total = sum(total for _, _, total in all_groups)
    expense_total = sum(total for _, _, total in all_expenses)

    lines = [update_line, "", "All Groups:"]
    for _, group_name, total in all_groups:
        lines.append(f"{group_name} : {total}")

    lines.extend([
        f"\nTotal : {grand_total}",
        f"Expenses Total : {expense_total}",
        f"Net Total : {grand_total - expense_total}",
    ])

    await context.bot.send_message(chat_id=ACCOUNTS_GROUP_ID, text="\n".join(lines))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_any_message(update, context, is_edited=False)

async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_any_message(update, context, is_edited=True)

async def total_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        all_groups = get_all_groups()
        all_expenses = get_all_expenses()

        if not all_groups and not all_expenses:
            await update.message.reply_text("No group records registered yet.")
            return

        grand_total = sum(total for _, _, total in all_groups)
        expense_total = sum(total for _, _, total in all_expenses)

        lines = ["All Groups:"]
        for _, group_name, total in all_groups:
            lines.append(f"{group_name} : {total}")

        lines.extend([f"\nTotal : {grand_total}", "", "Expenses:"])
        for _, group_name, total in all_expenses:
            lines.append(f"{group_name} : {total}")

        lines.extend([
            f"\nExpenses Total : {expense_total}",
            f"Net Total : {grand_total - expense_total}",
        ])
        await update.message.reply_text("\n".join(lines))
    else:
        group_name = update.effective_chat.title or "This group"
        total = get_existing_total(chat_id)
        expense_total = get_existing_expense(chat_id)
        await update.message.reply_text(
            f"{group_name} total: {total}\n"
            f"{group_name} expenses: {expense_total}\n"
            f"Net total: {total - expense_total}"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Here's what I can do:\n\n"
        "Price: 200 — add amount to this group's total\n"
        "Refund: 50 — deduct amount from this group's total\n\n"
        "Expenses — send a message containing RM 15, RM50, or 50 RM\n\n"
        "Commands:\n"
        "/total — show current total (all groups if used in Accounts group)\n"
        "/reset — reset this group's total to 0 (admins only)\n"
        "/setexpenses — register this group as Car Expenses (admin only)\n"
        "/resetall — reset all groups at once (admins only, Accounts group only)\n"
        "/remove — remove this group from tracking (admins only)\n\n"
        "Totals are automatically reset on the 1st of every month."
    )
    await update.message.reply_text(text)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]

    if user_id not in admin_ids:
        await update.message.reply_text("Only group admins can reset the total.")
        return

    group_name = update.effective_chat.title or "This group"
    old_total = get_existing_total(chat_id)
    old_expense = get_existing_expense(chat_id)

    save_total(chat_id, group_name, 0)
    save_expense(chat_id, group_name, 0)

    await update.message.reply_text(f"{group_name} reset:\nTotal: {old_total} → 0\nExpenses: {old_expense} → 0")
    await context.bot.send_message(
        chat_id=ACCOUNTS_GROUP_ID,
        text=f"{group_name} was manually reset by an admin:\nTotal: {old_total} → 0\nExpenses: {old_expense} → 0"
    )

async def setexpenses_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        await update.message.reply_text("Use /setexpenses inside the Cars Expenses group.")
        return

    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]

    if user_id not in admin_ids:
        await update.message.reply_text("Only group admins can set the expenses group.")
        return

    group_name = update.effective_chat.title or "Cars Expenses"
    cursor.execute("""
        INSERT INTO expense_group_config (id, group_id, group_name)
        VALUES (1, %s, %s)
        ON CONFLICT(id) DO UPDATE SET group_id = EXCLUDED.group_id, group_name = EXCLUDED.group_name
    """, (chat_id, group_name))
    conn.commit()
    await update.message.reply_text(f"{group_name} is now registered as the expenses group.")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    if update.effective_chat.id == ACCOUNTS_GROUP_ID:
        await update.message.reply_text("Use /remove inside the group you want to remove from tracking.")
        return

    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = [admin.user.id for admin in admins]

    if user_id not in admin_ids:
        await update.message.reply_text("Only group admins can remove a group from tracking.")
        return

    group_name = update.effective_chat.title or "This group"
    cursor.execute("DELETE FROM group_totals WHERE group_id = %s", (chat_id,))
    cursor.execute("DELETE FROM group_expenses WHERE group_id = %s", (chat_id,))
    conn.commit()

    await update.message.reply_text(f"{group_name} has been removed from tracking.")
    await context.bot.send_message(chat_id=ACCOUNTS_GROUP_ID, text=f"{group_name} was removed from tracking by an admin.")

async def resetall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if update.effective_chat.id != ACCOUNTS_GROUP_ID:
        await update.message.reply_text("This command can only be used in the Accounts group.")
        return

    admins = await context.bot.get_chat_administrators(ACCOUNTS_GROUP_ID)
    admin_ids = [admin.user.id for admin in admins]

    if user_id not in admin_ids:
        await update.message.reply_text("Only group admins can reset all totals.")
        return

    groups = get_all_groups()
    expenses = get_all_expenses()

    if not groups and not expenses:
        await update.message.reply_text("No groups to reset.")
        return

    lines = ["All groups manually reset:"]
    for _, group_name, total in groups:
        lines.append(f"{group_name} : {total} → 0")
    for _, group_name, total in expenses:
        lines.append(f"{group_name} expenses : {total} → 0")

    reset_all_totals()
    lines.append("\nAll totals are now 0.")
    await update.message.reply_text("\n".join(lines))

async def monthly_reset(context: ContextTypes.DEFAULT_TYPE):
    if has_reset_this_month():
        return

    groups = get_all_groups()
    expenses = get_all_expenses()
    
    log_monthly_reset()

    if not groups and not expenses:
        return

    month_label = datetime.now().strftime("%B %Y")
    lines = [f"Monthly reset — {month_label}"]

    for _, group_name, total in groups:
        lines.append(f"{group_name} : {total} → 0")
    for _, group_name, total in expenses:
        lines.append(f"{group_name} expenses : {total} → 0")

    reset_all_totals()

    await context.bot.send_message(chat_id=ACCOUNTS_GROUP_ID, text="\n".join(lines))

async def startup_check(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT COUNT(*) FROM group_totals")
    has_data = cursor.fetchone() > 0
    
    if not has_reset_this_month():
        if not has_data:
            log_monthly_reset()
        else:
            await monthly_reset(context)

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("total", total_command))
app.add_handler(CommandHandler("reset", reset_command))
app.add_handler(CommandHandler("setexpenses", setexpenses_command))
app.add_handler(CommandHandler("resetall", resetall_command))
app.add_handler(CommandHandler("remove", remove_command))

# Handlers for both new messages and edited corrections
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message))

app.job_queue.run_once(startup_check, when=5)
app.job_queue.run_monthly(monthly_reset, when=time(0, 0, 0), day=1)

# KEEP-ALIVE SERVER CONFIGURATION
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

print("Carentza Cars Bot is running...")
app.run_polling()


