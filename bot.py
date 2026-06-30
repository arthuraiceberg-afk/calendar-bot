import os
import sqlite3
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Список разрешённых chat_id через запятую, например: "111111,222222,333333"
# Узнать свой chat_id просто: напиши боту @userinfobot, он пришлёт число.
ALLOWED_CHAT_IDS = [
    int(cid.strip()) for cid in os.environ["ALLOWED_CHAT_IDS"].split(",") if cid.strip()
]

DB_PATH = "events.db"

REMINDER_OFFSETS = [
    ("12h", timedelta(hours=12)),
    ("2h", timedelta(hours=2)),
    ("1h", timedelta(hours=1)),
    ("15m", timedelta(minutes=15)),
]


# ---------- БАЗА ДАННЫХ ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_time TEXT NOT NULL,  -- ISO format
            status TEXT NOT NULL DEFAULT 'active',  -- active / done / cancelled
            reminded_12h INTEGER DEFAULT 0,
            reminded_2h INTEGER DEFAULT 0,
            reminded_1h INTEGER DEFAULT 0,
            reminded_15m INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def add_event(title, event_time: datetime):
    conn = db()
    cur = conn.execute(
        "INSERT INTO events (title, event_time) VALUES (?, ?)",
        (title, event_time.isoformat()),
    )
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid


def get_event(eid):
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    conn.close()
    return row


def update_event_time(eid, new_time: datetime):
    conn = db()
    conn.execute(
        """UPDATE events SET event_time = ?, status = 'active',
           reminded_12h = 0, reminded_2h = 0, reminded_1h = 0, reminded_15m = 0
           WHERE id = ?""",
        (new_time.isoformat(), eid),
    )
    conn.commit()
    conn.close()


def update_status(eid, status):
    conn = db()
    conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, eid))
    conn.commit()
    conn.close()


def mark_reminded(eid, field):
    conn = db()
    conn.execute(f"UPDATE events SET {field} = 1 WHERE id = ?", (eid,))
    conn.commit()
    conn.close()


def list_active_events():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM events WHERE status = 'active' ORDER BY event_time"
    ).fetchall()
    conn.close()
    return rows


def get_due_reminders():
    """Находит события, для которых пора слать напоминание."""
    conn = db()
    rows = conn.execute("SELECT * FROM events WHERE status = 'active'").fetchall()
    conn.close()
    now = datetime.now()
    due = []
    for row in rows:
        event_time = datetime.fromisoformat(row["event_time"])
        for label, offset in REMINDER_OFFSETS:
            field = f"reminded_{label}"
            if row[field] == 0:
                trigger_time = event_time - offset
                # окно в 1 минуту, чтобы не пропустить из-за частоты опроса
                if trigger_time <= now < trigger_time + timedelta(minutes=1):
                    due.append((row, label, field))
    return due


# ---------- ПАРСИНГ ДАТЫ/ВРЕМЕНИ ----------

def parse_event_input(text: str):
    """
    Ожидает: Название | ДД.ММ.ГГГГ | ЧЧ:ММ
    Возвращает (title, datetime) или бросает ValueError
    """
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        raise ValueError(
            "Формат: /new Название | ДД.ММ.ГГГГ | ЧЧ:ММ\nПример: /new Созвон с Гузель | 03.07.2026 | 15:00"
        )
    title, date_str, time_str = parts
    dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return title, dt


# ---------- ХЕНДЛЕРЫ КОМАНД ----------

async def check_auth(update: Update) -> bool:
    if update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    await update.message.reply_text(
        "Привет! Я твой бот-трекер-календарь.\n\n"
        "Добавить событие:\n"
        "/new Название | ДД.ММ.ГГГГ | ЧЧ:ММ\n\n"
        "Пример:\n"
        "/new Созвон с Гузель | 03.07.2026 | 15:00\n\n"
        "Посмотреть активные события: /list"
    )


async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    text = update.message.text.partition(" ")[2]  # всё после "/new "
    try:
        title, dt = parse_event_input(text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    if dt <= datetime.now():
        await update.message.reply_text("Эта дата/время уже в прошлом, проверь ввод.")
        return

    eid = add_event(title, dt)
    await update.message.reply_text(
        f"✅ Событие добавлено (#{eid}):\n«{title}»\n{dt.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Напомню за 12ч, 2ч, 1ч и 15 минут."
    )


async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    rows = list_active_events()
    if not rows:
        await update.message.reply_text("Активных событий нет.")
        return
    lines = []
    for r in rows:
        dt = datetime.fromisoformat(r["event_time"])
        lines.append(f"#{r['id']} — {r['title']} — {dt.strftime('%d.%m.%Y %H:%M')}")
    await update.message.reply_text("\n".join(lines))


# ---------- КНОПКИ НАПОМИНАНИЙ ----------

def reminder_keyboard(eid):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Сделано", callback_data=f"done:{eid}"),
                InlineKeyboardButton("❌ Отменить", callback_data=f"cancel:{eid}"),
            ],
            [
                InlineKeyboardButton("⏰ +30 мин", callback_data=f"snooze30:{eid}"),
                InlineKeyboardButton("📅 +1 день", callback_data=f"snoozeday:{eid}"),
            ],
        ]
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id not in ALLOWED_CHAT_IDS:
        await query.answer()
        return

    action, eid_str = query.data.split(":")
    eid = int(eid_str)
    row = get_event(eid)

    if row is None:
        await query.answer("Событие не найдено.")
        return

    if action == "done":
        update_status(eid, "done")
        await query.edit_message_text(f"✅ Отмечено как сделано: «{row['title']}»")

    elif action == "cancel":
        update_status(eid, "cancelled")
        await query.edit_message_text(f"❌ Отменено: «{row['title']}»")

    elif action == "snooze30":
        old_dt = datetime.fromisoformat(row["event_time"])
        new_dt = old_dt + timedelta(minutes=30)
        update_event_time(eid, new_dt)
        await query.edit_message_text(
            f"⏰ Перенесено на 30 минут: «{row['title']}»\nНовое время: {new_dt.strftime('%d.%m.%Y %H:%M')}"
        )

    elif action == "snoozeday":
        old_dt = datetime.fromisoformat(row["event_time"])
        new_dt = old_dt + timedelta(days=1)
        update_event_time(eid, new_dt)
        await query.edit_message_text(
            f"📅 Перенесено на 1 день: «{row['title']}»\nНовое время: {new_dt.strftime('%d.%m.%Y %H:%M')}"
        )

    await query.answer()


# ---------- ФОНОВАЯ ПРОВЕРКА НАПОМИНАНИЙ ----------

LABELS_RU = {
    "12h": "через 12 часов",
    "2h": "через 2 часа",
    "1h": "через 1 час",
    "15m": "через 15 минут",
}


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    due = get_due_reminders()
    for row, label, field in due:
        dt = datetime.fromisoformat(row["event_time"])
        text = (
            f"🔔 Напоминание ({LABELS_RU[label]}):\n"
            f"«{row['title']}»\n"
            f"{dt.strftime('%d.%m.%Y %H:%M')}"
        )
        for chat_id in ALLOWED_CHAT_IDS:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reminder_keyboard(row["id"]),
            )
        mark_reminded(row["id"], field)


# ---------- ЗАПУСК ----------

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_event))
    app.add_handler(CommandHandler("list", list_events))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Проверка напоминаний каждую минуту
    app.job_queue.run_repeating(check_reminders, interval=60, first=5)

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
