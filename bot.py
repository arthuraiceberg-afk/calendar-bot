import os
import json
import sqlite3
import logging
import requests
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Список разрешённых chat_id через запятую, например: "111111,222222,333333"
# Узнать свой chat_id просто: напиши боту @userinfobot, он пришлёт число.
ALLOWED_CHAT_IDS = [
    int(cid.strip()) for cid in os.environ["ALLOWED_CHAT_IDS"].split(",") if cid.strip()
]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Часовой пояс для ежедневной сводки и распознавания "сегодня/завтра"
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Istanbul"))
# Во сколько присылать утреннюю сводку (24-часовой формат, локальное время)
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "9"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))

DB_PATH = "events.db"

REMINDER_OFFSETS = [
    ("24h", timedelta(hours=24)),
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
            reminded_24h INTEGER DEFAULT 0,
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
           reminded_24h = 0, reminded_12h = 0, reminded_2h = 0, reminded_1h = 0, reminded_15m = 0
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


def now_local():
    """Текущее время в часовом поясе пользователя (наивное, без tzinfo,
    чтобы корректно сравниваться с event_time, который тоже хранится наивно
    и подразумевает локальный часовой пояс)."""
    return datetime.now(TIMEZONE).replace(tzinfo=None)


def get_due_reminders():
    """Находит события, для которых пора слать напоминание."""
    conn = db()
    rows = conn.execute("SELECT * FROM events WHERE status = 'active'").fetchall()
    conn.close()
    now = now_local()
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

def parse_with_claude(text: str):
    """
    Отправляет свободный текст в Claude API, получает обратно
    title и event_time (ISO). Возвращает (title, datetime) или None,
    если Claude не смог распознать дату/время в тексте.
    """
    now = datetime.now(TIMEZONE)
    system_prompt = (
        f"Сегодня {now.strftime('%d.%m.%Y')} ({now.strftime('%A')}), "
        f"текущее время {now.strftime('%H:%M')}, часовой пояс {TIMEZONE.key}.\n"
        "Пользователь пишет сообщение с описанием события (на русском языке), "
        "которое нужно превратить в напоминание. Извлеки короткое название события "
        "и точные дату и время. Если время не указано явно, выбери разумное "
        "(например, 'утром' = 09:00, 'днём' = 14:00, 'вечером' = 19:00). "
        "Если дата не указана явно, но описание не похоже на событие "
        "(нет ни даты, ни времени, ни намёка на 'завтра/сегодня/в пятницу' и т.п.), "
        "верни valid=false.\n\n"
        "Ответь СТРОГО в формате JSON, без пояснений и без markdown:\n"
        '{"valid": true, "title": "...", "datetime": "ДД.ММ.ГГГГ ЧЧ:ММ"}\n'
        "или\n"
        '{"valid": false}'
    )

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "system": system_prompt,
                "messages": [{"role": "user", "content": text}],
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = "".join(
            block["text"] for block in data["content"] if block["type"] == "text"
        ).strip()
        raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(raw_text)

        if not parsed.get("valid"):
            return None

        title = parsed["title"]
        dt = datetime.strptime(parsed["datetime"], "%d.%m.%Y %H:%M")
        return title, dt

    except Exception as e:
        logger.error(f"Ошибка распознавания через Claude API: {e}")
        return None



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
        "Можешь просто написать обычным текстом, например:\n"
        "«созвон с Гузель завтра в 15»\n\n"
        "Или строгим форматом:\n"
        "/new Название | ДД.ММ.ГГГГ | ЧЧ:ММ\n\n"
        "Посмотреть активные события: /list\n"
        f"Каждое утро в {DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d} пришлю сводку на сегодня."
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

    if dt <= now_local():
        await update.message.reply_text("Эта дата/время уже в прошлом, проверь ввод.")
        return

    eid = add_event(title, dt)
    await update.message.reply_text(
        f"✅ Событие добавлено (#{eid}):\n«{title}»\n{dt.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Напомню за 24ч, 12ч, 2ч, 1ч и 15 минут."
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


async def free_text_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает обычные сообщения (без команды) — пробует распознать событие через Claude."""
    if not await check_auth(update):
        return
    text = update.message.text.strip()
    if not text:
        return

    result = parse_with_claude(text)
    if result is None:
        await update.message.reply_text(
            "Не смог распознать в этом сообщении событие с датой/временем.\n"
            "Попробуй яснее, например: «созвон с Гузель завтра в 15»\n"
            "Или используй строгий формат: /new Название | ДД.ММ.ГГГГ | ЧЧ:ММ"
        )
        return

    title, dt = result
    if dt <= now_local():
        await update.message.reply_text(
            f"Распознал «{title}» на {dt.strftime('%d.%m.%Y %H:%M')} — но это уже в прошлом, проверь формулировку."
        )
        return

    eid = add_event(title, dt)
    await update.message.reply_text(
        f"✅ Событие добавлено (#{eid}):\n«{title}»\n{dt.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Напомню за 24ч, 12ч, 2ч, 1ч и 15 минут."
    )


# ---------- КНОПКИ НАПОМИНАНИЙ ----------

def reminder_keyboard(eid):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 Принято", callback_data=f"ack:{eid}"),
            ],
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

    if action == "ack":
        dt = datetime.fromisoformat(row["event_time"])
        await query.edit_message_text(
            f"👍 Принято, встреча в силе:\n«{row['title']}»\n{dt.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=reminder_keyboard(eid),
        )

    elif action == "done":
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
    "24h": "через 24 часа",
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


async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    today = now.date()

    rows = list_active_events()
    today_rows = [
        r for r in rows
        if datetime.fromisoformat(r["event_time"]).date() == today
    ]

    if not today_rows:
        text = "📋 На сегодня событий нет."
    else:
        lines = ["📋 События на сегодня:"]
        for r in today_rows:
            dt = datetime.fromisoformat(r["event_time"])
            lines.append(f"• {dt.strftime('%H:%M')} — {r['title']} (#{r['id']})")
        text = "\n".join(lines)

    for chat_id in ALLOWED_CHAT_IDS:
        await context.bot.send_message(chat_id=chat_id, text=text)


# ---------- ЗАПУСК ----------

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_event))
    app.add_handler(CommandHandler("list", list_events))
    app.add_handler(CallbackQueryHandler(button_handler))
    # Любое обычное сообщение (не команда) — пробуем распознать как событие
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_event))

    # Проверка напоминаний каждую минуту
    app.job_queue.run_repeating(check_reminders, interval=60, first=5)

    # Ежедневная сводка утром
    app.job_queue.run_daily(
        daily_summary,
        time=time(hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE, tzinfo=TIMEZONE),
    )

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
