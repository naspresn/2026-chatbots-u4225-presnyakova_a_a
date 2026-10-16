"""
Remindly — личный Telegram-ассистент для задач.

Возможности:
  • добавление задач (пошаговый диалог /add или просто текстом): приоритет, срок, категория;
  • управление категориями /categories: число задач, переименование и удаление своих категорий;
  • редактирование задач /edit: текст, приоритет, срок, категория;
  • списки /list (с фильтрами) и /today, поиск /find, история выполненных /history
    (с очисткой: задачи пропадают из истории и поиска, но остаются в статистике);
  • напоминания о задачах и утренняя сводка (каждый день или только в рабочие дни);
  • выгрузка /export и загрузка /import задач в CSV-файле, который открывается в Excel;
  • производственный календарь isdayoff.ru: срок «через N рабочих дней», /workdays;
  • статистика выполнения;
  • отзывы /feedback (бот сам предлагает оставить отзыв через 2 дня) и обезличенная
    статистика использования для администратора /admin.

Данные хранятся в базе SQLite (по умолчанию tasks.db рядом с ботом).
Задачи из старого tasks.json переносятся в базу автоматически при первом запуске.
Часовой пояс — Europe/Moscow.
"""

import calendar
import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
import warnings
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import KeyboardButtonStyle
from telegram.error import BadRequest, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

load_dotenv()  # читаем переменные из файла .env

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_DIR = Path(__file__).resolve().parent


def data_path(env_name: str, default: str) -> Path:
    """Путь из .env. Относительный путь считаем от папки бота, а не от текущей папки."""
    path = Path(os.getenv(env_name) or default)
    return path if path.is_absolute() else BASE_DIR / path


# База SQLite. На Railway база должна лежать на подключённом диске (volume): остальные файлы
# сервиса стираются при каждом обновлении бота. Railway сообщает путь к диску в переменной
# RAILWAY_VOLUME_MOUNT_PATH — если DB_FILE не задан, база сама кладётся туда.
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_FILE = data_path("DB_FILE", f"{VOLUME_PATH}/tasks.db" if VOLUME_PATH else "tasks.db")
JSON_FILE = data_path("DATA_FILE", "tasks.json")  # старый файл: переносится в базу при первом запуске
CALENDAR_URL = os.getenv("CALENDAR_URL") or "https://isdayoff.ru/api/getdata"
CALENDAR_TIMEOUT = 5  # сколько секунд ждать ответа производственного календаря

# Администратор (обычно это вы): ему приходят отзывы и доступна статистика /admin.
# В .env: ADMIN_ID=123456789 (свой ID можно узнать командой /myid). Можно несколько через запятую.
ADMIN_ID_RAW = os.getenv("ADMIN_ID") or ""
ADMIN_IDS = {int(part) for part in ADMIN_ID_RAW.replace(";", ",").split(",") if part.strip().isdigit()}
# --- Webhook (на Railway) ---
# Если у бота есть публичный адрес, Telegram сам присылает на него сообщения (webhook).
# Адрес берётся из WEBHOOK_URL, а на Railway — из RAILWAY_PUBLIC_DOMAIN: Railway задаёт её сам,
# когда у сервиса есть домен (Settings → Networking → Generate Domain).
# Адреса нет (например, на вашем компьютере) — бот, как раньше, сам забирает сообщения
# у Telegram (long polling).
PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
WEBHOOK_PATH = "telegram"  # путь приёма: https://<домен>/telegram
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or (f"https://{PUBLIC_DOMAIN}" if PUBLIC_DOMAIN else "")).rstrip("/")
WEBHOOK_URL = WEBHOOK_URL.removesuffix(f"/{WEBHOOK_PATH}")  # если путь уже вписали в адрес
PORT_RAW = os.getenv("PORT") or "8080"  # порт веб-сервера бота; на Railway PORT задаёт сам Railway
# Секрет, по которому бот узнаёт, что запрос пришёл от Telegram, а не от постороннего.
# Получается из токена, поэтому отдельно задавать его не нужно (Telegram разрешает буквы и цифры).
WEBHOOK_SECRET = hashlib.sha256(f"remindly-webhook:{BOT_TOKEN}".encode()).hexdigest()[:32]

TZ = ZoneInfo("Europe/Moscow")
DEFAULT_DIGEST_TIME = "09:00"
MAX_TASK_LENGTH = 500
DIALOG_TIMEOUT = 10 * 60  # диалог без ответа завершается через 10 минут

# Railway задаёт RAILWAY_ENVIRONMENT_NAME всем своим сервисам — так узнаём, что бот на сервере
ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT_NAME"))


class JsonLogFormatter(logging.Formatter):
    """Одна запись лога — одна строка JSON с полем level. По нему панель Railway
    раскрашивает логи: info обычным цветом, warning жёлтым, error красным."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:  # трейсбек ошибки — в ту же строку, иначе Railway разобьёт его
            message += "\n" + self.formatException(record.exc_info)
        return json.dumps({"level": record.levelname.lower(), "logger": record.name,
                           "message": message}, ensure_ascii=False)


# Логи пишем в консоль, в stdout: Railway показывает их в панели Deployments → Logs.
# Не в stderr (как по умолчанию в Python): всё из stderr Railway помечает как ошибки.
# Локально — привычный текст со временем, на Railway — JSON (время Railway ставит сам).
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(JsonLogFormatter() if ON_RAILWAY else
                          logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logging.getLogger("httpx").setLevel(logging.WARNING)  # не засоряем лог HTTP-запросами
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # и сообщениями планировщика
logging.getLogger("tornado.access").setLevel(logging.WARNING)  # и строкой на каждый запрос webhook
logger = logging.getLogger("remindly")

# ConversationHandler предупреждает о CallbackQueryHandler при per_message=False.
# Для нашего сценария (один диалог на пользователя) это нормально.
warnings.filterwarnings("ignore", message=r".*per_message.*", category=PTBUserWarning)

# Приоритеты: ключ хранится в файле, остальное — для отображения и сортировки
PRIORITIES = {
    "high": {"emoji": "🔴", "name": "высокий", "button": "🔴 Высокий", "order": 0},
    "medium": {"emoji": "🟡", "name": "средний", "button": "🟡 Средний", "order": 1},
    "low": {"emoji": "🟢", "name": "низкий", "button": "🟢 Низкий", "order": 2},
}

# Базовые категории есть у всех; свои категории пользователя хранятся в базе
BASE_CATEGORIES = ["Рабочие", "Бытовые", "Учебные", "Личные"]
MAX_CATEGORY_LENGTH = 30

# Состояния диалогов: добавление задачи (с категорией и сроком в рабочих днях),
# подтверждение удаления, выбор задачи кнопками, выбор напоминания и вопрос «что найти?»
(CONFIRM, TEXT, PRIORITY, DUE, DATE_INPUT, DELETE_CONFIRM,
 PICK_TASK, REMIND_OPTION, REMIND_TIME, DUPLICATE,
 CATEGORY, WORKDAYS_INPUT, FIND_QUERY,
 EDIT_FIELD, HISTORY_SCOPE, HISTORY_CONFIRM,
 CAT_ACTION, CAT_RENAME, CAT_DELETE_CONFIRM,
 FB_RATING, FB_TEXT, FB_WOULD_USE, DIGEST_TIME) = range(23)

# Данные текущего диалога в context.user_data — всё это забывается при его завершении
DIALOG_KEYS = ("draft", "delete_id", "pick", "remind_id", "remind_after_add", "remind_prefix",
               "edit", "clear_scope", "cat_edit", "feedback", "card", "list_view")


# ---------------------------------------------------------------------------
# Хранилище: база SQLite
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    next_id        INTEGER NOT NULL DEFAULT 1,      -- следующий номер задачи
    digest_enabled INTEGER NOT NULL DEFAULT 1,      -- 1 — утренняя сводка включена
    digest_time    TEXT    NOT NULL DEFAULT '09:00',
    digest_mode    TEXT    NOT NULL DEFAULT 'daily', -- 'daily' или 'workdays'
    started_at     TEXT,                            -- когда начал пользоваться ботом
    last_seen      TEXT,                            -- последняя активность
    feedback_offered INTEGER NOT NULL DEFAULT 0     -- 1 — бот уже предлагал оставить отзыв
);

CREATE TABLE IF NOT EXISTS tasks (
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    id          INTEGER NOT NULL,                   -- номер задачи у пользователя: 1, 2, 3…
    text        TEXT    NOT NULL,
    priority    TEXT    NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    category    TEXT,                               -- NULL — без категории
    due         TEXT,                               -- срок, ГГГГ-ММ-ДД
    remind_at   TEXT,                               -- время напоминания, ISO
    remind_note TEXT,                               -- выбранный вариант: «за сутки до срока»
    status      TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
    created_at  TEXT    NOT NULL,
    done_at     TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,         -- 1 — убрана из истории (в /stats учитывается)
    PRIMARY KEY (user_id, id)
);

-- Свои категории пользователя (базовые — в коде, в BASE_CATEGORIES)
CREATE TABLE IF NOT EXISTS categories (
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name    TEXT    NOT NULL,
    PRIMARY KEY (user_id, name)
);

-- Отзывы из /feedback
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    liked      TEXT,                                -- NULL — вопрос пропущен
    disliked   TEXT,
    improve    TEXT,
    would_use  TEXT    NOT NULL CHECK (would_use IN ('yes', 'maybe', 'no'))
);

-- Обезличенная статистика: какие команды вызывали (без текстов задач и сообщений)
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    action  TEXT    NOT NULL,                       -- команда: add, list… или quick_add
    source  TEXT    NOT NULL,                       -- slash — со слешем, text — обычным текстом
    used_at TEXT    NOT NULL
);

-- Служебные отметки, например «задачи из tasks.json уже перенесены»
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Поля задачи. По коду задача ходит как обычный словарь с этими ключами
TASK_FIELDS = ("id", "text", "priority", "category", "due", "remind_at", "remind_note",
               "status", "created_at", "done_at")
TASK_COLUMNS = ", ".join(TASK_FIELDS)
# При чтении задачи берём ещё пометку archived. Записывает её только archive_tasks,
# поэтому в TASK_FIELDS (вставка и сохранение задачи) её нет.
TASK_READ_COLUMNS = TASK_COLUMNS + ", archived"

# Колонки, появившиеся в новых версиях бота: в базу от прошлой версии их добавит
# Database._add_missing_columns (CREATE TABLE IF NOT EXISTS существующую таблицу не меняет)
NEW_COLUMNS = {
    "users": {"digest_mode": "TEXT NOT NULL DEFAULT 'daily'", "started_at": "TEXT",
              "last_seen": "TEXT", "feedback_offered": "INTEGER NOT NULL DEFAULT 0"},
    "tasks": {"archived": "INTEGER NOT NULL DEFAULT 0"},
}


class Database:
    """Все запросы к базе SQLite — только в этом классе.

    Схема работы в остальном коде такая же простая, как раньше с JSON:
    загрузили пользователя вместе с задачами (get_user) → поменяли поля задачи →
    сохранили её (save_task). Каждое изменение сразу записывается в базу.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)  # например, /data на диске Railway
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row  # строки результата можно читать как row["text"]
        self.conn.execute("PRAGMA foreign_keys = ON")
        # norm() — для поиска без учёта регистра и «ё». Встроенная LOWER() в SQLite
        # понимает только латиницу, поэтому подключаем свою функцию из Python.
        self.conn.create_function("norm", 1, lambda s: normalize(s) if s else "",
                                  deterministic=True)
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Обновляет базу, созданную прошлой версией бота: добавляет новые колонки.
        Данные при этом сохраняются, новые колонки получают значения по умолчанию."""
        for table, columns in NEW_COLUMNS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    logger.info("База обновлена: в таблицу %s добавлена колонка %s", table, name)
        self.conn.commit()
        self._fill_started_at()

    def _fill_started_at(self) -> None:
        """Начало использования для тех, у кого его нет (база прошлой версии или перенос
        из tasks.json): дата первой задачи, а если задач нет — сейчас."""
        with self.conn:
            self.conn.execute(
                "UPDATE users SET started_at = COALESCE("
                "(SELECT MIN(created_at) FROM tasks WHERE tasks.user_id = users.user_id), ?) "
                "WHERE started_at IS NULL", (datetime.now(TZ).isoformat(timespec="seconds"),))

    # --- Служебные отметки -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # --- Пользователи ---------------------------------------------------------------

    def find_user(self, user_id: int) -> dict | None:
        """Пользователь вместе со всеми задачами или None, если его ещё нет."""
        row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return self._user_from_row(row) if row else None

    def get_user(self, user_id: int) -> tuple[dict, bool]:
        """Пользователь (создаётся при первом обращении) и флаг «только что создан»."""
        user = self.find_user(user_id)
        if user:
            return user, False
        with self.conn:  # with — транзакция: при ошибке изменения откатятся
            current = now().isoformat(timespec="seconds")
            self.conn.execute(
                "INSERT INTO users (user_id, chat_id, digest_time, started_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, user_id, DEFAULT_DIGEST_TIME, current, current),  # chat_id = user_id
            )
        return self.find_user(user_id), True

    def all_users(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM users").fetchall()
        return [self._user_from_row(row) for row in rows]

    def save_user(self, user: dict) -> None:
        """Сохраняет chat_id и настройки сводки."""
        settings = user["settings"]
        with self.conn:
            self.conn.execute(
                "UPDATE users SET chat_id = ?, digest_enabled = ?, digest_time = ?, digest_mode = ? "
                "WHERE user_id = ?",
                (user["chat_id"], int(settings["digest_enabled"]), settings["digest_time"],
                 settings["digest_mode"], user["user_id"]),
            )

    def _user_from_row(self, row: sqlite3.Row) -> dict:
        return {
            "user_id": row["user_id"],
            "chat_id": row["chat_id"],
            "next_id": row["next_id"],
            "settings": {"digest_enabled": bool(row["digest_enabled"]),
                         "digest_time": row["digest_time"],
                         "digest_mode": row["digest_mode"]},
            "tasks": self.tasks(row["user_id"]),
        }

    # --- Задачи ------------------------------------------------------------------------

    def tasks(self, user_id: int) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT {TASK_READ_COLUMNS} FROM tasks WHERE user_id = ? ORDER BY id", (user_id,))
        return [dict(row) for row in rows]

    def add_task(self, user_id: int, **fields) -> dict:
        """Новая задача со следующим номером пользователя (номер не переиспользуется)."""
        with self.conn:
            return self._insert_task(user_id, **fields)

    def import_tasks(self, user_id: int, tasks: list[dict]) -> list[dict]:
        """Добавляет много задач в одной транзакции: либо все, либо ни одной."""
        with self.conn:
            return [self._insert_task(user_id, **fields) for fields in tasks]

    def _insert_task(self, user_id: int, text: str, priority: str, due: str | None = None,
                     category: str | None = None, status: str = "active",
                     created_at: str | None = None, done_at: str | None = None) -> dict:
        """Вставка без commit — вызывается внутри транзакции add_task или import_tasks."""
        row = self.conn.execute("SELECT next_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        task = {
            "id": row["next_id"],
            "text": text,
            "priority": priority,
            "category": category,
            "due": due,
            "remind_at": None,
            "remind_note": None,
            "status": status,
            "created_at": created_at or now().isoformat(timespec="seconds"),
            "done_at": done_at,
        }
        placeholders = ", ".join("?" for _ in TASK_FIELDS)
        self.conn.execute(f"INSERT INTO tasks (user_id, {TASK_COLUMNS}) VALUES (?, {placeholders})",
                          (user_id, *(task[f] for f in TASK_FIELDS)))
        self.conn.execute("UPDATE users SET next_id = next_id + 1 WHERE user_id = ?", (user_id,))
        if category:
            self._remember_category(user_id, category)
        return {**task, "archived": 0}

    def save_task(self, user_id: int, task: dict) -> None:
        """Записывает в базу все поля задачи (после того как их поменяли в словаре)."""
        fields = [f for f in TASK_FIELDS if f != "id"]
        assignments = ", ".join(f"{f} = ?" for f in fields)
        with self.conn:
            self.conn.execute(f"UPDATE tasks SET {assignments} WHERE user_id = ? AND id = ?",
                              (*(task.get(f) for f in fields), user_id, task["id"]))

    def delete_task(self, user_id: int, task_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tasks WHERE user_id = ? AND id = ?", (user_id, task_id))

    def search_tasks(self, user_id: int, words: list[str]) -> list[dict]:
        """Задачи, в тексте или категории которых есть все слова (без учёта регистра и «ё»).
        Задачи, убранные из истории, не ищутся."""
        sql = f"SELECT {TASK_READ_COLUMNS} FROM tasks WHERE user_id = ? AND archived = 0"
        for _ in words:
            sql += " AND instr(norm(text || ' ' || coalesce(category, '')), ?) > 0"
        rows = self.conn.execute(sql, (user_id, *words))
        return [dict(row) for row in rows]

    def done_history(self, user_id: int, limit: int) -> list[dict]:
        """Последние выполненные задачи (кроме убранных из истории), самые свежие первыми."""
        rows = self.conn.execute(
            f"SELECT {TASK_READ_COLUMNS} FROM tasks WHERE user_id = ? AND status = 'done' "
            "AND archived = 0 ORDER BY done_at DESC, id DESC LIMIT ?", (user_id, limit))
        return [dict(row) for row in rows]

    def archive_tasks(self, user_id: int, task_ids: list[int]) -> None:
        """Убирает задачи из истории и поиска. Из базы они не удаляются — /stats их считает."""
        with self.conn:
            self.conn.executemany("UPDATE tasks SET archived = 1 WHERE user_id = ? AND id = ?",
                                  [(user_id, task_id) for task_id in task_ids])

    # --- Категории ---------------------------------------------------------------------

    def custom_categories(self, user_id: int) -> list[str]:
        """Свои категории пользователя в порядке создания."""
        rows = self.conn.execute(
            "SELECT name FROM categories WHERE user_id = ? ORDER BY rowid", (user_id,))
        return [row["name"] for row in rows]

    def custom_categories_with_ids(self, user_id: int) -> list[tuple[int, str]]:
        """Свои категории с номером строки (rowid) — его удобно класть в кнопку."""
        rows = self.conn.execute(
            "SELECT rowid, name FROM categories WHERE user_id = ? ORDER BY rowid", (user_id,))
        return [(row["rowid"], row["name"]) for row in rows]

    def custom_category_name(self, user_id: int, rowid: int) -> str | None:
        row = self.conn.execute("SELECT name FROM categories WHERE user_id = ? AND rowid = ?",
                                (user_id, rowid)).fetchone()
        return row["name"] if row else None

    def add_category(self, user_id: int, name: str) -> None:
        with self.conn:
            self._remember_category(user_id, name)

    def rename_category(self, user_id: int, old: str, new: str) -> int:
        """Переименовывает свою категорию и во всех задачах. Возвращает число изменённых задач."""
        with self.conn:
            self.conn.execute("UPDATE categories SET name = ? WHERE user_id = ? AND name = ?",
                              (new, user_id, old))
            cursor = self.conn.execute(
                "UPDATE tasks SET category = ? WHERE user_id = ? AND norm(category) = ?",
                (new, user_id, normalize(old)))
            return cursor.rowcount

    def delete_category(self, user_id: int, name: str) -> int:
        """Удаляет свою категорию; её задачи остаются без категории. Возвращает их число."""
        with self.conn:
            self.conn.execute("DELETE FROM categories WHERE user_id = ? AND name = ?", (user_id, name))
            cursor = self.conn.execute(
                "UPDATE tasks SET category = NULL WHERE user_id = ? AND norm(category) = ?",
                (user_id, normalize(name)))
            return cursor.rowcount

    def _remember_category(self, user_id: int, name: str) -> None:
        """Запоминает свою категорию. Базовые и уже известные (без учёта регистра) не дублируем."""
        known = {normalize(c) for c in BASE_CATEGORIES + self.custom_categories(user_id)}
        if normalize(name) not in known:
            self.conn.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, name))

    # --- Статистика использования и отзывы -------------------------------------------

    def touch_user(self, user_id: int) -> None:
        """Отмечает последнюю активность (пользователя ещё нет в базе — ничего не делает)."""
        with self.conn:
            self.conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?",
                              (now().isoformat(timespec="seconds"), user_id))

    def log_usage(self, user_id: int, action: str, source: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO usage (user_id, action, source, used_at) VALUES (?, ?, ?, ?)",
                              (user_id, action, source, now().isoformat(timespec="seconds")))

    def add_feedback(self, user_id: int, rating: int, liked: str | None, disliked: str | None,
                     improve: str | None, would_use: str) -> int:
        """Сохраняет отзыв и возвращает его номер. После отзыва бот сам его больше не предлагает."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO feedback (user_id, created_at, rating, liked, disliked, improve, would_use) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, now().isoformat(timespec="seconds"), rating, liked, disliked, improve, would_use))
            self.conn.execute("UPDATE users SET feedback_offered = 1 WHERE user_id = ?", (user_id,))
            return cursor.lastrowid

    def all_feedback(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM feedback ORDER BY id")]

    def participant_numbers(self) -> dict[int, int]:
        """Telegram ID -> «номер участника» по порядку начала использования.
        Администратор видит только номер, а не ID или имя."""
        rows = self.conn.execute("SELECT user_id FROM users ORDER BY started_at, user_id")
        return {row["user_id"]: number for number, row in enumerate(rows, start=1)}

    def users_due_for_feedback(self, started_before: str) -> list[tuple[int, int]]:
        """(user_id, chat_id) тех, кто начал пользоваться ботом раньше started_before,
        ещё не получал предложения оставить отзыв и сам его не оставлял."""
        rows = self.conn.execute(
            "SELECT user_id, chat_id FROM users WHERE feedback_offered = 0 AND started_at <= ? "
            "AND NOT EXISTS (SELECT 1 FROM feedback WHERE feedback.user_id = users.user_id)",
            (started_before,))
        return [(row["user_id"], row["chat_id"]) for row in rows]

    def mark_feedback_offered(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET feedback_offered = 1 WHERE user_id = ?", (user_id,))

    def admin_stats(self, since: str) -> dict:
        """Обезличенные числа для /admin. since — начало недели (ISO-время)."""
        def one(sql: str, *args):
            return self.conn.execute(sql, args).fetchone()[0]
        usage = self.conn.execute(
            "SELECT action, COUNT(*) AS total, SUM(source = 'text') AS by_text FROM usage "
            "GROUP BY action ORDER BY total DESC, action")
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "active_week": one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", since),
            # next_id растёт с каждой новой задачей и не уменьшается при удалении,
            # поэтому так учитываются и задачи, которые потом удалили
            "created": one("SELECT COALESCE(SUM(next_id - 1), 0) FROM users"),
            "created_week": one("SELECT COUNT(*) FROM tasks WHERE created_at >= ?", since),
            "done": one("SELECT COUNT(*) FROM tasks WHERE status = 'done'"),
            "done_week": one("SELECT COUNT(*) FROM tasks WHERE status = 'done' AND done_at >= ?", since),
            "ratings": [row[0] for row in self.conn.execute("SELECT rating FROM feedback")],
            "would_use": {row[0]: row[1] for row in self.conn.execute(
                "SELECT would_use, COUNT(*) FROM feedback GROUP BY would_use")},
            "usage": [(row["action"], row["total"], row["by_text"]) for row in usage],
        }

    # --- Перенос из tasks.json ---------------------------------------------------------

    def migrate_from_json(self, path: Path) -> None:
        """Однократно переносит пользователей, настройки и задачи из старого tasks.json.

        После переноса в таблицу meta ставится отметка, чтобы перенос не повторился,
        а файл переименовывается в tasks.json.migrated — данные из него не удаляются.
        """
        if self.get_meta("json_migrated") or not path.exists():
            return
        try:
            users = json.loads(path.read_text(encoding="utf-8"))["users"]
            moved = skipped = 0
            with self.conn:  # одна транзакция: при сбое в базу не попадёт половина данных
                for key, user in users.items():
                    user_id = int(key)
                    settings = user.get("settings", {})
                    tasks = user.get("tasks", [])
                    # следующий номер — больше всех существующих, чтобы номера не повторялись
                    next_id = max([user.get("next_id", 1)] + [t.get("id", 0) + 1 for t in tasks])
                    self.conn.execute(
                        "INSERT OR IGNORE INTO users (user_id, chat_id, next_id, digest_enabled, "
                        "digest_time) VALUES (?, ?, ?, ?, ?)",
                        (user_id, user.get("chat_id", user_id), next_id,
                         int(settings.get("digest_enabled", True)),
                         settings.get("digest_time", DEFAULT_DIGEST_TIME)),
                    )
                    for task in tasks:
                        values = {f: task.get(f) for f in TASK_FIELDS}
                        values["status"] = values["status"] or "active"
                        values["created_at"] = values["created_at"] or now().isoformat(timespec="seconds")
                        placeholders = ", ".join("?" for _ in TASK_FIELDS)
                        try:
                            self.conn.execute(
                                f"INSERT INTO tasks (user_id, {TASK_COLUMNS}) VALUES (?, {placeholders})",
                                (user_id, *(values[f] for f in TASK_FIELDS)))
                            moved += 1
                        except sqlite3.IntegrityError as e:  # повреждённая запись или повтор номера
                            logger.warning("Задача %s пользователя %s не перенесена: %s",
                                           task.get("id"), user_id, e)
                            skipped += 1
                self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                  ("json_migrated", now().isoformat(timespec="seconds")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as e:
            logger.error("Не удалось перенести задачи из %s: %s. Файл оставлен как есть.", path, e)
            return
        self._fill_started_at()
        logger.info("Из %s перенесено задач: %d, пропущено: %d", path.name, moved, skipped)
        try:
            path.rename(path.with_name(path.name + ".migrated"))
        except OSError as e:
            logger.warning("Не удалось переименовать %s: %s", path, e)


try:
    db = Database(DB_FILE)
except (sqlite3.Error, OSError) as e:
    raise SystemExit(f"Не удалось открыть базу {DB_FILE}: {e}") from e


# ---------------------------------------------------------------------------
# Вспомогательные функции: время, разбор ввода, форматирование
# ---------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def parse_date(text: str) -> date | None:
    """«14.10» или «14.10.2026» -> date.

    Без года выбираем дату, ближайшую к сегодняшнему дню, — в этом или следующем году.
    Например, 20 декабря «10.01» — это 10 января следующего года,
    а 12 октября «01.10» — 1 октября этого года (уже прошло).
    """
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text.strip())
    if not m:
        return None
    day, month = int(m[1]), int(m[2])
    years = [int(m[3])] if m[3] else [today().year, today().year + 1]
    candidates = []
    for year in years:
        try:
            candidates.append(date(year, month, day))
        except ValueError:  # например, 31.02 или 29.02 в невисокосный год
            pass
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - today()).days))


def parse_time(text: str) -> time | None:
    """«18:30» -> time."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2])
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def fmt_date(d: date) -> str:
    """Дата коротко: «14.10», а для другого года — «14.10.2027»."""
    return d.strftime("%d.%m") if d.year == today().year else d.strftime("%d.%m.%Y")


def fmt_when(dt: datetime) -> str:
    """Момент напоминания по-человечески: «сегодня в 18:30», «завтра в 09:00», «14.10 в 18:30»."""
    d = dt.date()
    if d == today():
        day = "сегодня"
    elif d == today() + timedelta(days=1):
        day = "завтра"
    else:
        day = fmt_date(d)
    return f"{day} в {dt.strftime('%H:%M')}"


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                   "сентября", "октября", "ноября", "декабря"]
MONTHS_PREPOSITIONAL = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе",
                        "сентябре", "октябре", "ноябре", "декабре"]


def fmt_day(d: date) -> str:
    """Дата с днём недели: «14.10 (ср)»."""
    return f"{fmt_date(d)} ({WEEKDAYS[d.weekday()]})"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Слово в нужной форме для числа: 1 задача, 2 задачи, 5 задач."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


MESSAGE_LIMIT = 3800  # Telegram принимает до 4096 символов в сообщении — оставляем запас


def join_lines(header: str, lines: list[str]) -> str:
    """Заголовок и строки списка. Если всё не помещается в одно сообщение — обрезаем с пометкой."""
    text = header
    for i, line in enumerate(lines):
        if len(text) + len(line) + 30 > MESSAGE_LIMIT:
            return f"{text}\n… и ещё {len(lines) - i}"
        text += f"\n{line}"
    return text


def task_due(task: dict) -> date | None:
    return date.fromisoformat(task["due"]) if task.get("due") else None


def is_overdue(task: dict) -> bool:
    due = task_due(task)
    return task["status"] == "active" and due is not None and due < today()


def task_line(task: dict, show_id: bool = True) -> str:
    """Строка задачи: «#3 🔴 Подготовить отчёт · 🏷 Учебные · до 14.10 ⚠️ просрочено»."""
    line = f"{PRIORITIES[task['priority']]['emoji']} {task['text']}"
    if show_id:
        line = f"#{task['id']} {line}"
    if task.get("category"):
        line += f" · 🏷 {task['category']}"
    due = task_due(task)
    if due:
        line += f" · до {fmt_date(due)}"
    if is_overdue(task):
        line += " ⚠️ просрочено"
    return line


def sort_key(task: dict):
    """Порядок: просроченные -> приоритет (высокий первым) -> срок (без срока в конце) -> номер."""
    return (
        0 if is_overdue(task) else 1,
        PRIORITIES[task["priority"]]["order"],
        task_due(task) or date.max,
        task["id"],
    )


def active_tasks(user: dict) -> list[dict]:
    return sorted((t for t in user["tasks"] if t["status"] == "active"), key=sort_key)


def today_tasks(user: dict) -> list[dict]:
    """Активные задачи со сроком на сегодня и просроченные."""
    return [t for t in active_tasks(user) if task_due(t) and task_due(t) <= today()]


def find_task(user: dict, task_id: int) -> dict | None:
    return next((t for t in user["tasks"] if t["id"] == task_id), None)


def parse_task_id(arg: str) -> int | None:
    """«3» или «#3» -> 3."""
    arg = arg.lstrip("#")
    return int(arg) if arg.isdigit() else None


def get_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    """Данные пользователя. Для нового пользователя сразу включаем ежедневную сводку."""
    user, created = db.get_user(user_id)
    if created:
        schedule_digest(context.job_queue, user_id, user)
    return user


# ---------------------------------------------------------------------------
# Планировщик: напоминания и ежедневная сводка
# ---------------------------------------------------------------------------

def reminder_job_name(user_id: int, task_id: int) -> str:
    return f"remind:{user_id}:{task_id}"


def digest_job_name(user_id: int) -> str:
    return f"digest:{user_id}"


def cancel_jobs(job_queue, name: str) -> None:
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def schedule_reminder(job_queue, user_id: int, task: dict) -> None:
    """Ставит (или переставляет) напоминание по полю task["remind_at"]."""
    name = reminder_job_name(user_id, task["id"])
    cancel_jobs(job_queue, name)
    if not task.get("remind_at") or task["status"] != "active":
        return
    when = datetime.fromisoformat(task["remind_at"])
    # Если напоминание пропущено, пока бот был выключен, — отправим его сразу
    when = max(when, now() + timedelta(seconds=5))
    job_queue.run_once(
        reminder_job, when, name=name, data={"user_id": user_id, "task_id": task["id"]}
    )


def schedule_digest(job_queue, user_id: int, user: dict) -> None:
    """Ставит ежедневную сводку на время из настроек (или снимает, если сводка выключена)."""
    name = digest_job_name(user_id)
    cancel_jobs(job_queue, name)
    settings = user["settings"]
    if not settings.get("digest_enabled"):
        return
    t = parse_time(settings.get("digest_time", DEFAULT_DIGEST_TIME)) or parse_time(DEFAULT_DIGEST_TIME)
    job_queue.run_daily(
        digest_job, t.replace(tzinfo=TZ), name=name, data={"user_id": user_id}
    )


def reminder_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнено", callback_data=f"rdone:{task_id}", style=GREEN),
        InlineKeyboardButton("⏰ Отложить на 1 час", callback_data=f"snooze:{task_id}"),
    ]])


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывание напоминания."""
    user_id = context.job.data["user_id"]
    user = db.find_user(user_id)
    task = find_task(user, context.job.data["task_id"]) if user else None
    if not task or task["status"] != "active":
        return  # задачу успели удалить или выполнить
    task["remind_at"] = None
    task["remind_note"] = None
    db.save_task(user_id, task)
    try:
        await context.bot.send_message(
            user["chat_id"],
            f"⏰ Напоминание\n{task_line(task)}",
            reply_markup=reminder_keyboard(task["id"]),
        )
    except TelegramError as e:  # например, пользователь заблокировал бота
        logger.warning("Не удалось отправить напоминание %s: %s", user_id, e)


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная сводка: задачи на сегодня и просроченные."""
    user_id = context.job.data["user_id"]
    user = db.find_user(user_id)
    if not user:
        return
    if user["settings"]["digest_mode"] == "workdays":
        # Если календарь недоступен, is_day_off считает нерабочими только субботу и воскресенье
        day_off, _ = await is_day_off(today())
        if day_off:
            logger.info("Сводка для %s пропущена: сегодня нерабочий день", user_id)
            return
    tasks = today_tasks(user)
    if tasks:
        text = join_lines("☀️ Доброе утро! Задачи на сегодня:\n", [task_line(t) for t in tasks])
    else:
        text = "☀️ Доброе утро! На сегодня задач нет 🎉"
    try:
        await context.bot.send_message(user["chat_id"], text)
    except TelegramError as e:
        logger.warning("Не удалось отправить сводку %s: %s", user_id, e)


def restore_jobs(application: Application) -> None:
    """После перезапуска восстанавливаем сводки и напоминания из базы."""
    reminders = 0
    users = db.all_users()
    for user in users:
        user_id = user["user_id"]
        schedule_digest(application.job_queue, user_id, user)
        for task in user["tasks"]:
            if task["status"] == "active" and task.get("remind_at"):
                schedule_reminder(application.job_queue, user_id, task)
                reminders += 1
    logger.info("Восстановлено: пользователей %d, напоминаний %d", len(users), reminders)


# ---------------------------------------------------------------------------
# Производственный календарь (isdayoff.ru)
# ---------------------------------------------------------------------------
# Запрос «?year=2026» возвращает строку из 365 (366) цифр — по одной на каждый день года:
# «0» — рабочий день, «1» — выходной или праздник. Ответ кэшируем: календарь на год
# меняется редко, а сервис не стоит дёргать на каждый вопрос.
# Если сервис недоступен, бот не ломается: выходными считает только субботу и воскресенье
# и честно пишет об этом пользователю (CALENDAR_FALLBACK_NOTE).

CALENDAR_CACHE_TTL = timedelta(hours=12)   # сколько хранить загруженный год
CALENDAR_RETRY_AFTER = timedelta(minutes=5)  # после ошибки не повторяем запрос сразу
CALENDAR_FALLBACK_NOTE = ("⚠️ Производственный календарь сейчас недоступен — "
                          "выходными считаю только субботу и воскресенье, праздники не учитываю.")

# год -> (строка «0»/«1» по дням или None, если загрузить не удалось; когда загружали)
_calendar_cache: dict[int, tuple[str | None, datetime]] = {}


async def load_calendar_year(year: int) -> str | None:
    """Дни года из производственного календаря («0» — рабочий, «1» — нерабочий) или None."""
    cached = _calendar_cache.get(year)
    if cached:
        days, loaded_at = cached
        ttl = CALENDAR_CACHE_TTL if days else CALENDAR_RETRY_AFTER
        if now() - loaded_at < ttl:
            return days

    days = None
    try:
        async with httpx.AsyncClient(timeout=CALENDAR_TIMEOUT) as client:
            response = await client.get(CALENDAR_URL, params={"year": year, "cc": "ru"})
        response.raise_for_status()  # 404 — например, на этот год данных ещё нет
        text = response.text.strip()
        expected_length = 366 if calendar.isleap(year) else 365
        if len(text) != expected_length or not text.isdigit():
            raise ValueError(f"неожиданный ответ: {text[:30]!r}")
        days = text
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        # Нет интернета, сервис не отвечает, неверный адрес в .env — бот продолжает работать
        logger.warning("Производственный календарь на %s год недоступен: %s", year, e)
    _calendar_cache[year] = (days, now())
    return days


async def is_day_off(d: date) -> tuple[bool, bool]:
    """(нерабочий ли день, взят ли ответ из производственного календаря).
    Если календарь недоступен, нерабочими считаются только суббота и воскресенье."""
    days = await load_calendar_year(d.year)
    if days is None:
        return d.weekday() >= 5, False
    return days[d.timetuple().tm_yday - 1] == "1", True


async def add_workdays(start: date, count: int) -> tuple[date, bool]:
    """Дата через count рабочих дней после start (сам start не считается).
    Возвращает дату и признак, что все дни взяты из производственного календаря."""
    d, left, from_calendar = start, count, True
    while left > 0:
        d += timedelta(days=1)
        day_off, from_api = await is_day_off(d)
        from_calendar = from_calendar and from_api
        if not day_off:
            left -= 1
    return d, from_calendar


async def day_off_warning(due: date | None) -> str:
    """Предупреждение, если срок выпадает на выходной или праздник; '' — если всё в порядке."""
    if due is None or due < today():
        return ""
    day_off, from_calendar = await is_day_off(due)
    text = ""
    if day_off:
        kind = "выходной день" if due.weekday() >= 5 else "праздник, нерабочий день"
        text += f"\n⚠️ {fmt_day(due)} — {kind}."
    if not from_calendar:
        text += f"\n{CALENDAR_FALLBACK_NOTE}"
    return text


async def workdays_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/workdays — сколько рабочих дней осталось до конца месяца."""
    await update.message.reply_text(await workdays_text())


async def workdays_text() -> str:
    """Сколько рабочих дней осталось до конца месяца (для /workdays и кнопки в «Мои задачи»)."""
    start = today()
    last = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    total = left = 0
    today_off, from_calendar = False, True
    d = start.replace(day=1)
    while d <= last:
        day_off, from_api = await is_day_off(d)
        from_calendar = from_calendar and from_api
        if d == start:
            today_off = day_off
        if not day_off:
            total += 1
            if d >= start:
                left += 1
        d += timedelta(days=1)

    month_gen = MONTHS_GENITIVE[start.month - 1]
    verb = "остался" if plural(left, "1", "2", "5") == "1" else "осталось"
    text = (f"📅 До конца {month_gen} {verb} {left} "
            f"{plural(left, 'рабочий день', 'рабочих дня', 'рабочих дней')}")
    text += " (сегодня выходной)." if today_off else " (включая сегодня)."
    text += (f"\nВсего в {MONTHS_PREPOSITIONAL[start.month - 1]} {total} "
             f"{plural(total, 'рабочий день', 'рабочих дня', 'рабочих дней')}.")
    if not from_calendar:
        text += f"\n\n{CALENDAR_FALLBACK_NOTE}"
    return text


# ---------------------------------------------------------------------------
# Диалоги: добавление задачи (/add, быстрое добавление) и подтверждение /delete
# ---------------------------------------------------------------------------

PRIORITY_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton(p["button"], callback_data=f"prio:{key}") for key, p in PRIORITIES.items()
]])

DUE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Сегодня", callback_data="due:today"),
     InlineKeyboardButton("Завтра", callback_data="due:tomorrow")],
    [InlineKeyboardButton("Без срока", callback_data="due:none"),
     InlineKeyboardButton("Указать дату", callback_data="due:custom")],
    [InlineKeyboardButton("Через N рабочих дней", callback_data="due:workdays")],
])

# Текстовые ответы вместо кнопок: вариант -> значение (сравниваются через match_phrase)
YES_NO_WORDS = {"да": True, "ок": True, "добавить": True, "нет": False, "отмена": False}
DUPLICATE_WORDS = {**YES_NO_WORDS, "создать": True, "сохранить": True}
DELETE_WORDS = {**YES_NO_WORDS, "удалить": True, "оставить": False}
PRIORITY_WORDS = {p["name"]: key for key, p in PRIORITIES.items()}  # «высокий» -> "high"
DUE_WORDS = {"сегодня": "today", "завтра": "tomorrow", "без срока": "none", "указать дату": "custom",
             "через n рабочих дней": "workdays", "рабочие дни": "workdays"}

# Подсказки, если текстовый ответ не распознан
YES_NO_HINT = "Напишите «да» или «нет», либо нажмите кнопку."
PRIORITY_HINT = "Напишите: высокий, средний или низкий, либо нажмите кнопку."
DUE_HINT = ("Напишите: сегодня, завтра, без срока, дату ДД.ММ или «через 5 рабочих дней», "
            "либо нажмите кнопку.")


def normalize(text: str) -> str:
    """Готовит ответ к сравнению: нижний регистр, ё -> е, без лишних пробелов
    и знаков препинания в конце («Привет!» -> «привет»)."""
    return " ".join(text.lower().replace("ё", "е").split()).rstrip(".,!?;:…").strip()


# --- Опечатки ------------------------------------------------------------------

# Раскладка ЙЦУКЕН: ряды клавиш и их сдвиг вправо относительно верхнего ряда
KEYBOARD_ROWS = [("йцукенгшщзхъ", 0.0), ("фывапролджэ", 0.25), ("ячсмитьбю", 0.75)]
KEY_POSITIONS = {
    key: (row, index + shift)
    for row, (keys, shift) in enumerate(KEYBOARD_ROWS)
    for index, key in enumerate(keys)
}


def keys_adjacent(a: str, b: str) -> bool:
    """Соседние ли клавиши: рядом в одном ряду или касаются в соседнем ряду («а» — «п», «а» — «к»)."""
    if a not in KEY_POSITIONS or b not in KEY_POSITIONS:
        return False
    (row_a, x_a), (row_b, x_b) = KEY_POSITIONS[a], KEY_POSITIONS[b]
    if row_a == row_b:
        return abs(x_a - x_b) == 1
    return abs(row_a - row_b) == 1 and abs(x_a - x_b) < 1


def substitution_cost(a: str, b: str) -> int:
    """Цена замены буквы: своя — 0, соседняя клавиша — 1 опечатка, далёкая — 2."""
    if a == b:
        return 0
    return 1 if keys_adjacent(a, b) else 2


def edit_distance(a: str, b: str) -> int:
    """Сколько опечаток отделяет a от b с учётом клавиатуры.

    Одна опечатка: пропущенная или лишняя буква («звтра»), перестановка двух соседних
    букв («срдений») или нажатие соседней клавиши («зпвтра»: «п» рядом с «а»).
    Буква, заменённая на далёкую клавишу, считается за две опечатки:
    поэтому «помочь» не путается с «помощь» — «ч» и «щ» на клавиатуре далеко.
    """
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = substitution_cost(a[i - 1], b[j - 1])
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def typo_limit(word: str) -> int:
    """Сколько опечаток допускаем в слове. Короткие («да», «нет», «ок», «лист») — только точно:
    в них одна опечатка уже даёт другое слово («лиса» — не «лист»)."""
    if len(word) <= 4:
        return 0
    return 1 if len(word) <= 7 else 2


def match_phrase(text: str, options: dict):
    """Ищет в options фразу, совпадающую с text с учётом небольших опечаток.

    Слова сравниваются по отдельности, количество слов должно совпадать.
    Возвращает значение найденного варианта или None, если ничего не подошло
    или подошли разные варианты одинаково хорошо.
    """
    text = normalize(text)
    if text in options:
        return options[text]
    words = text.split()
    best, best_distance, ambiguous = None, None, False
    for phrase, value in options.items():
        phrase_words = phrase.split()
        if len(phrase_words) != len(words):
            continue
        distances = [edit_distance(w, p) for w, p in zip(words, phrase_words)]
        if any(d > typo_limit(p) for d, p in zip(distances, phrase_words)):
            continue
        distance = sum(distances)
        if best_distance is None or distance < best_distance:
            best, best_distance, ambiguous = value, distance, False
        elif distance == best_distance and value != best:
            ambiguous = True
    return None if ambiguous else best


# --- Команды обычным текстом ------------------------------------------------------

# Команды без аргументов: фраза -> (команда, аргументы)
TEXT_COMMANDS = {
    "список": ("list", []),
    "лист": ("list", []),
    "задачи": ("list", []),
    "мои задачи": ("list", []),
    "сегодня": ("today", []),
    "на сегодня": ("today", []),
    "напоминания": ("reminders", []),
    "статистика": ("stats", []),
    "помощь": ("help", []),
    "команды": ("help", []),
    "сводка": ("digest", []),
    "сводка вкл": ("digest", ["on"]),
    "сводка выкл": ("digest", ["off"]),
    "сводка рабочие": ("digest", ["рабочие"]),
    "сводка рабочие дни": ("digest", ["рабочие"]),
    "сводка каждый день": ("digest", ["каждый", "день"]),
    "сводка ежедневно": ("digest", ["каждый", "день"]),
    "добавить": ("add", []),
    "отмена": ("cancel", []),
    # Без номера — бот покажет задачи кнопками
    "готово": ("done", []),
    "выполнено": ("done", []),
    "сделано": ("done", []),
    "удалить": ("delete", []),
    "напомни": ("remind", []),
    "напомнить": ("remind", []),
    "изменить": ("edit", []),
    "редактировать": ("edit", []),
    # Поиск, история, файлы, календарь
    "найти": ("find", []),
    "найди": ("find", []),
    "поиск": ("find", []),
    "история": ("history", []),
    "выполненные": ("history", []),
    "очистить историю": ("clear_history", []),
    "очистить всю историю": ("clear_history", ["все"]),
    "экспорт": ("export", []),
    "выгрузить": ("export", []),
    "выгрузка": ("export", []),
    "импорт": ("import", []),
    "загрузить": ("import", []),
    "загрузка": ("import", []),
    "рабочие дни": ("workdays", []),
    "категории": ("categories", []),
    "мои категории": ("categories", []),
    "настройки": ("settings", []),
    "отзыв": ("feedback", []),
    "оставить отзыв": ("feedback", []),
    "мой id": ("myid", []),
    # Приветствия — отвечаем как на /start, а не предлагаем сохранить задачу
    "привет": ("start", []),
    "приветик": ("start", []),
    "здравствуй": ("start", []),
    "здравствуйте": ("start", []),
    "добрый день": ("start", []),
    "доброе утро": ("start", []),
    "добрый вечер": ("start", []),
    "хай": ("start", []),
    "салют": ("start", []),
    "hi": ("start", []),
    "hello": ("start", []),
    "начать": ("start", []),
    "старт": ("start", []),
}

# Команды с аргументами: первое слово -> (команда, шаблон для остальной части сообщения)
TASK_ID_RE = r"#?\d+"
TIME_RE = r"\d{1,2}:\d{2}"
DATE_RE = r"\d{1,2}\.\d{1,2}(?:\.\d{4})?"
REMIND_RE = rf"{TASK_ID_RE}(?: (?:{DATE_RE} )?{TIME_RE})?"
TEXT_COMMANDS_WITH_ARGS = {
    "добавить": ("add", r".+"),
    "готово": ("done", TASK_ID_RE),
    "выполнено": ("done", TASK_ID_RE),
    "сделано": ("done", TASK_ID_RE),
    "удалить": ("delete", TASK_ID_RE),
    "изменить": ("edit", TASK_ID_RE),
    "редактировать": ("edit", TASK_ID_RE),
    "напомни": ("remind", REMIND_RE),
    "напомнить": ("remind", REMIND_RE),
    "сводка": ("digest", TIME_RE),
    "найти": ("find", r".+"),
    "найди": ("find", r".+"),
    "поиск": ("find", r".+"),
    # «список рабочие», «список высокий», «список неделя» — фильтр проверяется отдельно,
    # чтобы «Список покупок» оставался задачей
    "список": ("list", None),
    "лист": ("list", None),
    "задачи": ("list", None),
}


def parse_text_command(text: str, user_id: int | None = None) -> tuple[str, list[str]] | None:
    """«Лист» -> ("list", []), «удалить 3» -> ("delete", ["3"]), «список рабочие» -> ("list", ["рабочие"]).

    Командой считается только сообщение, которое целиком совпадает с вариантом:
    «Список покупок» или «Сегодня позвонить маме» — это задачи, а не команды.
    user_id нужен, чтобы узнать категории пользователя для «список <категория>».
    """
    command = match_phrase(text, TEXT_COMMANDS)
    if command:
        return command
    words = text.split()
    if len(words) < 2:
        return None
    verb = match_phrase(words[0], TEXT_COMMANDS_WITH_ARGS)
    if not verb:
        return None
    name, pattern = verb
    if name in ("add", "find"):
        return name, words[1:]  # текст задачи или запроса сохраняем как есть, с регистром
    if name == "list":
        wanted = " ".join(words[1:])
        if user_id is not None and match_phrase(wanted, list_filter_options(user_id)) is not None:
            return name, words[1:]
        return None
    rest = normalize(" ".join(words[1:]))
    if re.fullmatch(pattern, rest):
        return name, rest.split()
    return None


async def run_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           command: tuple[str, list[str]]) -> int:
    """Выполняет текстовую команду так же, как команду со слешем.
    Если шёл диалог, он молча завершается. Возвращает новое состояние диалога."""
    name, args = command
    context.args = args
    # Исходный текст: /find подскажет, как добавить «Найти ключи» задачей, если поиск пустой
    context.user_data["text_command_source"] = update.message.text.strip()
    record_usage(update.effective_user.id, name, "text")
    try:
        if name in DIALOG_COMMAND_HANDLERS:  # /add, /edit, /done, /delete, /remind, /find, /cancel…
            return await DIALOG_COMMAND_HANDLERS[name](update, context)
        await end_dialog_quietly(context)
        await COMMAND_HANDLERS[name](update, context)
        return ConversationHandler.END
    finally:
        context.user_data.pop("text_command_source", None)


async def try_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Если сообщение — текстовая команда, выполняет её и возвращает новое состояние, иначе None."""
    command = parse_text_command(update.message.text, update.effective_user.id)
    if command is None:
        return None
    return await run_text_command(update, context, command)


def validate_task_text(text: str) -> str | None:
    """Возвращает сообщение об ошибке или None, если текст подходит."""
    if not text.strip():
        return "Текст задачи пустой. Напишите, что нужно сделать."
    if len(text) > MAX_TASK_LENGTH:
        return f"Слишком длинно (больше {MAX_TASK_LENGTH} символов). Сократите, пожалуйста."
    return None


async def drop_keyboard(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Убирает кнопки из последнего вопроса диалога (например, когда ответили текстом)."""
    kb_message = context.user_data.pop("kb_message", None)
    if not kb_message:
        return
    chat_id, message_id = kb_message
    try:
        await context.bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except TelegramError:
        pass  # сообщение удалено или кнопок там уже нет — не страшно


async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                  keyboard: InlineKeyboardMarkup | None = None) -> None:
    """Ответ на шаге диалога.

    Нажата кнопка — редактируем это же сообщение.
    Пришёл текст — убираем кнопки из прошлого вопроса и отправляем новое сообщение.
    Сообщение с кнопками запоминаем, чтобы потом их можно было убрать.
    """
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=keyboard)
        message_key = (query.message.chat_id, query.message.message_id)
        if keyboard:
            context.user_data["kb_message"] = message_key
        elif context.user_data.get("kb_message") == message_key:
            context.user_data.pop("kb_message")
    else:
        await drop_keyboard(context)
        message = await update.message.reply_text(text, reply_markup=keyboard)
        if keyboard:
            context.user_data["kb_message"] = (message.chat_id, message.message_id)


async def end_dialog_quietly(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Молча завершает текущий диалог: забываем его данные и убираем кнопки."""
    for key in DIALOG_KEYS:
        context.user_data.pop(key, None)
    await drop_keyboard(context)


# --- Кнопки: цвета, «Назад» и «Отмена» -----------------------------------------------
# Telegram красит кнопку по полю style: success — зелёная, danger — красная, primary — синяя.
# Без style приложение рисует кнопку как обычно.
GREEN, RED, BLUE = KeyboardButtonStyle.SUCCESS, KeyboardButtonStyle.DANGER, KeyboardButtonStyle.PRIMARY


def button(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    """Кнопка под сообщением; style — цвет (GREEN, RED, BLUE) или None."""
    return InlineKeyboardButton(text, callback_data=data, style=style)


CANCEL_BUTTON = button("✖️ Отмена", "nav:cancel")


def with_nav(keyboard: InlineKeyboardMarkup | None, back: str | None = None,
             keep: tuple[str, str] | None = None) -> InlineKeyboardMarkup:
    """Кнопки вопроса плюс нижние ряды: «➡️ Оставить: …» (keep = (что показать, шаг)) —
    если на этот шаг вернулись «Назад» и ответ уже есть, «⬅️ Назад» (на шаг back) и «✖️ Отмена»."""
    rows = [list(row) for row in keyboard.inline_keyboard] if keyboard else []
    if keep:
        rows.append([button(f"➡️ Оставить: {keep[0]}", f"keep:{keep[1]}")])
    rows.append([button("⬅️ Назад", f"back:{back}"), CANCEL_BUTTON] if back else [CANCEL_BUTTON])
    return InlineKeyboardMarkup(rows)


# Постоянные кнопки под полем ввода. Нажатие присылает их текст, а main_menu_button
# превращает его в команду — в любой момент, даже посреди диалога.
MAIN_MENU = {"Добавить": "add", "Мои задачи": "list", "Настройки": "settings", "Помощь": "help"}
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Добавить", style=GREEN), KeyboardButton("Мои задачи", style=BLUE)],
     [KeyboardButton("Настройки", style=BLUE), KeyboardButton("Помощь", style=BLUE)]],
    resize_keyboard=True,   # кнопки компактные, а не на пол-экрана
    is_persistent=True,     # видны всегда, даже когда открыта обычная клавиатура
    input_field_placeholder="Напишите задачу или нажмите кнопку",
)


async def show_view(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                    keyboard: InlineKeyboardMarkup | None = None) -> None:
    """Экран раздела («Мои задачи», карточка задачи, «Настройки»). Это не вопрос диалога,
    поэтому его кнопки не запоминаем в kb_message — они работают и потом.
    После нажатия кнопки экран меняется в том же сообщении, иначе приходит новым."""
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=keyboard)
        if context.user_data.get("kb_message") == (query.message.chat_id, query.message.message_id):
            context.user_data.pop("kb_message")
    else:
        await drop_keyboard(context)
        await update.message.reply_text(text, reply_markup=keyboard)


async def main_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки под полем ввода: «Добавить», «Мои задачи», «Настройки», «Помощь».
    Работают в любой момент: если шёл диалог, он завершается. Исключение — «Добавить»
    в ответ на «Сохранить как задачу?»: это и есть «да»."""
    name = MAIN_MENU[update.message.text]
    record_usage(update.effective_user.id, name, "button")
    if name == "add" and (context.user_data.get("draft") or {}).get("awaiting_confirm"):
        return await quick_answer(update, context, True)
    context.args = []
    if name in DIALOG_COMMAND_HANDLERS:
        return await DIALOG_COMMAND_HANDLERS[name](update, context)
    await end_dialog_quietly(context)
    await COMMAND_HANDLERS[name](update, context)
    return ConversationHandler.END


async def step_gone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка от уже закончившегося диалога (например, после перезапуска бота)."""
    await end_dialog_quietly(context)
    await respond(update, context, "Этот шаг уже закончился. Начните заново: «Добавить» или «Мои задачи».")
    return ConversationHandler.END


async def back_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«⬅️ Назад»: возвращает на предыдущий шаг. Куда именно — записано в кнопке (back:шаг).
    При добавлении задачи уже введённое сохраняется в черновике."""
    await update.callback_query.answer()
    target = update.callback_query.data.split(":", 1)[1]
    data = context.user_data
    draft = data.get("draft") or {}

    # Шаги добавления задачи
    if target in ("text", "priority", "due", "category"):
        if editing(context) or "text" not in draft:
            return await step_gone(update, context)
        if target == "text":
            return await ask_text_step(update, context)
        if target == "priority":
            return await ask_priority_step(update, context)
        if target == "due":
            return await ask_due_step(update, context)
        return await ask_category(update, context, date.fromisoformat(draft["due"]) if draft.get("due") else None)

    if target == "card" and data.get("card"):  # из шага карточки — обратно в карточку
        card = data["card"]
        await end_dialog_quietly(context)
        return await show_card(update, context, card["id"], card["flt"], card["page"])
    if target == "edit" and data.get("edit"):  # из шага /edit — к вопросу «Что изменить?»
        return await ask_edit_field(update, context, data["edit"])
    if target == "edit_due" and editing(context):  # из ввода даты — к выбору срока
        return await ask_edit_value(update, context, "due")
    if target == "remind" and task_in_remind_dialog(update, context):  # из «Указать время»
        task = task_in_remind_dialog(update, context)
        return await ask_remind_option(update, context, task, remind_question(context, task))
    if target == "list":  # из поиска — к списку задач
        flt, page = data.get("list_view", "all:0").split(":")
        await end_dialog_quietly(context)
        return await show_task_list(update, context, flt, int(page))
    if target == "digest":  # из ввода времени сводки — к настройкам сводки
        await end_dialog_quietly(context)
        await show_digest_panel(update, context)
        return ConversationHandler.END
    if target == "cat_action" and managed_category(update, context):  # из переименования категории
        return await ask_category_action(update, context, managed_category(update, context))
    if target == "fb" and data.get("feedback") is not None:  # к предыдущему вопросу отзыва
        step = data["feedback"].get("step", 0)
        if step <= 0:
            return await ask_feedback_rating(update, context)
        return await ask_feedback_question(update, context, step - 1)
    return await step_gone(update, context)


async def keep_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«➡️ Оставить: …» — на этот шаг вернулись «Назад», ответ уже есть: идём дальше без изменений."""
    await update.callback_query.answer()
    step = update.callback_query.data.split(":", 1)[1]
    draft = context.user_data.get("draft") or {}
    if editing(context) or "text" not in draft:
        return await step_gone(update, context)
    if step == "text":
        return await ask_priority_step(update, context, f"📝 «{draft['text']}»\n")
    if step == "priority" and draft.get("priority"):
        return await set_priority(update, context, draft["priority"])
    if step == "due" and "due" in draft:
        return await ask_category(update, context, date.fromisoformat(draft["due"]) if draft["due"] else None)
    if step == "category" and "category" in draft:
        return await finish_add(update, context, draft["category"])
    return await step_gone(update, context)


async def cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«✖️ Отмена» в любом диалоге. Из карточки задачи — обратно в карточку."""
    await update.callback_query.answer()
    card = context.user_data.get("card")
    message = cancel_message(context)
    await end_dialog_quietly(context)
    if card:
        return await show_card(update, context, card["id"], card["flt"], card["page"], f"{message}\n\n")
    await update.callback_query.edit_message_text(message)
    return ConversationHandler.END


def cancel_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Что ответить на отмену: если задачу уже сохранили (вернулись «Назад» после сохранения),
    говорим, что она осталась."""
    task_id = (context.user_data.get("draft") or {}).get("task_id")
    if task_id:
        return f"Отменено. Задача #{task_id} уже сохранена."
    return "Отменено." if any(key in context.user_data for key in DIALOG_KEYS) else "Нечего отменять 🙂"


# --- Добавление задачи -------------------------------------------------------
# Шаги: текст → приоритет → срок → категория → напоминание. На каждом шаге есть «⬅️ Назад»:
# ответы лежат в черновике (user_data["draft"]) и не теряются. Задача сохраняется на шаге
# категории; если после этого вернуться «Назад» и что-то поменять, обновится та же задача
# (её номер в черновике — draft["task_id"]).

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/add — начало диалога. Можно сразу: /add Купить хлеб."""
    get_user(context, update.effective_user.id)
    await end_dialog_quietly(context)  # /add посреди диалога начинает заново
    context.user_data["draft"] = {}
    text = " ".join(context.args or []).strip()
    if text:
        error = validate_task_text(text)
        if error:
            await update.message.reply_text(error)
            return ConversationHandler.END
        return await start_new_task(update, context, text, ask_confirm=False)
    return await ask_text_step(update, context)


async def ask_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 1: текст задачи. Это первый шаг, назад с него некуда — есть только «Отмена».
    Если вернулись сюда «Назад», показываем введённый текст и кнопку «Оставить»."""
    text = (context.user_data.get("draft") or {}).get("text")
    if text:
        await respond(update, context, f"📝 Сейчас: «{text}»\nНапишите новый текст или оставьте этот.",
                      with_nav(None, keep=("текст", "text")))
    else:
        await respond(update, context, "📝 Напишите текст задачи.", with_nav(None))
    return TEXT


async def add_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 1: получили текст задачи.
    Здесь любой текст подходит как ответ, поэтому сначала проверяем текстовые команды
    («список», «отмена»): иначе из них получились бы задачи."""
    state = await try_text_command(update, context)
    if state is not None:
        return state
    text = update.message.text.strip()
    error = validate_task_text(text)
    if error:
        await update.message.reply_text(error)
        return TEXT
    if editing(context):
        return await edit_text_entered(update, context, text)
    return await start_new_task(update, context, text, ask_confirm=False)


async def quick_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Быстрое добавление: пользователь просто написал текст вне диалога."""
    get_user(context, update.effective_user.id)
    state = await try_text_command(update, context)  # «лист», «удалить 3» и т. п.
    if state is not None:
        return state
    text = update.message.text.strip()
    if normalize(text) in YES_NO_WORDS:
        # «да», «нет», «ок» без вопроса — это не задача
        await update.message.reply_text("Чтобы добавить задачу, напишите её текст.")
        return ConversationHandler.END
    error = validate_task_text(text)
    if error:
        await update.message.reply_text(error)
        return ConversationHandler.END
    record_usage(update.effective_user.id, "quick_add", "text")
    return await start_new_task(update, context, text, ask_confirm=True)


QUICK_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Добавить", callback_data="quick:yes", style=GREEN),
    InlineKeyboardButton("Отмена", callback_data="quick:no"),
]])

DUPLICATE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Создать", callback_data="dup:yes"),
    InlineKeyboardButton("Отмена", callback_data="dup:no"),
]])


def find_duplicate(user: dict, text: str, exclude_id: int | None = None) -> dict | None:
    """Активная задача с тем же текстом — без учёта регистра, лишних пробелов
    и знаков препинания в конце. Выполненные задачи не сравниваем.
    exclude_id — номер редактируемой задачи: сама с собой она не «повторяется»."""
    key = normalize(text)
    return next((t for t in active_tasks(user)
                  if normalize(t["text"]) == key and t["id"] != exclude_id), None)


async def start_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         text: str, ask_confirm: bool) -> int:
    """Текст задачи получен (/add, /add <текст> или быстрое добавление).

    Если такая активная задача уже есть — спрашиваем, создавать ли повтор
    (при быстром добавлении этот вопрос заменяет «Сохранить как задачу?»).
    Иначе быстрое добавление спрашивает «Сохранить как задачу?», а /add — сразу приоритет.
    """
    draft = context.user_data.setdefault("draft", {})  # уже введённое при «Назад» сохраняется
    draft["text"] = text
    duplicate = find_duplicate(get_user(context, update.effective_user.id), text,
                               exclude_id=draft.get("task_id"))
    if duplicate:
        line = task_line(duplicate)
        dot = "" if line.endswith((".", "!", "?", "…")) else "."  # без «ТЕСТ!.»
        keyboard = InlineKeyboardMarkup(list(DUPLICATE_KEYBOARD.inline_keyboard) +
                                        [[button("⬅️ Назад", "back:text")]])
        await respond(update, context, f"Такая задача уже есть: {line}{dot} Всё равно создать?", keyboard)
        return DUPLICATE
    if ask_confirm:
        draft["awaiting_confirm"] = True  # кнопка «Добавить» внизу тоже будет означать «да»
        await respond(update, context, f"Сохранить как задачу?\n«{text}»", QUICK_KEYBOARD)
        return CONFIRM
    return await ask_priority_step(update, context)


async def duplicate_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки «Создать» / «Отмена» под вопросом о повторе."""
    query = update.callback_query
    await query.answer()
    return await duplicate_answer(update, context, query.data == "dup:yes")


async def duplicate_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Всё равно создать?»: «да», «создать», «нет», «отмена»…"""
    yes = match_phrase(update.message.text, DUPLICATE_WORDS)
    if yes is not None:
        return await duplicate_answer(update, context, yes)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(YES_NO_HINT)
    return DUPLICATE


async def duplicate_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, yes: bool) -> int:
    """Создаём повтор (дальше — приоритет и срок) или отменяем.
    При редактировании «да» сохраняет новый текст задачи."""
    if editing(context):
        if yes:
            return await finish_edit(update, context, {"text": context.user_data["draft"]["text"]})
        for key in ("edit", "draft"):
            context.user_data.pop(key, None)
        if context.user_data.get("card"):
            return await return_to_card(update, context, "Ок, задача не изменена.")
        await respond(update, context, "Ок, задача не изменена.")
        return ConversationHandler.END
    if not yes:
        message = cancel_message(context) if context.user_data["draft"].get("task_id") else "Ок, не создаю."
        context.user_data.pop("draft", None)
        await respond(update, context, message)
        return ConversationHandler.END
    return await ask_priority_step(update, context, f"📝 «{context.user_data['draft']['text']}»\n")


async def quick_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки «Добавить» / «Отмена»."""
    query = update.callback_query
    await query.answer()
    return await quick_answer(update, context, query.data == "quick:yes")


async def quick_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Сохранить как задачу?». Сначала — ответ, затем — текстовая команда."""
    yes = match_phrase(update.message.text, YES_NO_WORDS)
    if yes is not None:
        return await quick_answer(update, context, yes)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(YES_NO_HINT)
    return CONFIRM


async def quick_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, yes: bool) -> int:
    """Общая логика для кнопки и текста: сохраняем дальше или отменяем."""
    if not yes:
        context.user_data.pop("draft", None)
        await respond(update, context, "Ок, не сохраняю.")
        return ConversationHandler.END
    context.user_data["draft"].pop("awaiting_confirm", None)
    return await ask_priority_step(update, context, f"📝 «{context.user_data['draft']['text']}»\n")


async def priority_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2: приоритет выбран кнопкой."""
    query = update.callback_query
    await query.answer()
    return await set_priority(update, context, query.data.split(":", 1)[1])


async def priority_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2: приоритет написан текстом (с опечатками: «срдений» -> средний)."""
    priority = match_phrase(update.message.text, PRIORITY_WORDS)
    if priority is not None:
        return await set_priority(update, context, priority)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(PRIORITY_HINT)
    return PRIORITY


async def set_priority(update: Update, context: ContextTypes.DEFAULT_TYPE, priority: str) -> int:
    if editing(context):
        return await finish_edit(update, context, {"priority": priority})
    context.user_data["draft"]["priority"] = priority
    p = PRIORITIES[priority]
    return await ask_due_step(update, context, f"{p['emoji']} Приоритет: {p['name']}. ")


async def ask_priority_step(update: Update, context: ContextTypes.DEFAULT_TYPE, intro: str = "") -> int:
    """Шаг 2: приоритет. «Назад» — к тексту; если приоритет уже выбран — «Оставить»."""
    priority = context.user_data["draft"].get("priority")
    keep = (PRIORITIES[priority]["name"], "priority") if priority else None
    await respond(update, context, f"{intro}Какой приоритет?",
                  with_nav(PRIORITY_KEYBOARD, back="text", keep=keep))
    return PRIORITY


async def ask_due_step(update: Update, context: ContextTypes.DEFAULT_TYPE, intro: str = "") -> int:
    """Шаг 3: срок. «Назад» — к приоритету; если срок уже выбран — «Оставить»."""
    draft = context.user_data["draft"]
    keep = None
    if "due" in draft:
        keep = (fmt_date(date.fromisoformat(draft["due"])) if draft["due"] else "без срока", "due")
    await respond(update, context, f"{intro}Какой срок?", with_nav(DUE_KEYBOARD, back="priority", keep=keep))
    return DUE


async def due_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: срок выбран кнопкой."""
    query = update.callback_query
    await query.answer()
    return await set_due(update, context, query.data.split(":", 1)[1])


async def due_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: срок написан текстом — словом («завтра»), датой («14.10»)
    или в рабочих днях («через 5 рабочих дней»)."""
    # «сегодня» здесь — это срок, а не команда /today: ответ на вопрос проверяется первым
    choice = match_phrase(update.message.text, DUE_WORDS)
    if choice is not None:
        return await set_due(update, context, choice)
    due = parse_date(normalize(update.message.text))
    if due is not None:
        return await ask_category(update, context, due)
    count = parse_workdays(update.message.text, word_required=True)
    if count is not None:
        return await set_due_in_workdays(update, context, count)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(DUE_HINT)
    return DUE


async def set_due(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> int:
    back = "edit_due" if editing(context) else "due"  # «Назад» — к выбору срока
    if choice == "custom":
        await respond(update, context, "📅 Введите дату: ДД.ММ или ДД.ММ.ГГГГ, например 14.10",
                      with_nav(None, back=back))
        return DATE_INPUT
    if choice == "workdays":
        await respond(update, context, f"📅 Через сколько рабочих дней? {WORKDAYS_HINT}",
                      with_nav(None, back=back))
        return WORKDAYS_INPUT
    due = {"today": today(), "tomorrow": today() + timedelta(days=1), "none": None}[choice]
    return await ask_category(update, context, due)


async def date_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3 (вариант): после «Указать дату» ввели дату текстом."""
    due = parse_date(normalize(update.message.text))
    if due is None:
        state = await try_text_command(update, context)
        if state is not None:
            return state
        await update.message.reply_text("Не понял дату. Пример: 14.10 или 14.10.2026")
        return DATE_INPUT
    return await ask_category(update, context, due)


# --- Срок в рабочих днях -------------------------------------------------------------

MAX_WORKDAYS = 250  # примерно год рабочих дней
WORKDAYS_HINT = f"Напишите число от 1 до {MAX_WORKDAYS}, например 5."
# «через 5 рабочих дней», «5 рабочих дней», «5 раб. дней»; на шаге ввода числа — просто «5»
WORKDAYS_WORDS_RE = r"(?: (?:рабочих|рабочий|рабочего|раб)\.?(?: (?:дней|дня|день|дн)\.?)?)"


def parse_workdays(text: str, word_required: bool) -> int | None:
    """Число рабочих дней из ответа. word_required — нужно ли слово «рабочих» (на шаге срока
    просто «5» могло бы означать что угодно, а на шаге «Через сколько рабочих дней?» — нет)."""
    words = WORKDAYS_WORDS_RE if word_required else WORKDAYS_WORDS_RE + "?"
    m = re.fullmatch(rf"(?:через )?(\d{{1,3}}){words}", normalize(text))
    return int(m[1]) if m else None


async def workdays_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """После «Через N рабочих дней» написали число."""
    count = parse_workdays(update.message.text, word_required=False)
    if count is None:
        state = await try_text_command(update, context)
        if state is not None:
            return state
        await update.message.reply_text(f"Не понял число. {WORKDAYS_HINT}")
        return WORKDAYS_INPUT
    return await set_due_in_workdays(update, context, count)


async def set_due_in_workdays(update: Update, context: ContextTypes.DEFAULT_TYPE, count: int) -> int:
    """Срок через count рабочих дней: считаем по производственному календарю."""
    if not 1 <= count <= MAX_WORKDAYS:
        await update.message.reply_text(f"Так не получится. {WORKDAYS_HINT}")
        return WORKDAYS_INPUT
    due, from_calendar = await add_workdays(today(), count)
    intro = (f"📅 Через {count} {plural(count, 'рабочий день', 'рабочих дня', 'рабочих дней')} — "
             f"{fmt_day(due)}")
    if not from_calendar:
        intro += f"\n{CALENDAR_FALLBACK_NOTE}"
    # Дата и так рабочая, поэтому предупреждение о выходном не нужно
    return await ask_category(update, context, due, intro, check_day_off=False)


# --- Шаг 4: категория -----------------------------------------------------------------

NO_CATEGORY = ""  # «без категории» в ответах (None у match_phrase означает «не распознано»)
NO_CATEGORY_WORDS = {"без категории": NO_CATEGORY, "без": NO_CATEGORY, "нет": NO_CATEGORY,
                     "пропустить": NO_CATEGORY}
CATEGORY_BUTTON_LIMIT = 20  # сколько категорий показывать кнопками
CATEGORY_HINT = "Нажмите кнопку или напишите название категории."


def user_categories(user_id: int) -> list[str]:
    """Базовые категории и свои категории пользователя — в порядке создания."""
    return BASE_CATEGORIES + db.custom_categories(user_id)


def find_category(text: str, categories: list[str]) -> str | None:
    """Существующая категория, совпадающая с text без учёта регистра (и с небольшими опечатками)."""
    return match_phrase(text, {normalize(name): name for name in categories})


def validate_category(name: str) -> str | None:
    """Сообщение об ошибке или None, если название категории подходит."""
    if not any(ch.isalpha() for ch in name):
        return "В названии категории должна быть хотя бы одна буква."
    if len(name) > MAX_CATEGORY_LENGTH:
        return f"Слишком длинное название (больше {MAX_CATEGORY_LENGTH} символов)."
    return None


def clean_category_name(text: str) -> tuple[str, str | None]:
    """Название категории из ответа: без лишних пробелов и знаков в конце, с заглавной буквы
    («спорт» -> «Спорт», как у базовых категорий). Возвращает (название, ошибка или None)."""
    name = " ".join(text.split()).rstrip(".,!?;:…")
    error = validate_category(name)
    if error:
        return name, error
    return name[0].upper() + name[1:], None


def button_rows(buttons: list[InlineKeyboardButton], long_label: int = 14) -> list[list]:
    """Кнопки по две в ряд; длинные подписи — на всю строку, чтобы Telegram их не обрезал."""
    rows, pair = [], []
    for button in buttons:
        if len(button.text) > long_label:
            rows.append([button])
            continue
        pair.append(button)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return rows


def category_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    """Категории кнопками (номер в callback: название может не влезть в 64 байта)
    и «Без категории» отдельной строкой внизу."""
    buttons = [InlineKeyboardButton(name, callback_data=f"cat:{i}")
               for i, name in enumerate(categories[:CATEGORY_BUTTON_LIMIT])]
    rows = button_rows(buttons)
    rows.append([InlineKeyboardButton("Без категории", callback_data="cat:none")])
    return InlineKeyboardMarkup(rows)


async def ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE, due: date | None,
                       intro: str | None = None, check_day_off: bool = True) -> int:
    """Срок выбран: запоминаем его, предупреждаем о выходном или празднике
    и спрашиваем категорию. При редактировании — сразу сохраняем новый срок."""
    if editing(context):
        notes = (intro or "") + (await day_off_warning(due) if check_day_off else "")
        return await finish_edit(update, context, {"due": due.isoformat() if due else None},
                                 [line for line in notes.split("\n") if line])
    context.user_data["draft"]["due"] = due.isoformat() if due else None
    if intro is None:
        intro = f"📅 Срок: {fmt_day(due)}" if due else "📅 Без срока."
    if check_day_off:
        intro += await day_off_warning(due)
    draft = context.user_data["draft"]
    keep = (draft["category"] or "без категории", "category") if "category" in draft else None
    categories = user_categories(update.effective_user.id)
    await respond(update, context,
                  f"{intro}\n\n🏷 Категория задачи? Выберите из списка или напишите свою.",
                  with_nav(category_keyboard(categories), back="due", keep=keep))
    return CATEGORY


async def category_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Категория выбрана кнопкой."""
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "none":
        return await finish_add(update, context, None)
    categories = user_categories(update.effective_user.id)
    index = int(value)
    if index >= len(categories):  # кнопка от старого списка
        await respond(update, context, f"Выберите категорию ещё раз. {CATEGORY_HINT}",
                      category_keyboard(categories))
        return CATEGORY
    return await finish_add(update, context, categories[index])


async def category_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Категория написана текстом: существующая, «без категории» или новая.
    Порядок: сначала ответ на вопрос (существующая категория), затем текстовая команда,
    и только потом — новая категория (иначе «отмена» стала бы категорией)."""
    text = update.message.text
    if match_phrase(text, NO_CATEGORY_WORDS) is not None:
        return await finish_add(update, context, None)
    user_id = update.effective_user.id
    existing = find_category(text, user_categories(user_id))
    if existing:
        return await finish_add(update, context, existing)
    state = await try_text_command(update, context)
    if state is not None:
        return state

    name, error = clean_category_name(text)
    if error:
        await update.message.reply_text(f"{error} {CATEGORY_HINT}")
        return CATEGORY
    db.add_category(user_id, name)
    return await finish_add(update, context, name, new_category=True)


def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, dict]:
    """Сохраняет черновик как задачу. Возвращает текст подтверждения и саму задачу.
    Если задачу уже сохранили, а потом вернулись «Назад» и что-то поменяли, — обновляет
    ту же задачу: номер не меняется. Черновик остаётся до ответа о напоминании."""
    draft = context.user_data["draft"]
    user = get_user(context, update.effective_user.id)
    fields = {"text": draft["text"], "priority": draft["priority"], "due": draft.get("due"),
              "category": draft.get("category")}
    task = find_task(user, draft["task_id"]) if draft.get("task_id") else None
    if task:
        task.update(fields)
        db.save_task(user["user_id"], task)
        verb = "обновлена"
    else:
        task = db.add_task(user["user_id"], **fields)
        draft["task_id"] = task["id"]
        verb = "добавлена"

    p = PRIORITIES[task["priority"]]
    due = task_due(task)
    result = f"Задача #{task['id']} {verb}: {p['emoji']} {p['name']}, "
    result += f"срок {fmt_date(due)}" if due else "без срока"
    if task["category"]:
        result += f", 🏷 {task['category']}"
    if due and due < today():
        result += "\n⚠️ Эта дата уже прошла — задача сразу просрочена."
    return result, task


async def finish_add(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     category: str | None, new_category: bool = False) -> int:
    """Последний шаг добавления: сохраняем задачу и сразу спрашиваем, напомнить ли о ней.
    При редактировании — сохраняем новую категорию задачи."""
    if editing(context):
        notes = [f"🏷 Новая категория «{category}» сохранена."] if new_category else []
        return await finish_edit(update, context, {"category": category}, notes)
    context.user_data["draft"]["category"] = category
    confirmation, task = save_draft(update, context)
    if new_category:
        confirmation = f"🏷 Новая категория «{category}» сохранена.\n{confirmation}"
    context.user_data["remind_after_add"] = True
    context.user_data["remind_prefix"] = f"{confirmation}\n\n"  # чтобы не потерять подтверждение
    return await ask_remind_option(update, context, task, f"{confirmation}\n\nНапомнить о задаче?")


# --- Редактирование задачи (/edit) ---------------------------------------------------
# Те же шаги, что при добавлении (текст, приоритет, срок, категория). Пока в user_data
# лежит «edit» с номером задачи, последний шаг не создаёт задачу, а вызывает finish_edit.

EDIT_FIELD_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Текст", callback_data="efield:text"),
     InlineKeyboardButton("Приоритет", callback_data="efield:priority")],
    [InlineKeyboardButton("Срок", callback_data="efield:due"),
     InlineKeyboardButton("Категория", callback_data="efield:category")],
])
EDIT_FIELD_WORDS = {"текст": "text", "приоритет": "priority", "срок": "due", "дата": "due",
                    "категория": "category", "категорию": "category"}
EDIT_FIELD_HINT = "Напишите: текст, приоритет, срок или категория, либо нажмите кнопку."
EDIT_DUPLICATE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Сохранить", callback_data="dup:yes"),
    InlineKeyboardButton("Отмена", callback_data="dup:no"),
]])

# Напоминания, привязанные к сроку: при смене срока они переносятся на новую дату
DUE_REMINDER_NOTES = {"в день срока": "due", "за сутки до срока": "day",
                      "за неделю до срока": "week", "за месяц до срока": "month"}


def editing(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Идёт ли сейчас редактирование задачи (а не добавление новой)."""
    return "edit" in context.user_data


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/edit <номер> — изменить задачу. Без номера — выбор задачи кнопками."""
    await end_dialog_quietly(context)
    args = context.args or []
    if not args:
        return await ask_pick_task(update, context, "edit")
    task_id = parse_task_id(args[0]) if len(args) == 1 else None
    if task_id is None:
        await update.message.reply_text("Укажите номер задачи. Пример: /edit 3")
        return ConversationHandler.END
    return await ask_edit_field(update, context, task_id)


async def ask_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> int:
    """Показывает задачу и спрашивает, что в ней изменить."""
    task = find_task(get_user(context, update.effective_user.id), task_id)
    if not task:
        await respond(update, context, f"Задачи #{task_id} нет. Посмотреть номера: /list")
        return ConversationHandler.END
    if task["status"] == "done":
        await respond(update, context,
                      f"Задача #{task_id} уже выполнена — изменить можно только активную задачу.")
        return ConversationHandler.END
    context.user_data["edit"] = task_id
    context.user_data["draft"] = {}  # шаги добавления складывают ответы в черновик
    await respond(update, context, f"✏️ {task_line(task)}\nЧто изменить?",
                  with_nav(EDIT_FIELD_KEYBOARD, back="card" if context.user_data.get("card") else None))
    return EDIT_FIELD


def edited_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Задача, которую сейчас редактируем (None, если её успели удалить или выполнить)."""
    task_id = context.user_data.get("edit")
    task = find_task(get_user(context, update.effective_user.id), task_id) if task_id else None
    return task if task and task["status"] == "active" else None


async def edit_field_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки «Текст», «Приоритет», «Срок», «Категория»."""
    query = update.callback_query
    await query.answer()
    return await ask_edit_value(update, context, query.data.split(":", 1)[1])


async def edit_field_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Что изменить?»: «срок», «категорию»…"""
    field = match_phrase(update.message.text, EDIT_FIELD_WORDS)
    if field:
        return await ask_edit_value(update, context, field)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(EDIT_FIELD_HINT)
    return EDIT_FIELD


async def ask_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> int:
    """Тот же вопрос, что при добавлении задачи, и текущее значение."""
    task = edited_task(update, context)
    if not task:
        return await finish_edit(update, context, {})  # finish_edit объяснит, что задачи нет
    back = "card" if context.user_data.get("card") else "edit"  # «Назад» — в карточку или к «Что изменить?»
    if field == "text":
        await respond(update, context, f"📝 Сейчас: «{task['text']}»\nНапишите новый текст задачи.",
                      with_nav(None, back=back))
        return TEXT
    if field == "priority":
        p = PRIORITIES[task["priority"]]
        await respond(update, context, f"Сейчас: {p['emoji']} {p['name']}. Какой приоритет?",
                      with_nav(PRIORITY_KEYBOARD, back=back))
        return PRIORITY
    if field == "due":
        due = task_due(task)
        current = f"до {fmt_day(due)}" if due else "без срока"
        await respond(update, context, f"📅 Сейчас: {current}. Какой срок?", with_nav(DUE_KEYBOARD, back=back))
        return DUE
    current = f"🏷 {task['category']}" if task.get("category") else "без категории"
    await respond(update, context,
                  f"Сейчас: {current}.\n🏷 Категория задачи? Выберите из списка или напишите свою.",
                  with_nav(category_keyboard(user_categories(update.effective_user.id)), back=back))
    return CATEGORY


async def edit_text_entered(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    """Новый текст задачи. Как при добавлении, переспрашиваем, если такая задача уже есть."""
    user = get_user(context, update.effective_user.id)
    duplicate = find_duplicate(user, text, exclude_id=context.user_data["edit"])
    if duplicate:
        context.user_data["draft"]["text"] = text
        line = task_line(duplicate)
        dot = "" if line.endswith((".", "!", "?", "…")) else "."
        await respond(update, context, f"Такая задача уже есть: {line}{dot} Всё равно сохранить?",
                      EDIT_DUPLICATE_KEYBOARD)
        return DUPLICATE
    return await finish_edit(update, context, {"text": text})


def move_due_reminder(task: dict) -> str:
    """Срок изменился: напоминание «за сутки до срока» и т. п. переносим на новую дату.
    Возвращает строку для ответа пользователю ('' — если напоминание не привязано к сроку)."""
    note = task.get("remind_note")
    if not task.get("remind_at") or note not in DUE_REMINDER_NOTES:
        return ""
    if task_due(task) is None:
        task["remind_at"] = task["remind_note"] = None
        return f"🔕 Напоминание «{note}» снято: у задачи больше нет срока."
    when = due_relative_time(DUE_REMINDER_NOTES[note], task_due(task))
    if when <= now():
        task["remind_at"] = task["remind_note"] = None
        return (f"🔕 Напоминание «{note}» снято: для нового срока это время уже прошло "
                f"({fmt_when(when)}). Поставить другое: /remind {task['id']}")
    task["remind_at"] = when.isoformat(timespec="seconds")
    return f"⏰ Напоминание перенесено: {fmt_when(when)} ({note})."


async def finish_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, changes: dict,
                      notes: list[str] | None = None) -> int:
    """Сохраняет изменения задачи и показывает её обновлённой."""
    task = edited_task(update, context)
    task_id = context.user_data.get("edit")
    for key in ("edit", "draft"):
        context.user_data.pop(key, None)
    if not task:
        await respond(update, context, f"Задача #{task_id} уже удалена или выполнена — изменить её нельзя.")
        return ConversationHandler.END

    old_due = task["due"]
    task.update(changes)
    lines = [f"✏️ Задача #{task['id']} изменена: {task_line(task, show_id=False)}"]
    lines += notes or []
    if "due" in changes:
        due = task_due(task)
        if due and due < today():
            lines.append("⚠️ Эта дата уже прошла — задача просрочена.")
        if task["due"] != old_due:
            reminder_note = move_due_reminder(task)
            if reminder_note:
                lines.append(reminder_note)

    user_id = update.effective_user.id
    db.save_task(user_id, task)
    schedule_reminder(context.job_queue, user_id, task)  # переставит или снимет напоминание
    if context.user_data.get("card"):  # изменили из карточки — показываем её обновлённой
        return await return_to_card(update, context, "\n".join(lines))
    await respond(update, context, "\n".join(lines))
    return ConversationHandler.END


# --- Удаление с подтверждением ----------------------------------------------

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/delete <номер> — спрашиваем подтверждение. Без номера — выбор задачи кнопками."""
    await end_dialog_quietly(context)  # если шёл другой диалог — завершаем его
    args = context.args or []
    if not args:
        return await ask_pick_task(update, context, "delete")
    task_id = parse_task_id(args[0]) if len(args) == 1 else None
    if task_id is None:
        await update.message.reply_text("Укажите номер задачи. Пример: /delete 3")
        return ConversationHandler.END
    return await ask_delete(update, context, task_id)


async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int) -> int:
    """Спрашивает «Удалить задачу?» (кнопкой или текстом «да» / «нет»)."""
    task = find_task(get_user(context, update.effective_user.id), task_id)
    if not task:
        await respond(update, context, f"Задачи #{task_id} нет. Посмотреть номера: /list")
        return ConversationHandler.END
    context.user_data["delete_id"] = task_id
    keyboard = InlineKeyboardMarkup([[
        button("Да, удалить", f"del:yes:{task_id}", RED),
        button("Нет", f"del:no:{task_id}"),
    ]])
    await respond(update, context, f"Удалить задачу?\n{task_line(task)}", keyboard)
    return DELETE_CONFIRM


async def delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки «Да» / «Нет» под /delete. Работают и после окончания диалога."""
    query = update.callback_query
    await query.answer()
    _, answer, raw_id = query.data.split(":")
    return await delete_answer(update, context, int(raw_id), answer == "yes")


async def delete_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Удалить задачу?». Сначала — ответ, затем — текстовая команда."""
    yes = match_phrase(update.message.text, DELETE_WORDS)
    if yes is not None:
        return await delete_answer(update, context, context.user_data.get("delete_id"), yes)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(YES_NO_HINT)
    return DELETE_CONFIRM


async def delete_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        task_id: int | None, yes: bool) -> int:
    """Общая логика для кнопки и текста: удаляем или оставляем."""
    context.user_data.pop("delete_id", None)
    card = context.user_data.pop("card", None)  # удаляли из карточки задачи
    if not yes:
        if card:
            return await show_card(update, context, card["id"], card["flt"], card["page"],
                                   "Ок, задача остаётся.\n\n")
        await respond(update, context, "Ок, задача остаётся.")
        return ConversationHandler.END
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    task = find_task(user, task_id) if task_id is not None else None
    if not task:
        await respond(update, context, f"Задача #{task_id} уже удалена.")
        return ConversationHandler.END
    db.delete_task(user_id, task_id)
    cancel_jobs(context.job_queue, reminder_job_name(user_id, task_id))
    if card:  # из карточки — обратно к списку
        return await show_task_list(update, context, card["flt"], card["page"],
                                    f"🗑 Задача #{task_id} удалена.\n\n")
    await respond(update, context, f"🗑 Задача #{task_id} удалена.")
    return ConversationHandler.END


# --- Выход из диалога ---------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel или «отмена» текстом — прервать диалог (в диалогах для этого есть кнопка «✖️ Отмена»)."""
    card = context.user_data.get("card")
    message = cancel_message(context)
    await end_dialog_quietly(context)
    if card:
        return await show_card(update, context, card["id"], card["flt"], card["page"], f"{message}\n\n")
    await update.message.reply_text(message, reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def end_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Любая другая команда посреди диалога: молча завершаем диалог.
    Саму команду затем выполнит обычный обработчик из группы 1 (см. build_application)."""
    await end_dialog_quietly(context)
    return ConversationHandler.END


async def dialog_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """10 минут без ответа — тихо завершаем диалог."""
    await end_dialog_quietly(context)


# ---------------------------------------------------------------------------
# Команды: списки и выполнение
# ---------------------------------------------------------------------------

# --- /list и фильтры ---------------------------------------------------------------
# Фильтр — пара (вид, значение): ("prio", "high"), ("cat", "Рабочие"), ("cat", "") — без
# категории, ("week", "") — ближайшая неделя. None — без фильтра, все активные задачи.

LIST_FILTER_EXAMPLE = "Например: /list высокий, /list рабочие, /list неделя."


def list_filter_options(user_id: int) -> dict:
    """Текстовые варианты фильтров: «высокий», «неделя», названия категорий…"""
    options = {
        "высокий": ("prio", "high"), "высокие": ("prio", "high"),
        "средний": ("prio", "medium"), "средние": ("prio", "medium"),
        "низкий": ("prio", "low"), "низкие": ("prio", "low"),
        "неделя": ("week", ""), "неделю": ("week", ""), "на неделю": ("week", ""),
        "на неделе": ("week", ""), "ближайшая неделя": ("week", ""),
        "без категории": ("cat", ""),
    }
    for name in user_categories(user_id):
        options.setdefault(normalize(name), ("cat", name))
    return options


def filter_tasks(user: dict, spec: tuple[str, str] | None) -> tuple[str, list[dict]]:
    """Заголовок и активные задачи, подходящие под фильтр (в порядке /list)."""
    tasks = active_tasks(user)
    if spec is None:
        return f"📋 Активные задачи ({len(tasks)}):", tasks
    kind, value = spec
    if kind == "prio":
        selected = [t for t in tasks if t["priority"] == value]
        p = PRIORITIES[value]
        return f"{p['emoji']} Приоритет «{p['name']}» ({len(selected)}):", selected
    if kind == "cat":
        selected = [t for t in tasks if normalize(t.get("category") or "") == normalize(value)]
        title = f"🏷 {value}" if value else "🏷 Без категории"
        return f"{title} ({len(selected)}):", selected
    # Ближайшая неделя: срок не позже чем через 6 дней (просроченные тоже сюда попадают)
    last = today() + timedelta(days=6)
    selected = [t for t in tasks if task_due(t) and task_due(t) <= last]
    return f"📅 Ближайшая неделя, срок до {fmt_day(last)} ({len(selected)}):", selected


def render_list(user: dict, spec: tuple[str, str] | None) -> str:
    title, tasks = filter_tasks(user, spec)
    if not tasks:
        return f"{title}\nЗадач нет."
    return join_lines(f"{title}\n", [task_line(t) for t in tasks])


def list_keyboard(filtered: bool) -> InlineKeyboardMarkup:
    """Кнопки фильтров под списком задач."""
    rows = [
        [InlineKeyboardButton(p["button"], callback_data=f"flt:p:{key}")
         for key, p in PRIORITIES.items()],
        [InlineKeyboardButton("📅 Неделя", callback_data="flt:week"),
         InlineKeyboardButton("🏷 Категории", callback_data="flt:cats")],
    ]
    if filtered:
        rows.append([InlineKeyboardButton("📋 Все задачи", callback_data="flt:all")])
    return InlineKeyboardMarkup(rows)


def category_filter_keyboard(user: dict) -> InlineKeyboardMarkup:
    """Категории, в которых есть активные задачи, с количеством задач."""
    counts = Counter(normalize(t.get("category") or "") for t in active_tasks(user))
    buttons = [InlineKeyboardButton(f"{name} ({counts[normalize(name)]})", callback_data=f"flt:c:{i}")
               for i, name in enumerate(user_categories(user["user_id"])) if counts[normalize(name)]]
    rows = button_rows(buttons)
    if counts[""]:
        rows.append([InlineKeyboardButton(f"Без категории ({counts['']})", callback_data="flt:c:none")])
    rows.append([InlineKeyboardButton("⬅️ Все задачи", callback_data="flt:all")])
    return InlineKeyboardMarkup(rows)


async def list_filter_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки фильтров под /list: flt:p:high, flt:week, flt:cats, flt:c:3, flt:c:none, flt:all."""
    query = update.callback_query
    await query.answer()
    user = get_user(context, update.effective_user.id)
    parts = query.data.split(":")
    if parts[1] == "cats":  # показать кнопки категорий вместо кнопок фильтров
        await query.edit_message_reply_markup(category_filter_keyboard(user))
        return
    spec = None
    if parts[1] == "p" and parts[2] in PRIORITIES:
        spec = ("prio", parts[2])
    elif parts[1] == "week":
        spec = ("week", "")
    elif parts[1] == "c":
        categories = user_categories(user["user_id"])
        if parts[2] == "none":
            spec = ("cat", "")
        elif int(parts[2]) < len(categories):
            spec = ("cat", categories[int(parts[2])])
    await query.edit_message_text(render_list(user, spec), reply_markup=list_keyboard(spec is not None))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today — задачи на сегодня и просроченные."""
    tasks = today_tasks(get_user(context, update.effective_user.id))
    if not tasks:
        await update.message.reply_text("На сегодня задач нет 🎉")
        return
    await update.message.reply_text(join_lines("📅 На сегодня:\n", [task_line(t) for t in tasks]))


def complete_task(context: ContextTypes.DEFAULT_TYPE, user_id: int, task: dict) -> None:
    """Отмечает задачу выполненной и снимает её напоминание."""
    task["status"] = "done"
    task["done_at"] = now().isoformat(timespec="seconds")
    task["remind_at"] = None
    task["remind_note"] = None
    cancel_jobs(context.job_queue, reminder_job_name(user_id, task["id"]))
    db.save_task(user_id, task)


def complete_by_id(context: ContextTypes.DEFAULT_TYPE, user_id: int, task_id: int) -> str:
    """Отмечает задачу выполненной по номеру и возвращает текст ответа."""
    task = find_task(get_user(context, user_id), task_id)
    if not task:
        return f"Задачи #{task_id} нет. Посмотреть номера: /list"
    if task["status"] == "done":
        return f"Задача #{task_id} уже выполнена 👍"
    complete_task(context, user_id, task)
    return f"✅ Задача #{task_id} выполнена: {task['text']}"


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/done <номер>. Без номера — выбор задачи кнопками."""
    await end_dialog_quietly(context)
    args = context.args or []
    if not args:
        return await ask_pick_task(update, context, "done")
    task_id = parse_task_id(args[0]) if len(args) == 1 else None
    if task_id is None:
        await update.message.reply_text("Укажите номер задачи. Пример: /done 3")
        return ConversationHandler.END
    await update.message.reply_text(complete_by_id(context, update.effective_user.id, task_id))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Раздел «Мои задачи» и карточка задачи
# ---------------------------------------------------------------------------
# Список — экран с кнопками. Его вид записан в кнопках коротко, как «фильтр:страница»:
#   all — все активные, today — на сегодня и просроченные, week — ближайшая неделя,
#   rem — с напоминаниями, done — выполненные, pH / pM / pL — по приоритету,
#   c3 — категория №3 в списке категорий, c- — без категории, s — результаты поиска.
# Карточка открывается кнопкой с задачей и заменяет команды /edit, /done, /remind, /delete.

LIST_PAGE_SIZE = 8       # сколько задач на одной странице списка
DONE_LIST_LIMIT = 100    # сколько последних выполненных задач показывать
PRIORITY_CODES = {"pH": "high", "pM": "medium", "pL": "low"}
LIST_EMPTY = {
    "all": "Активных задач нет 🎉",
    "today": "На сегодня задач нет 🎉",
    "week": "На ближайшую неделю задач нет.",
    "rem": "Напоминаний нет. Поставить напоминание можно в карточке задачи.",
    "done": "Выполненных задач пока нет.",
    "s": "Ничего не нашлось.",
}
BACK_TO_LIST = InlineKeyboardMarkup([[button("⬅️ К задачам", "ml:v:all:0")]])


def filter_code(spec: tuple[str, str], user_id: int) -> str:
    """Старый фильтр /list («высокий», «рабочие», «неделя») -> код вида списка."""
    kind, value = spec
    if kind == "prio":
        return {"high": "pH", "medium": "pM", "low": "pL"}[value]
    if kind == "week":
        return "week"
    return f"c{user_categories(user_id).index(value)}" if value else "c-"


def list_view_tasks(user: dict, flt: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, list[dict], str]:
    """Заголовок, задачи и вид строк («task», «remind», «done») для фильтра списка."""
    if flt == "today":
        return "📅 На сегодня и просроченные", today_tasks(user), "task"
    if flt == "week":
        last = today() + timedelta(days=6)
        return f"🗓 Ближайшая неделя, срок до {fmt_day(last)}", filter_tasks(user, ("week", ""))[1], "task"
    if flt == "rem":
        tasks = [t for t in active_tasks(user) if t.get("remind_at")]
        tasks.sort(key=lambda t: datetime.fromisoformat(t["remind_at"]))
        return "⏰ С напоминаниями", tasks, "remind"
    if flt == "done":
        return "✅ Выполненные", db.done_history(user["user_id"], DONE_LIST_LIMIT), "done"
    if flt in PRIORITY_CODES:
        p = PRIORITIES[PRIORITY_CODES[flt]]
        return f"{p['emoji']} Приоритет «{p['name']}»", filter_tasks(user, ("prio", PRIORITY_CODES[flt]))[1], "task"
    if flt == "c-":
        return "🏷 Без категории", filter_tasks(user, ("cat", ""))[1], "task"
    if flt.startswith("c") and flt[1:].isdigit():
        categories = user_categories(user["user_id"])
        if int(flt[1:]) < len(categories):
            name = categories[int(flt[1:])]
            return f"🏷 {name}", filter_tasks(user, ("cat", name))[1], "task"
    if flt == "s" and context.user_data.get("last_search"):
        query = context.user_data["last_search"]
        tasks, exact = search_tasks(user, query)
        active = sorted((t for t in tasks if t["status"] == "active"), key=sort_key)
        done = sorted((t for t in tasks if t["status"] == "done"),
                      key=lambda t: t.get("done_at") or "", reverse=True)
        return f"🔍 Поиск «{query}»" + ("" if exact or not tasks else " (похожие)"), active + done, "task"
    return "📋 Активные задачи", active_tasks(user), "task"


def list_line(task: dict, kind: str) -> str:
    """Строка задачи в списке: с временем напоминания или датой выполнения, если нужно."""
    if task["status"] == "done":
        finished = done_date(task)
        return f"✅ {fmt_date(finished)} — {task_line(task)}" if finished else f"✅ {task_line(task)}"
    line = task_line(task)
    if kind == "remind":
        line += f" — ⏰ {fmt_when(datetime.fromisoformat(task['remind_at']))}"
        if task.get("remind_note"):
            line += f" ({task['remind_note']})"
    return line


def list_filter_rows(flt: str) -> list[list[InlineKeyboardButton]]:
    """Кнопки фильтров и инструментов под списком задач."""
    rows = [
        [button("📅 Сегодня", "ml:v:today:0"), button("🗓 Неделя", "ml:v:week:0")],
        [button("🏷 Категория", "ml:cats"), button("⚡ Приоритет", "ml:prios")],
        [button("⏰ Напоминания", "ml:v:rem:0"), button("✅ Выполненные", "ml:v:done:0")],
        [button("🔍 Поиск", "ml:find"), button("📊 Статистика", "ml:stats")],
        [button("🗓 Рабочие дни", "ml:wd")],
    ]
    if flt != "all":
        rows[-1].append(button("📋 Все задачи", "ml:v:all:0"))
    return rows


def render_task_list(user: dict, context: ContextTypes.DEFAULT_TYPE, flt: str, page: int,
                     prefix: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки списка: задачи текущей страницы кнопками, листание, фильтры."""
    title, tasks, kind = list_view_tasks(user, flt, context)
    pages = max(1, -(-len(tasks) // LIST_PAGE_SIZE))  # деление с округлением вверх
    page = min(max(page, 0), pages - 1)
    chunk = tasks[page * LIST_PAGE_SIZE:(page + 1) * LIST_PAGE_SIZE]

    header = f"{title}: {len(tasks)}" + (f" · стр. {page + 1} из {pages}" if pages > 1 else "")
    empty = LIST_EMPTY.get(flt, "Задач нет.")
    if flt == "done" and not tasks and any(t["archived"] for t in user["tasks"]):
        empty = "История пуста: выполненные задачи из неё убраны. В статистике они учитываются."
    body = "\n".join(list_line(t, kind) for t in chunk) or empty
    text = f"{prefix}{header}\n\n{body}"
    if chunk:
        text += "\n\nНажмите на задачу, чтобы открыть её карточку."

    rows = [[button(("✅ " if t["status"] == "done" else "") + button_label(t),
                    f"tc:o:{t['id']}:{flt}:{page}")] for t in chunk]
    paging = []
    if page > 0:
        paging.append(button("◀️ Предыдущие", f"ml:v:{flt}:{page - 1}"))
    if page < pages - 1:
        paging.append(button("Следующие ▶️", f"ml:v:{flt}:{page + 1}"))
    if paging:
        rows.append(paging)
    if flt == "done" and tasks:
        rows.append(list(HISTORY_CLEAR_KEYBOARD.inline_keyboard[0]))  # «Очистить всё», «Старше 30 дней»
    if flt == "all" and not tasks:
        rows.append([button("➕ Добавить задачу", "ml:add", GREEN)])
    rows += list_filter_rows(flt)
    return text, InlineKeyboardMarkup(rows)


async def show_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE, flt: str = "all",
                         page: int = 0, prefix: str = "") -> int:
    text, keyboard = render_task_list(get_user(context, update.effective_user.id), context, flt, page, prefix)
    await show_view(update, context, text, keyboard)
    return ConversationHandler.END


def category_picker(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор категории для списка: только категории, где есть активные задачи, с их числом."""
    counts = Counter(normalize(t.get("category") or "") for t in active_tasks(user))
    buttons = [button(f"{name} ({counts[normalize(name)]})", f"ml:v:c{i}:0")
               for i, name in enumerate(user_categories(user["user_id"])) if counts[normalize(name)]]
    rows = button_rows(buttons)
    if counts[""]:
        rows.append([button(f"Без категории ({counts['']})", "ml:v:c-:0")])
    rows.append([button("⬅️ Назад", "ml:v:all:0")])
    text = "🏷 Задачи какой категории показать?" if len(rows) > 1 else "Активных задач пока нет."
    return text, InlineKeyboardMarkup(rows)


def priority_picker(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор приоритета для списка, с числом активных задач."""
    counts = Counter(t["priority"] for t in active_tasks(user))
    codes = {"high": "pH", "medium": "pM", "low": "pL"}
    rows = [[button(f"{p['button']} ({counts[key]})", f"ml:v:{codes[key]}:0")] for key, p in PRIORITIES.items()]
    rows.append([button("⬅️ Назад", "ml:v:all:0")])
    return "⚡ Задачи какого приоритета показать?", InlineKeyboardMarkup(rows)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list, кнопка «Мои задачи» — раздел задач. Старый вид с фильтром тоже работает:
    /list высокий, /list рабочие, /list неделя, «список рабочие»."""
    user = get_user(context, update.effective_user.id)
    flt = "all"
    if context.args:
        wanted = " ".join(context.args)
        spec = match_phrase(wanted, list_filter_options(user["user_id"]))
        if spec is None:
            categories = ", ".join(user_categories(user["user_id"]))
            await update.message.reply_text(
                f"Не понял фильтр «{wanted}». {LIST_FILTER_EXAMPLE}\nКатегории: {categories}.")
            return
        flt = filter_code(spec, user["user_id"])
    await show_task_list(update, context, flt)


async def list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки раздела «Мои задачи» (ml:…). Работают и посреди диалога — он завершается."""
    query = update.callback_query
    await query.answer()
    await end_dialog_quietly(context)
    user = get_user(context, update.effective_user.id)
    parts = query.data.split(":")
    action = parts[1]
    if action == "v":
        return await show_task_list(update, context, parts[2], int(parts[3]))
    if action == "cats":
        await show_view(update, context, *category_picker(user))
    elif action == "prios":
        await show_view(update, context, *priority_picker(user))
    elif action == "stats":
        await show_view(update, context, stats_text(user), BACK_TO_LIST)
    elif action == "wd":
        await show_view(update, context, await workdays_text(), BACK_TO_LIST)
    elif action == "find":
        context.user_data["list_view"] = "all:0"
        await respond(update, context, SEARCH_PROMPT, with_nav(None, back="list"))
        return FIND_QUERY
    elif action == "add":
        return await add_start(update, context)
    return ConversationHandler.END


# --- Карточка задачи ------------------------------------------------------------------

def card_text(task: dict) -> str:
    """Всё о задаче: текст, приоритет, срок, категория, напоминание, даты."""
    p = PRIORITIES[task["priority"]]
    lines = [f"📌 Задача #{task['id']}", task["text"], "", f"{p['emoji']} Приоритет: {p['name']}"]
    due = task_due(task)
    if due and task["status"] == "active":
        days = (due - today()).days
        if days == 0:
            left = "сегодня"
        elif days == 1:
            left = "завтра"
        elif days > 1:
            left = f"через {days} {plural(days, 'день', 'дня', 'дней')}"
        else:
            left = f"⚠️ просрочено на {-days} {plural(-days, 'день', 'дня', 'дней')}"
        lines.append(f"📅 Срок: {fmt_day(due)} — {left}")
    elif due:
        lines.append(f"📅 Срок: {fmt_day(due)}")
    else:
        lines.append("📅 Без срока")
    lines.append(f"🏷 Категория: {task['category']}" if task.get("category") else "🏷 Без категории")
    if task["status"] == "active":
        if task.get("remind_at"):
            reminder = f"⏰ Напоминание: {fmt_when(datetime.fromisoformat(task['remind_at']))}"
            lines.append(reminder + (f" ({task['remind_note']})" if task.get("remind_note") else ""))
        else:
            lines.append("⏰ Без напоминания")
    created = datetime.fromisoformat(task["created_at"]).date()
    lines.append(f"🕓 Создана {fmt_date(created)}")
    if task["status"] == "done" and done_date(task):
        lines.append(f"✅ Выполнена {fmt_date(done_date(task))}")
    return "\n".join(lines)


def card_keyboard(task: dict, flt: str, page: int) -> InlineKeyboardMarkup:
    """Кнопки карточки. В каждой — номер задачи и вид списка, куда вернуться по «Назад»."""
    ref = f"{task['id']}:{flt}:{page}"
    if task["status"] == "active":
        rows = [
            [button("✏️ Текст", f"tc:a:text:{ref}"), button("⚡ Приоритет", f"tc:a:prio:{ref}")],
            [button("📅 Срок", f"tc:a:due:{ref}"), button("🏷 Категория", f"tc:a:cat:{ref}")],
            [button("⏰ Напоминание", f"tc:a:rem:{ref}")],
            [button("✅ Выполнено", f"tc:a:done:{ref}", GREEN), button("🗑 Удалить", f"tc:a:del:{ref}", RED)],
        ]
    else:
        rows = [[button("🗑 Удалить", f"tc:a:del:{ref}", RED)]]
    rows.append([button("⬅️ Назад", f"ml:v:{flt}:{page}")])
    return InlineKeyboardMarkup(rows)


async def show_card(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int,
                    flt: str = "all", page: int = 0, prefix: str = "") -> int:
    task = find_task(get_user(context, update.effective_user.id), task_id)
    if not task:
        return await show_task_list(update, context, flt, page, f"Задачи #{task_id} уже нет.\n\n")
    await show_view(update, context, prefix + card_text(task), card_keyboard(task, flt, page))
    return ConversationHandler.END


async def card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки карточки (tc:…): открыть карточку или изменить, напомнить, выполнить, удалить.
    Изменение использует те же шаги, что /edit, а после ответа бот возвращается в карточку."""
    query = update.callback_query
    await query.answer()
    await end_dialog_quietly(context)
    parts = query.data.split(":")
    if parts[1] == "o":  # tc:o:<номер>:<фильтр>:<страница>
        return await show_card(update, context, int(parts[2]), parts[3], int(parts[4]))

    action, task_id, flt, page = parts[2], int(parts[3]), parts[4], int(parts[5])
    user_id = update.effective_user.id
    task = find_task(get_user(context, user_id), task_id)
    if not task:
        return await show_task_list(update, context, flt, page, f"Задачи #{task_id} уже нет.\n\n")
    if action == "done":
        if task["status"] == "active":
            complete_task(context, user_id, task)
            prefix = f"✅ Задача #{task_id} выполнена: {task['text']}\n\n"
        else:
            prefix = f"Задача #{task_id} уже выполнена 👍\n\n"
        return await show_task_list(update, context, flt, page, prefix)

    context.user_data["card"] = {"id": task_id, "flt": flt, "page": page}  # куда вернуться
    if action == "del":
        return await ask_delete(update, context, task_id)
    if task["status"] != "active":
        context.user_data.pop("card")
        return await show_card(update, context, task_id, flt, page,
                               "Задача уже выполнена — изменить её нельзя.\n\n")
    if action == "rem":
        return await remind_task(update, context, task_id, [])
    context.user_data["edit"] = task_id
    context.user_data["draft"] = {}  # шаги изменения складывают ответы в черновик
    field = {"text": "text", "prio": "priority", "due": "due", "cat": "category"}[action]
    return await ask_edit_value(update, context, field)


async def return_to_card(update: Update, context: ContextTypes.DEFAULT_TYPE, result: str) -> int:
    """Итог действия, начатого из карточки, — и снова карточка задачи."""
    card = context.user_data.pop("card")
    return await show_card(update, context, card["id"], card["flt"], card["page"], f"{result}\n\n")


# ---------------------------------------------------------------------------
# Раздел «Настройки»: утренняя сводка, категории, файл задач
# ---------------------------------------------------------------------------
# Заменяет команды /digest, /categories, /export, /import (они по-прежнему работают).

SETTINGS_KEYBOARD = InlineKeyboardMarkup([
    [button("🔔 Утренняя сводка", "st:digest")],
    [button("🏷 Категории", "st:cats")],
    [button("📤 Выгрузить", "st:export"), button("📥 Загрузить", "st:import")],
])
BACK_TO_SETTINGS = InlineKeyboardMarkup([[button("⬅️ Назад", "st:home")]])


def import_prompt() -> str:
    """Подсказка «Загрузить»: формат файла (IMPORT_HELP описан в разделе выгрузки и загрузки)."""
    return ("📥 Загрузка задач из файла\n\nПришлите в этот чат CSV-файл с задачами — например, "
            f"выгруженный кнопкой «📤 Выгрузить».\n\n{IMPORT_HELP}")


def settings_text(user: dict) -> str:
    s = user["settings"]
    digest = (f"включена, {s['digest_time']}, {DIGEST_MODES[s['digest_mode']]}"
              if s["digest_enabled"] else "выключена")
    custom = len(db.custom_categories(user["user_id"]))
    return ("⚙️ Настройки\n\n"
            f"🔔 Утренняя сводка: {digest}\n"
            f"🏷 Категории: базовых {len(BASE_CATEGORIES)}, своих {custom}\n"
            "📁 Файл задач: выгрузить все задачи в файл для Excel или загрузить задачи из файла")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings, кнопка «Настройки»."""
    await show_view(update, context, settings_text(get_user(context, update.effective_user.id)),
                    SETTINGS_KEYBOARD)


def digest_panel(settings: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Настройки утренней сводки: включить или выключить, режим, время."""
    rows = [[button("🔕 Выключить", "st:dg:off") if settings["digest_enabled"]
             else button("🔔 Включить", "st:dg:on")]]
    rows += [[button(("✅ " if key == settings["digest_mode"] else "") + label, f"st:dg:{key}")]
             for key, label in DIGEST_MODE_BUTTONS.items()]  # по строке: «Только в рабочие дни» длинная
    rows.append([button(f"🕘 Изменить время ({settings['digest_time']})", "st:dg:time")])
    rows.append([button("⬅️ Назад", "st:home")])
    text = (f"🔔 Утренняя сводка\n\n{digest_status(settings)}\n\n"
            "Это задачи на сегодня и просроченные. «Только в рабочие дни» — без выходных "
            "и праздников по производственному календарю.")
    return text, InlineKeyboardMarkup(rows)


async def show_digest_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = "") -> None:
    text, keyboard = digest_panel(get_user(context, update.effective_user.id)["settings"])
    await show_view(update, context, prefix + text, keyboard)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки раздела «Настройки» (st:…). Работают и посреди диалога — он завершается."""
    query = update.callback_query
    await query.answer()
    await end_dialog_quietly(context)
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    parts = query.data.split(":")
    action = parts[1]
    if action == "digest":
        await show_digest_panel(update, context)
    elif action == "dg":
        value = parts[2]
        if value == "time":
            await respond(update, context, "🕘 Во сколько присылать сводку? Напишите время, например 08:30",
                          with_nav(None, back="digest"))
            return DIGEST_TIME
        settings = user["settings"]
        if value in ("on", "off"):
            settings["digest_enabled"] = value == "on"
        elif value in DIGEST_MODES:
            settings["digest_mode"] = value
            settings["digest_enabled"] = True  # выбор режима заодно включает сводку
        db.save_user(user)
        schedule_digest(context.job_queue, user_id, user)
        await show_digest_panel(update, context)
    elif action == "cats":
        await show_categories(update, context)
    elif action == "export":
        await send_export(query.message, user["tasks"])
    elif action == "import":
        await show_view(update, context, import_prompt(), BACK_TO_SETTINGS)
    else:  # home
        await show_view(update, context, settings_text(user), SETTINGS_KEYBOARD)
    return ConversationHandler.END


async def digest_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Новое время сводки из «Настроек»: «08:30»."""
    new_time = parse_time(normalize(update.message.text))
    if new_time is None:
        state = await try_text_command(update, context)
        if state is not None:
            return state
        await update.message.reply_text("Не понял время. Пример: 08:30")
        return DIGEST_TIME
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    user["settings"]["digest_time"] = new_time.strftime("%H:%M")
    user["settings"]["digest_enabled"] = True
    db.save_user(user)
    schedule_digest(context.job_queue, user_id, user)
    await show_digest_panel(update, context, f"✅ Сводка будет приходить в {user['settings']['digest_time']}.\n\n")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------

REMIND_EXAMPLE = "Пример: /remind 3 18:30 или /remind 3 14.10 18:30"
CUSTOM_TIME_EXAMPLE = "Пример: 18:30 или 14.10 18:30"
REMIND_MORNING = time(10, 0)  # «завтра утром», «в день срока», «за сутки» и т. п.
REMIND_EVENING = time(19, 0)  # «сегодня вечером»
REMIND_MIN_AHEAD = timedelta(minutes=5)  # совсем близкие к «сейчас» варианты не предлагаем
PICK_LIMIT = 10            # сколько задач показывать кнопками
BUTTON_TEXT_LIMIT = 30     # сколько символов текста задачи помещается на кнопку


def parse_remind_moment(parts: list[str], example: str) -> tuple[datetime | None, str | None, bool]:
    """«18:30» или «14.10 18:30» -> момент напоминания.

    Если указано только время и сегодня оно уже прошло — напоминаем завтра.
    Возвращает (момент, None, перенесено ли на завтра) или (None, текст ошибки, False).
    """
    if len(parts) not in (1, 2):
        return None, f"Не понял время. {example}", False
    remind_time = parse_time(parts[-1])
    if remind_time is None:
        return None, f"Не понял время. {example}", False
    if len(parts) == 2:
        remind_date = parse_date(parts[0])
        if remind_date is None:
            return None, f"Не понял дату. {example}", False
        when = datetime.combine(remind_date, remind_time, tzinfo=TZ)
        if when <= now():
            return None, "Это время уже прошло. Укажите момент в будущем.", False
        return when, None, False
    when = datetime.combine(today(), remind_time, tzinfo=TZ)
    if when > now():
        return when, None, False
    return when + timedelta(days=1), None, True


def looks_like_time(parts: list[str]) -> bool:
    """Похоже ли сообщение на время («18:30», «14.10 18:30»), даже если в нём ошибка («25:00»)."""
    return bool(parts) and len(parts) <= 2 and re.fullmatch(TIME_RE, parts[-1]) is not None


def set_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, task: dict,
                 when: datetime, note: str | None = None, moved: bool = False) -> str:
    """Ставит напоминание (старое заменяется: у задачи оно одно) и возвращает подтверждение.
    moved — время указали без даты, а сегодня оно уже прошло: прямо пишем, что напомним завтра."""
    task["remind_at"] = when.isoformat(timespec="seconds")
    task["remind_note"] = note
    db.save_task(user_id, task)
    schedule_reminder(context.job_queue, user_id, task)
    if moved:
        hhmm = when.strftime("%H:%M")
        return (f"⏰ Сегодня {hhmm} уже прошло, поэтому напомню о задаче #{task['id']} завтра — "
                f"{fmt_day(when.date())} в {hhmm}.")
    text = f"⏰ Напомню о задаче #{task['id']} {fmt_when(when)}"
    return f"{text} ({note})" if note else text


def clear_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, task: dict) -> bool:
    """Снимает напоминание с задачи. Возвращает True, если оно было."""
    had_reminder = bool(task.get("remind_at"))
    task["remind_at"] = None
    task["remind_note"] = None
    cancel_jobs(context.job_queue, reminder_job_name(user_id, task["id"]))
    db.save_task(user_id, task)
    return had_reminder


def month_before(d: date) -> date:
    """То же число предыдущего месяца, а если такого числа нет — последний день месяца.
    Например, 15.11 -> 15.10, 31.03 -> 28.02 (или 29.02 в високосный год)."""
    year, month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


# Варианты, привязанные к сроку задачи
DUE_OPTION_KEYS = ("due", "day", "week", "month")


def due_relative_time(key: str, due: date) -> datetime:
    """Момент варианта «в день срока», «за сутки», «за неделю», «за месяц до срока» (в 10:00)."""
    day = {"due": due, "day": due - timedelta(days=1), "week": due - timedelta(days=7),
           "month": month_before(due)}[key]
    return datetime.combine(day, REMIND_MORNING, tzinfo=TZ)


def all_remind_variants(task: dict) -> dict[str, tuple[str, datetime, str | None]]:
    """Все варианты напоминания: ключ -> (текст кнопки, момент, пояснение для подтверждения)."""
    current = now()
    variants = {
        "hour": ("Через час", current + timedelta(hours=1), "через час"),
        "3h": ("Через 3 часа", current + timedelta(hours=3), "через 3 часа"),
        "evening": ("Сегодня вечером", datetime.combine(current.date(), REMIND_EVENING, tzinfo=TZ),
                    "сегодня вечером"),
        "morning": ("Завтра утром", datetime.combine(current.date() + timedelta(days=1), REMIND_MORNING,
                                                     tzinfo=TZ), "завтра утром"),
        "week_later": ("Через неделю", current + timedelta(days=7), "через неделю"),
    }
    due = task_due(task)
    if due:
        # На кнопках короткие подписи: ширину кнопок Telegram задать не даёт, и при двух
        # колонках «За сутки до срока» обрезалось бы. Полное название — в пояснении.
        for key, label, note in (("due", "В день срока", "в день срока"),
                                 ("day", "За сутки", "за сутки до срока"),
                                 ("week", "За неделю", "за неделю до срока"),
                                 ("month", "За месяц", "за месяц до срока")):
            variants[key] = (label, due_relative_time(key, due), note)
    return variants


def suggested_remind_keys(due: date | None, day: date) -> list[str]:
    """Какие варианты предложить — в зависимости от того, сколько осталось до срока."""
    if due is None:
        return ["hour", "evening", "morning", "week_later"]
    days_left = (due - day).days
    if days_left < 0:
        return []  # срок прошёл: остаётся только «Указать время»
    if days_left == 0:
        return ["hour", "3h", "evening"]
    if days_left == 1:
        return ["hour", "evening", "morning"]
    if days_left < 7:
        return ["morning", "day", "due"]
    keys = ["week", "day", "due"]
    return ["month"] + keys if month_before(due) > day else keys  # «за месяц» — если до срока больше месяца


def remind_options(task: dict) -> dict[str, tuple[str, datetime | None, str | None]]:
    """Варианты напоминания для кнопок: ключ -> (текст кнопки, момент, пояснение).

    Только разумные варианты: в будущем и не позже конца дня срока, без повторов
    (например, при сроке через 2 дня «завтра утром» и «за сутки» — одно и то же время).
    «Указать время» есть всегда — вручную можно поставить любое время, в том числе после срока.
    """
    current = now()
    due = task_due(task)
    deadline = datetime.combine(due, time(23, 59), tzinfo=TZ) if due else None
    variants = all_remind_variants(task)
    chosen: list[str] = []
    for key in suggested_remind_keys(due, current.date()):
        when = variants[key][1]
        if when < current + REMIND_MIN_AHEAD or (deadline and when > deadline):
            continue  # уже прошло (или вот-вот) либо позже срока
        if any(variants[other][1] == when for other in chosen):
            continue  # то же время, что у другого варианта
        chosen.append(key)
    chosen.sort(key=lambda key: variants[key][1])  # по времени: сначала ближайшие
    options = {key: variants[key] for key in chosen}
    options["custom"] = ("Указать время", None, None)
    options["none"] = ("Не напоминать", None, None)
    return options


# Текстовые ответы на «Когда напомнить?» — в том числе для вариантов, которых сейчас нет на
# кнопках: тогда бот объяснит, почему вариант не подходит
REMIND_ALIASES = {
    "через час": "hour", "через 3 часа": "3h", "через три часа": "3h",
    "сегодня вечером": "evening", "вечером": "evening",
    "завтра утром": "morning", "завтра": "morning", "утром": "morning",
    "через неделю": "week_later",
    "в день срока": "due", "в день срока утром": "due",
    "за сутки": "day", "за сутки до срока": "day",
    "за неделю": "week", "за неделю до срока": "week",
    "за месяц": "month", "за месяц до срока": "month",
    "указать время": "custom", "не напоминать": "none", "нет": "none", "не надо": "none",
}


def remind_keyboard(options: dict) -> InlineKeyboardMarkup:
    """Кнопки вариантов по две в ряд, «Не напоминать» — отдельной строкой внизу."""
    buttons = [InlineKeyboardButton(label, callback_data=f"ropt:{key}")
               for key, (label, _, _) in options.items() if key != "none"]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(options["none"][0], callback_data="ropt:none")])
    return InlineKeyboardMarkup(rows)


def remind_option_words(options: dict) -> dict[str, str]:
    """Текстовые ответы: названия кнопок и короткие формы («за сутки» -> "day")."""
    words = {normalize(label): key for key, (label, _, _) in options.items()}
    words.update(REMIND_ALIASES)
    return words


def due_options_note(task: dict) -> str:
    """Пояснение под вопросом: кнопки «За …» считаются от срока; если срок прошёл — что делать."""
    due = task_due(task)
    if due and due < today():
        return "\nСрок задачи уже прошёл — укажите время вручную."
    options = remind_options(task)
    return "\nВарианты «за …» — до срока" if any(k in options for k in ("day", "week", "month")) else ""


def remind_back(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Куда ведёт «Назад» из вопроса о напоминании: к категории (сразу после добавления задачи),
    в карточку задачи — или никуда (только «Отмена»), если вопрос задан командой /remind."""
    if context.user_data.get("remind_after_add"):
        return "category"
    return "card" if context.user_data.get("card") else None


def remind_question(context: ContextTypes.DEFAULT_TYPE, task: dict) -> str:
    if context.user_data.get("remind_after_add"):
        return f"{context.user_data.get('remind_prefix', '')}Напомнить о задаче?"
    return f"Когда напомнить?\n{task_line(task)}"


def remind_option_hint(options: dict) -> str:
    first_label = next(iter(options.values()))[0].lower()
    return (f"Выберите вариант кнопкой или напишите его, например «{first_label}», "
            f"либо время: 18:30 или 14.10 18:30.")


# --- /remind ---------------------------------------------------------------------

async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/remind — выбрать задачу кнопками; /remind 3 — варианты напоминания;
    /remind 3 18:30 или /remind 3 14.10 18:30 — сразу на указанное время."""
    await end_dialog_quietly(context)
    args = context.args or []
    if not args:
        return await ask_pick_task(update, context, "remind")
    if len(args) > 3:
        await update.message.reply_text(f"Укажите номер задачи и время. {REMIND_EXAMPLE}")
        return ConversationHandler.END
    task_id = parse_task_id(args[0])
    if task_id is None:
        await update.message.reply_text(f"Не понял номер задачи. {REMIND_EXAMPLE}")
        return ConversationHandler.END
    return await remind_task(update, context, task_id, args[1:])


async def remind_task(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      task_id: int, time_parts: list[str]) -> int:
    """Напоминание для задачи: без времени — показываем варианты, со временем — ставим сразу."""
    user_id = update.effective_user.id
    task = find_task(get_user(context, user_id), task_id)
    if not task:
        await respond(update, context, f"Задачи #{task_id} нет. Посмотреть номера: /list")
        return ConversationHandler.END
    if task["status"] == "done":
        await respond(update, context, f"Задача #{task_id} уже выполнена — напоминать не о чем 👍")
        return ConversationHandler.END
    if not time_parts:
        return await ask_remind_option(update, context, task, remind_question(context, task))
    when, error, moved = parse_remind_moment(time_parts, REMIND_EXAMPLE)
    if error:
        await respond(update, context, error)
        return ConversationHandler.END
    await respond(update, context, set_reminder(context, user_id, task, when, moved=moved))
    return ConversationHandler.END


# --- Выбор задачи кнопками (/done, /delete, /remind, /edit без номера) ---------------

PICK_PROMPTS = {
    "done": "Какую задачу отметить выполненной?\nНажмите кнопку или напишите номер.",
    "delete": "Какую задачу удалить?\nНажмите кнопку или напишите номер.",
    "edit": "Какую задачу изменить?\nНажмите кнопку или напишите номер.",
    "remind": ("Для какой задачи поставить напоминание?\n"
               "Нажмите кнопку или напишите номер — можно сразу со временем: 1 11:43"),
}
PICK_HINTS = {
    "done": "Нажмите кнопку с задачей или напишите её номер, например 3.",
    "delete": "Нажмите кнопку с задачей или напишите её номер, например 3.",
    "edit": "Нажмите кнопку с задачей или напишите её номер, например 3.",
    "remind": "Нажмите кнопку с задачей или напишите номер, например 3 или 3 18:30.",
}


def button_label(task: dict) -> str:
    """«#4 Подготовить отчёт по лаб…»: номер и начало текста — не больше 30 символов."""
    text = task["text"]
    if len(text) > BUTTON_TEXT_LIMIT:
        text = text[:BUTTON_TEXT_LIMIT - 1].rstrip() + "…"
    return f"#{task['id']} {text}"


async def ask_pick_task(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> int:
    """Показывает активные задачи кнопками (в порядке /list) для done / delete / remind / edit."""
    tasks = active_tasks(get_user(context, update.effective_user.id))
    if not tasks:
        await update.message.reply_text("Активных задач нет 🎉 Добавить: /add")
        return ConversationHandler.END
    context.user_data["pick"] = action
    prompt = PICK_PROMPTS[action]
    if len(tasks) > PICK_LIMIT:
        prompt += f"\nПоказаны первые {PICK_LIMIT} из {len(tasks)}, номер любой задачи можно написать."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(button_label(t), callback_data=f"pick:{t['id']}")]
        for t in tasks[:PICK_LIMIT]
    ])
    await respond(update, context, prompt, with_nav(keyboard))
    return PICK_TASK


async def pick_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажата кнопка с задачей."""
    query = update.callback_query
    await query.answer()
    return await pick_task(update, context, int(query.data.split(":")[1]), [])


async def pick_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Номер задачи текстом. Для /remind можно сразу со временем: «1 11:43», «1 14.10 11:43»."""
    action = context.user_data.get("pick")
    parts = normalize(update.message.text).split()
    task_id = parse_task_id(parts[0]) if parts else None
    max_parts = 3 if action == "remind" else 1
    if task_id is not None and len(parts) <= max_parts:
        return await pick_task(update, context, task_id, parts[1:])
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(PICK_HINTS.get(action, PICK_HINTS["done"]))
    return PICK_TASK


async def pick_task(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    task_id: int, rest: list[str]) -> int:
    """Задача выбрана — продолжаем ту команду, ради которой её выбирали."""
    action = context.user_data.pop("pick", None)
    if action == "done":
        await respond(update, context, complete_by_id(context, update.effective_user.id, task_id))
        return ConversationHandler.END
    if action == "delete":
        return await ask_delete(update, context, task_id)
    if action == "remind":
        return await remind_task(update, context, task_id, rest)
    if action == "edit":
        return await ask_edit_field(update, context, task_id)
    return ConversationHandler.END


# --- Выбор варианта напоминания -----------------------------------------------------

async def ask_remind_option(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            task: dict, question: str) -> int:
    """Показывает варианты напоминания для задачи."""
    context.user_data["remind_id"] = task["id"]
    await respond(update, context, question + due_options_note(task),
                  with_nav(remind_keyboard(remind_options(task)), back=remind_back(context)))
    return REMIND_OPTION


def task_in_remind_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Задача, для которой сейчас выбираем напоминание (None, если её удалили или выполнили)."""
    task_id = context.user_data.get("remind_id")
    task = find_task(get_user(context, update.effective_user.id), task_id) if task_id else None
    return task if task and task["status"] == "active" else None


async def finish_remind_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE, result: str) -> int:
    """Показывает итог и завершает диалог.
    Если вопрос задан сразу после добавления задачи и ответили кнопкой, то сообщение
    редактируется — поэтому сохраняем в нём подтверждение «Задача #N добавлена…»."""
    prefix = context.user_data.get("remind_prefix", "") if update.callback_query else ""
    for key in ("remind_id", "remind_after_add", "remind_prefix", "draft"):  # добавление закончено
        context.user_data.pop(key, None)
    if context.user_data.get("card"):  # напоминание ставили из карточки — возвращаемся в неё
        return await return_to_card(update, context, result)
    await respond(update, context, prefix + result)
    return ConversationHandler.END


async def remind_option_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажата кнопка варианта напоминания."""
    query = update.callback_query
    await query.answer()
    return await apply_remind_option(update, context, query.data.split(":", 1)[1])


async def remind_option_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом: вариант («за сутки», «звтра»), сразу время («18:30») или что-то другое."""
    task = task_in_remind_dialog(update, context)
    if not task:
        return await finish_remind_dialog(update, context, "Задача уже удалена или выполнена.")
    options = remind_options(task)
    key = match_phrase(update.message.text, remind_option_words(options))
    if key:
        return await apply_remind_option(update, context, key)
    parts = normalize(update.message.text).split()
    if looks_like_time(parts):
        return await apply_custom_time(update, context, parts)
    if context.user_data.get("remind_after_add"):
        if match_phrase(update.message.text, YES_NO_WORDS) is True:  # «да» — но когда именно?
            await update.message.reply_text(remind_option_hint(options))
            return REMIND_OPTION
        return await close_question_after_add(update, context)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(remind_option_hint(options))
    return REMIND_OPTION


async def apply_remind_option(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> int:
    """Общая логика для кнопки и текста: ставим, снимаем или просим указать время."""
    task = task_in_remind_dialog(update, context)
    if not task:
        return await finish_remind_dialog(update, context, "Задача уже удалена или выполнена.")
    options = remind_options(task)
    if key not in options:
        if key in DUE_OPTION_KEYS and not task_due(task):
            reason = "У задачи нет срока, этот вариант не подходит."
        else:
            label = all_remind_variants(task).get(key, (key,))[0]
            reason = (f"Вариант «{label}» сейчас не подходит: напоминание должно быть в будущем "
                      "и не позже срока задачи.")
        return await ask_remind_again(update, context, task,
                                      f"{reason} Выберите из кнопок или «Указать время».")
    label, when, note = options[key]
    if when is not None and when <= now():
        name = note[0].upper() + note[1:] if note else label  # «За неделю до срока»
        return await ask_remind_again(update, context, task,
                                      f"Время для варианта «{name}» уже прошло "
                                      f"({fmt_when(when)}). Выберите другой вариант.")
    if key == "custom":
        prefix = context.user_data.get("remind_prefix", "") if update.callback_query else ""
        await respond(update, context,
                      f"{prefix}🕒 Напишите время: ЧЧ:ММ или ДД.ММ ЧЧ:ММ. {CUSTOM_TIME_EXAMPLE}\n"
                      "Можно любое время, в том числе после срока.",
                      with_nav(None, back="remind"))
        return REMIND_TIME
    user_id = update.effective_user.id
    if key == "none":
        if clear_reminder(context, user_id, task):
            result = f"🔕 Напоминание о задаче #{task['id']} снято."
        else:
            result = "Ок, без напоминания."
    else:
        _, when, note = options[key]
        result = set_reminder(context, user_id, task, when, note)
    return await finish_remind_dialog(update, context, result)


async def ask_remind_again(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           task: dict, warning: str) -> int:
    """Выбранный вариант не подошёл: оставляем кнопки и объясняем почему."""
    text = warning + due_options_note(task)
    if update.callback_query:
        # Сообщение с кнопками редактируется — сохраняем в нём, о какой задаче речь
        prefix = context.user_data.get("remind_prefix") or f"{task_line(task)}\n\n"
        text = prefix + text
    await respond(update, context, text,
                  with_nav(remind_keyboard(remind_options(task)), back=remind_back(context)))
    return REMIND_OPTION


async def apply_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            parts: list[str]) -> int:
    """Напоминание на время, написанное текстом: «18:30» или «14.10 18:30»."""
    task = task_in_remind_dialog(update, context)
    if not task:
        return await finish_remind_dialog(update, context, "Задача уже удалена или выполнена.")
    when, error, moved = parse_remind_moment(parts, CUSTOM_TIME_EXAMPLE)
    if error:
        await update.message.reply_text(error)
        return REMIND_TIME
    result = set_reminder(context, update.effective_user.id, task, when, moved=moved)
    return await finish_remind_dialog(update, context, result)


async def remind_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """После «Указать время»: ждём ЧЧ:ММ или ДД.ММ ЧЧ:ММ."""
    parts = normalize(update.message.text).split()
    if looks_like_time(parts):
        return await apply_custom_time(update, context, parts)
    if context.user_data.get("remind_after_add"):
        return await close_question_after_add(update, context)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(f"Не понял время. {CUSTOM_TIME_EXAMPLE}")
    return REMIND_TIME


async def close_question_after_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Вместо ответа на «Напомнить о задаче?» написали новую задачу или команду:
    молча закрываем вопрос и обрабатываем сообщение как обычно."""
    await end_dialog_quietly(context)
    return await quick_offer(update, context)


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под напоминанием: «✅ Выполнено» и «⏰ Отложить на 1 час»."""
    query = update.callback_query
    await query.answer()
    action, raw_id = query.data.split(":")
    task_id = int(raw_id)
    user_id = update.effective_user.id
    task = find_task(get_user(context, user_id), task_id)

    if not task:
        await query.edit_message_text(f"Задача #{task_id} уже удалена.")
        return
    if task["status"] == "done":
        await query.edit_message_text(f"✅ Задача #{task_id} уже выполнена.")
        return

    if action == "rdone":
        complete_task(context, user_id, task)
        await query.edit_message_text(f"✅ Задача #{task_id} выполнена: {task['text']}")
    else:  # snooze
        when = now() + timedelta(hours=1)
        task["remind_at"] = when.isoformat(timespec="seconds")
        task["remind_note"] = None
        db.save_task(user_id, task)
        schedule_reminder(context.job_queue, user_id, task)
        await query.edit_message_text(f"{task_line(task)}\n⏰ Отложено, напомню {fmt_when(when)}")


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reminders — задачи с установленными напоминаниями, ближайшие первыми."""
    user = get_user(context, update.effective_user.id)
    tasks = [t for t in user["tasks"] if t["status"] == "active" and t.get("remind_at")]
    if not tasks:
        await update.message.reply_text("Напоминаний нет. Поставить: /remind")
        return
    tasks.sort(key=lambda t: datetime.fromisoformat(t["remind_at"]))
    lines = []
    for t in tasks:
        line = f"{task_line(t)} — ⏰ {fmt_when(datetime.fromisoformat(t['remind_at']))}"
        if t.get("remind_note"):
            line += f" ({t['remind_note']})"  # например, «за сутки до срока»
        lines.append(line)
    await update.message.reply_text(join_lines(f"⏰ Напоминания ({len(tasks)}):\n", lines))


DIGEST_EXAMPLE = ("Пример: /digest on, /digest off, /digest 08:30, "
                  "/digest рабочие или /digest каждый день")
# Режимы сводки: ключ хранится в базе (users.digest_mode)
DIGEST_MODES = {"daily": "каждый день", "workdays": "только в рабочие дни"}
DIGEST_MODE_BUTTONS = {"daily": "Каждый день", "workdays": "Только в рабочие дни"}
DIGEST_MODE_WORDS = {"рабочие": "workdays", "рабочие дни": "workdays", "в рабочие дни": "workdays",
                     "только в рабочие дни": "workdays", "workdays": "workdays",
                     "каждый день": "daily", "ежедневно": "daily", "все дни": "daily",
                     "daily": "daily"}


def digest_status(settings: dict) -> str:
    """«🔔 Сводка включена: приходит в 09:00, только в рабочие дни.»"""
    if not settings["digest_enabled"]:
        return "🔕 Ежедневная сводка выключена."
    mode = DIGEST_MODES[settings["digest_mode"]]
    return f"🔔 Сводка включена: приходит в {settings['digest_time']}, {mode}."


def digest_keyboard(mode: str) -> InlineKeyboardMarkup:
    """Кнопки режима, выбранный отмечен галочкой. Каждая кнопка — на всю строку:
    «Только в рабочие дни» в две колонки не поместится."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if key == mode else "") + label, callback_data=f"dmode:{key}")]
        for key, label in DIGEST_MODE_BUTTONS.items()
    ])


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/digest — настройки и кнопки режима; /digest on|off, /digest 08:30,
    /digest рабочие, /digest каждый день."""
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    settings = user["settings"]

    if not context.args:
        await update.message.reply_text(f"{digest_status(settings)}\n{DIGEST_EXAMPLE}",
                                        reply_markup=digest_keyboard(settings["digest_mode"]))
        return

    wanted = " ".join(context.args).lower()
    mode = match_phrase(wanted, DIGEST_MODE_WORDS)
    if wanted == "off":
        settings["digest_enabled"] = False
        reply = "🔕 Ежедневная сводка выключена."
    elif wanted == "on":
        settings["digest_enabled"] = True
        reply = digest_status(settings)
    elif parse_time(wanted):
        settings["digest_time"] = parse_time(wanted).strftime("%H:%M")
        settings["digest_enabled"] = True
        reply = f"🔔 Сводка будет приходить в {settings['digest_time']}, {DIGEST_MODES[settings['digest_mode']]}."
    elif mode:
        settings["digest_mode"] = mode
        settings["digest_enabled"] = True
        reply = f"🔔 Сводка будет приходить {DIGEST_MODES[mode]} в {settings['digest_time']}."
    else:
        await update.message.reply_text(f"Не понял параметр. {DIGEST_EXAMPLE}")
        return

    db.save_user(user)
    schedule_digest(context.job_queue, user_id, user)
    await update.message.reply_text(reply)


async def digest_mode_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки «Каждый день» / «Только в рабочие дни» под /digest. Выбор режима включает сводку."""
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    if mode not in DIGEST_MODES:
        return
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    user["settings"]["digest_mode"] = mode
    user["settings"]["digest_enabled"] = True
    db.save_user(user)
    schedule_digest(context.job_queue, user_id, user)
    await query.edit_message_text(f"{digest_status(user['settings'])}\n{DIGEST_EXAMPLE}",
                                  reply_markup=digest_keyboard(mode))


# ---------------------------------------------------------------------------
# Поиск и история
# ---------------------------------------------------------------------------

SEARCH_PROMPT = "🔍 Что найти? Напишите слово из задачи, например: отчёт"
HISTORY_LIMIT = 10


def done_date(task: dict) -> date | None:
    return datetime.fromisoformat(task["done_at"]).date() if task.get("done_at") else None


def search_tasks(user: dict, query: str) -> tuple[list[dict], bool]:
    """Задачи, где встречаются все слова запроса. Сначала ищем точно (запросом к базе);
    если ничего нет — слова с небольшими опечатками. Возвращает (задачи, точный ли поиск)."""
    words = normalize(query).split()
    found = db.search_tasks(user["user_id"], words)
    if found:
        return found, True

    def similar(task: dict) -> bool:
        task_words = re.findall(r"\w+", normalize(f"{task['text']} {task.get('category') or ''}"))
        return all(any(edit_distance(w, tw) <= typo_limit(w) for tw in task_words) for w in words)

    return [t for t in user["tasks"] if not t["archived"] and similar(t)], False


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/find <слово> — поиск по задачам. Без слова бот спросит, что искать."""
    await end_dialog_quietly(context)
    query = " ".join(context.args or []).strip()
    if not query:
        await respond(update, context, SEARCH_PROMPT, with_nav(None))
        return FIND_QUERY
    await send_search_results(update, context, query)
    return ConversationHandler.END


async def find_query_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ на «Что найти?». Любой текст подходит как запрос, поэтому сначала проверяем
    текстовые команды («отмена», «список»), иначе их было бы не набрать."""
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await send_search_results(update, context, update.message.text)
    return ConversationHandler.END


async def send_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    query = " ".join(query.split())
    user = get_user(context, update.effective_user.id)
    tasks, exact = search_tasks(user, query)
    if not tasks:
        text = f"🔍 По запросу «{query}» ничего не нашлось."
        source = context.user_data.get("text_command_source")
        if source:  # «Найти ключи» могло быть задачей, а не поиском — подскажем, как её добавить
            text += f"\nЕсли это задача, добавьте её так: /add {source}"
        await update.message.reply_text(text)
        return

    # Результаты — списком с кнопками: найденную задачу можно сразу открыть
    context.user_data["last_search"] = query
    await show_task_list(update, context, "s", 0)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history — последние выполненные задачи."""
    user = get_user(context, update.effective_user.id)
    tasks = db.done_history(user["user_id"], HISTORY_LIMIT)
    if not tasks:
        if any(t["archived"] for t in user["tasks"]):  # выполненные есть, но их убрали из истории
            await update.message.reply_text("📜 История пуста: выполненные задачи из неё убраны. "
                                            "В /stats они по-прежнему учитываются.")
        else:
            await update.message.reply_text("📜 Выполненных задач пока нет. Отметить задачу: /done")
        return
    total = sum(1 for t in user["tasks"] if t["status"] == "done" and not t["archived"])
    lines = []
    for t in tasks:
        finished, due = done_date(t), task_due(t)
        line = f"✅ {fmt_date(finished)} — {task_line(t)}" if finished else f"✅ {task_line(t)}"
        if finished and due and finished > due:
            line += " (позже срока)"
        lines.append(line)
    header = f"📜 Последние выполненные задачи ({len(tasks)} из {total}):\n"
    await update.message.reply_text(join_lines(header, lines), reply_markup=HISTORY_CLEAR_KEYBOARD)


# --- Очистка истории ---------------------------------------------------------------
# Задачи не удаляются, а помечаются archived = 1: они пропадают из /history и /find,
# но /stats их по-прежнему считает, и статистика не обнуляется.

HISTORY_KEEP_DAYS = 30
HISTORY_CLEAR_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Очистить всё", callback_data="hclear:all"),
    InlineKeyboardButton(f"Старше {HISTORY_KEEP_DAYS} дней", callback_data="hclear:old"),
]])
HISTORY_CONFIRM_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Да", callback_data="hconf:yes"),
    InlineKeyboardButton("Нет", callback_data="hconf:no"),
]])
HISTORY_SCOPE_WORDS = {"все": "all", "всю": "all", "всю историю": "all", "очистить все": "all",
                       f"старше {HISTORY_KEEP_DAYS} дней": "old", "старые": "old",
                       "старше месяца": "old"}
HISTORY_SCOPE_HINT = f"Напишите «всё» или «старше {HISTORY_KEEP_DAYS} дней», либо нажмите кнопку."
CLEAR_WORDS = {**YES_NO_WORDS, "очистить": True, "удалить": True}


def history_tasks_to_clear(user: dict, scope: str) -> list[dict]:
    """Выполненные задачи, которые сейчас видны в истории: все или выполненные
    больше 30 дней назад (scope = "all" или "old")."""
    tasks = [t for t in user["tasks"] if t["status"] == "done" and not t["archived"]]
    if scope == "old":
        cutoff = now() - timedelta(days=HISTORY_KEEP_DAYS)
        tasks = [t for t in tasks if t.get("done_at") and datetime.fromisoformat(t["done_at"]) < cutoff]
    return tasks


async def clear_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«очистить историю» (или /clear_history): спрашиваем, что убрать, затем подтверждение."""
    await end_dialog_quietly(context)
    if context.args:
        scope = match_phrase(" ".join(context.args), HISTORY_SCOPE_WORDS)
        if scope:
            return await ask_clear_confirm(update, context, scope)
    user = get_user(context, update.effective_user.id)
    if not history_tasks_to_clear(user, "all"):
        await update.message.reply_text("📜 История и так пуста.")
        return ConversationHandler.END
    await respond(update, context, "🧹 Что убрать из истории?", with_nav(HISTORY_CLEAR_KEYBOARD))
    return HISTORY_SCOPE


async def history_clear_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопки «Очистить всё» / «Старше 30 дней» — под /history или под вопросом «Что убрать?».
    Срабатывают и посреди другого диалога: он молча завершается."""
    query = update.callback_query
    await query.answer()
    await end_dialog_quietly(context)
    return await ask_clear_confirm(update, context, query.data.split(":", 1)[1])


async def history_scope_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Что убрать из истории?»."""
    scope = match_phrase(update.message.text, HISTORY_SCOPE_WORDS)
    if scope:
        return await ask_clear_confirm(update, context, scope)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(HISTORY_SCOPE_HINT)
    return HISTORY_SCOPE


async def ask_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str) -> int:
    """Перед очисткой спрашиваем «Да» / «Нет». Вопрос — отдельным сообщением,
    чтобы список /history над ним не пропал."""
    await drop_keyboard(context)
    tasks = history_tasks_to_clear(get_user(context, update.effective_user.id), scope)
    chat_id = update.effective_chat.id
    if not tasks:
        text = ("📜 История и так пуста." if scope == "all" else
                f"📜 В истории нет задач, выполненных больше {HISTORY_KEEP_DAYS} дней назад.")
        await context.bot.send_message(chat_id, text)
        return ConversationHandler.END
    what = ("все выполненные задачи" if scope == "all" else
            f"задачи, выполненные больше {HISTORY_KEEP_DAYS} дней назад")
    context.user_data["clear_scope"] = scope
    message = await context.bot.send_message(
        chat_id, f"🧹 Убрать из истории {what} ({len(tasks)})? В /stats они останутся.",
        reply_markup=HISTORY_CONFIRM_KEYBOARD)
    context.user_data["kb_message"] = (message.chat_id, message.message_id)
    return HISTORY_CONFIRM


async def history_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await history_clear_answer(update, context, query.data == "hconf:yes")


async def history_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Убрать из истории…?»: «да», «очистить», «нет»…"""
    yes = match_phrase(update.message.text, CLEAR_WORDS)
    if yes is not None:
        return await history_clear_answer(update, context, yes)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(YES_NO_HINT)
    return HISTORY_CONFIRM


async def history_clear_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, yes: bool) -> int:
    scope = context.user_data.pop("clear_scope", None)
    if not yes or scope is None:
        await respond(update, context, "Ок, история не тронута.")
        return ConversationHandler.END
    user = get_user(context, update.effective_user.id)
    tasks = history_tasks_to_clear(user, scope)
    db.archive_tasks(user["user_id"], [t["id"] for t in tasks])
    n = len(tasks)
    await respond(update, context,
                  f"🧹 Из истории {plural(n, 'убрана', 'убраны', 'убрано')} {n} "
                  f"{plural(n, 'задача', 'задачи', 'задач')}. В /stats "
                  f"{plural(n, 'она по-прежнему учитывается', 'они по-прежнему учитываются', 'они по-прежнему учитываются')}.")
    return ConversationHandler.END




# ---------------------------------------------------------------------------
# Выгрузка и загрузка задач (CSV, открывается в Excel)
# ---------------------------------------------------------------------------
# Формат — как в data/sample_tasks.csv: UTF-8 с BOM (по нему Excel узнаёт кодировку),
# разделитель «;» (его ждёт Excel с русскими настройками), даты ДД.ММ.ГГГГ.

CSV_COLUMNS = ["номер", "задача", "приоритет", "категория", "срок", "статус", "создана", "выполнена"]
STATUS_NAMES = {"active": "активна", "done": "выполнена"}
STATUS_WORDS = {"активна": "active", "активная": "active", "активно": "active",
                "выполнена": "done", "выполнено": "done", "выполненная": "done",
                "active": "active", "done": "done"}
IMPORT_PRIORITY_WORDS = {**PRIORITY_WORDS, "high": "high", "medium": "medium", "low": "low"}
MAX_IMPORT_BYTES = 1_000_000  # 1 МБ — это десятки тысяч задач
MAX_IMPORT_ROWS = 1000
FORMULA_CHARS = ("=", "+", "-", "@")  # с этих символов Excel начинает формулу

IMPORT_HELP = (
    "Колонки в первой строке: номер;задача;приоритет;категория;срок;статус;создана;выполнена.\n"
    "Обязательна только «задача», порядок колонок любой, разделитель «;» или «,».\n"
    "Приоритет: высокий, средний или низкий (пусто — средний). Срок и даты: ДД.ММ.ГГГГ. "
    "Статус: активна или выполнена (пусто — активна).\n"
    "Номера из файла не используются — задачи получат новые номера."
)


def excel_safe(value: str) -> str:
    """Excel считает формулой ячейку, которая начинается с = + - @. Чтобы задача
    «-купить хлеб» не превратилась в формулу, ставим апостроф — Excel его не показывает."""
    return f"'{value}" if value.startswith(FORMULA_CHARS) else value


def from_excel_safe(value: str) -> str:
    """Обратно к excel_safe: убираем апостроф перед = + - @."""
    return value[1:] if len(value) > 1 and value[0] == "'" and value[1] in FORMULA_CHARS else value


def ru_date(value: str | None) -> str:
    """«2026-10-14» или «2026-10-14T15:56:33+03:00» -> «14.10.2026»."""
    return datetime.fromisoformat(value).strftime("%d.%m.%Y") if value else ""


def build_export_csv(tasks: list[dict]) -> bytes:
    """Все задачи в CSV в формате sample_tasks.csv."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for t in sorted(tasks, key=lambda t: t["id"]):
        writer.writerow([
            t["id"], excel_safe(t["text"]), PRIORITIES[t["priority"]]["name"],
            excel_safe(t.get("category") or ""), ru_date(t.get("due")), STATUS_NAMES[t["status"]],
            ru_date(t.get("created_at")), ru_date(t.get("done_at")),
        ])
    return buffer.getvalue().encode("utf-8-sig")  # utf-8-sig добавляет BOM в начало файла


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export — присылает файл со всеми задачами."""
    await send_export(update.message, get_user(context, update.effective_user.id)["tasks"])


async def send_export(message, tasks: list[dict]) -> None:
    """Файл со всеми задачами (для /export и кнопки «📤 Выгрузить» в «Настройках»)."""
    if not tasks:
        await message.reply_text("Задач пока нет — выгружать нечего.")
        return
    active = sum(1 for t in tasks if t["status"] == "active")
    await message.reply_document(
        document=build_export_csv(tasks),
        filename=f"remindly_{today().strftime('%Y-%m-%d')}.csv",
        caption=(f"📤 Все задачи: {len(tasks)} (активных {active}, выполненных {len(tasks) - active}).\n"
                 "Файл открывается в Excel. Загрузить его обратно — «Настройки» → «📥 Загрузить»."),
    )


async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/import — объясняет, какой файл прислать. Сам файл обрабатывает import_document."""
    await update.message.reply_text(
        f"📥 Пришлите CSV-файл с задачами — например, выгруженный через /export.\n\n{IMPORT_HELP}")


def decode_file(raw: bytes) -> str:
    """Текст файла: UTF-8 (так сохраняет /export и «CSV UTF-8» в Excel) или Windows-1251
    (так сохраняет обычный «CSV» в Excel с русскими настройками)."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


def parse_import_date(value: str, what: str) -> tuple[date | None, str | None]:
    """«25.10.2026» -> дата. Пустое значение — (None, None). Ошибка — (None, причина)."""
    if not value:
        return None, None
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})", value)
    if not m:
        return None, f"неверный формат даты {what} «{value}» — нужно ДД.ММ.ГГГГ, например 25.10.2026"
    year = int(m[3]) if len(m[3]) == 4 else 2000 + int(m[3])
    try:
        return date(year, int(m[2]), int(m[1])), None
    except ValueError:
        return None, f"несуществующая дата {what} «{value}»"


def at_noon(d: date) -> str:
    """Дата из файла -> время для базы. Полдень — чтобы дата не «съезжала» из-за часового пояса."""
    return datetime.combine(d, time(12, 0), tzinfo=TZ).isoformat(timespec="seconds")


def parse_import_row(cell, categories: list[str]) -> tuple[dict | None, str | None]:
    """Одна строка файла -> поля задачи для базы или причина, почему строка пропущена.
    cell("срок") возвращает значение колонки ('' — если колонки или значения нет)."""
    text = from_excel_safe(" ".join(cell("задача").split()))
    if not text:
        return None, "нет текста задачи"
    if len(text) > MAX_TASK_LENGTH:
        return None, f"текст задачи длиннее {MAX_TASK_LENGTH} символов"

    raw = cell("приоритет")
    priority = match_phrase(raw, IMPORT_PRIORITY_WORDS) if raw else "medium"
    if priority is None:
        return None, f"неизвестный приоритет «{raw}» — нужно: высокий, средний или низкий"

    raw = " ".join(from_excel_safe(cell("категория")).split())
    category = (find_category(raw, categories) or raw) if raw else None
    error = validate_category(category) if category else None
    if error:
        return None, f"неподходящая категория «{raw}»: {error[0].lower()}{error[1:].rstrip('.')}"

    due, error = parse_import_date(cell("срок"), "срока")
    if error:
        return None, error

    raw = cell("статус")
    status = match_phrase(raw, STATUS_WORDS) if raw else "active"
    if status is None:
        return None, f"неизвестный статус «{raw}» — нужно: активна или выполнена"

    created, error = parse_import_date(cell("создана"), "создания")
    if error:
        return None, error
    finished, error = parse_import_date(cell("выполнена"), "выполнения")
    if error:
        return None, error

    current = now().isoformat(timespec="seconds")
    return {
        "text": text,
        "priority": priority,
        "category": category,
        "due": due.isoformat() if due else None,
        "status": status,
        "created_at": at_noon(created) if created else current,
        # у выполненной задачи без даты выполнения ставим сегодняшнюю
        "done_at": (at_noon(finished) if finished else current) if status == "done" else None,
    }, None


def import_key(task: dict) -> tuple:
    """По этим полям узнаём, что задача уже есть: повторная загрузка файла не создаст дублей."""
    return normalize(task["text"]), task.get("due"), task["status"]


def parse_import_file(text: str, user: dict) -> tuple[list[dict], list[tuple[int, str]], list[int]]:
    """Разбирает CSV. Возвращает задачи для добавления, строки с ошибками
    (номер строки в файле, причина) и номера строк-повторов (такая задача уже есть).
    Если файл не подходит целиком — ValueError с причиной."""
    if not text.strip():
        raise ValueError("файл пустой")
    first_line = text.splitlines()[0]
    # Excel с русскими настройками разделяет колонки «;», с английскими — «,»
    delimiter = max([";", ",", "\t"], key=first_line.count)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as e:
        raise ValueError(f"файл не похож на CSV ({e})") from e

    header = [normalize(name) for name in rows[0]]
    if "задача" not in header:
        raise ValueError("в первой строке нет колонки «задача»")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise ValueError(f"слишком много строк ({len(rows) - 1}), можно не больше {MAX_IMPORT_ROWS}")
    columns = {name: header.index(name) for name in CSV_COLUMNS if name in header}

    categories = user_categories(user["user_id"])
    # Задачи, которые уже есть (в списке или выше в файле): повторы не добавляем
    seen = {import_key(t) for t in user["tasks"]}
    tasks, skipped, duplicates = [], [], []
    for row_number, row in enumerate(rows[1:], start=2):  # строка 1 — заголовок
        if not any(value.strip() for value in row):
            continue  # пустые строки пропускаем молча

        def cell(name: str) -> str:
            index = columns.get(name)
            return row[index].strip() if index is not None and index < len(row) else ""

        task, reason = parse_import_row(cell, categories)
        if not task:
            skipped.append((row_number, reason))
        elif import_key(task) in seen:
            duplicates.append(row_number)
        else:
            seen.add(import_key(task))
            tasks.append(task)
            if task["category"] and not find_category(task["category"], categories):
                categories.append(task["category"])  # новая категория — для следующих строк
    return tasks, skipped, duplicates


def compact_numbers(numbers: list[int]) -> str:
    """[2, 3, 4, 7, 9, 10] -> «2–4, 7, 9–10»."""
    parts, start = [], None
    for i, n in enumerate(numbers):
        start = n if start is None else start
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(str(n) if start == n else f"{start}–{n}")
            start = None
    return ", ".join(parts)


def import_report(file_name: str, added: list[dict], skipped: list[tuple[int, str]],
                  duplicates: list[int], new_categories: list[str]) -> str:
    """Итог загрузки: сколько задач добавлено, какие строки пропущены и почему."""
    if added:
        done = sum(1 for t in added if t["status"] == "done")
        numbers = f"#{added[0]['id']}" + (f"–#{added[-1]['id']}" if len(added) > 1 else "")
        text = (f"📥 Из файла «{file_name}» добавлено {len(added)} "
                f"{plural(len(added), 'задача', 'задачи', 'задач')} ({numbers})")
        text += f", из них выполненных — {done}." if done else "."
    else:
        text = f"📥 Из файла «{file_name}» не добавлено ни одной задачи."
    if new_categories:
        text += f"\n🏷 Новые категории: {', '.join(new_categories)}."
    if duplicates:  # повторы — одной строкой, а не списком (например, если файл загрузили дважды)
        text += (f"\n♻️ Уже есть в списке, пропущено {len(duplicates)} "
                 f"{plural(len(duplicates), 'строка', 'строки', 'строк')} ({compact_numbers(duplicates)}).")
    if skipped:
        header = (f"{text}\n\n⚠️ Пропущено {len(skipped)} "
                  f"{plural(len(skipped), 'строка', 'строки', 'строк')}:")
        text = join_lines(header, [f"• строка {number}: {reason}" for number, reason in skipped])
    return text


async def import_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь прислал файл — загружаем из него задачи."""
    document = update.message.document
    file_name = document.file_name or "файл"
    if file_name.lower().endswith((".xlsx", ".xls")):
        await update.message.reply_text(
            "Это файл Excel. Сохраните его как CSV («Файл → Сохранить как → CSV UTF-8» "
            "или «CSV (разделители — точка с запятой)») и пришлите снова.")
        return
    if not file_name.lower().endswith((".csv", ".txt")):
        await update.message.reply_text(
            "Чтобы загрузить задачи, пришлите CSV-файл — такой, как выдаёт /export. Подробнее: /import")
        return
    if document.file_size and document.file_size > MAX_IMPORT_BYTES:
        await update.message.reply_text("Файл слишком большой: можно до 1 МБ.")
        return

    try:
        telegram_file = await document.get_file()
        raw = bytes(await telegram_file.download_as_bytearray())
    except TelegramError as e:
        logger.warning("Не удалось скачать файл для импорта: %s", e)
        await update.message.reply_text("Не получилось скачать файл. Попробуйте отправить его ещё раз.")
        return

    user = get_user(context, update.effective_user.id)
    try:
        tasks, skipped, duplicates = parse_import_file(decode_file(raw), user)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ Не получилось прочитать «{file_name}»: {e}.\n\n{IMPORT_HELP}")
        return

    categories_before = set(user_categories(user["user_id"]))
    added = db.import_tasks(user["user_id"], tasks) if tasks else []
    new_categories = [c for c in user_categories(user["user_id"]) if c not in categories_before]
    await update.message.reply_text(import_report(file_name, added, skipped, duplicates, new_categories))


# ---------------------------------------------------------------------------
# Управление категориями (/categories)
# ---------------------------------------------------------------------------
# Список категорий с числом задач — обычное сообщение, его кнопки работают в любой момент.
# Нажатие на свою категорию начинает короткий диалог: переименовать или удалить.
# Базовые категории (BASE_CATEGORIES) кнопок не получают и не меняются.

CATEGORY_ACTION_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("✏️ Переименовать", callback_data="cact:rename")],
    [InlineKeyboardButton("🗑 Удалить", callback_data="cact:delete"),
     InlineKeyboardButton("⬅️ Назад", callback_data="cact:back")],
])
CATEGORY_DELETE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Да", callback_data="cdel:yes"),
    InlineKeyboardButton("Нет", callback_data="cdel:no"),
]])
CATEGORY_ACTION_WORDS = {"переименовать": "rename", "изменить": "rename", "удалить": "delete",
                         "назад": "back"}
CATEGORY_ACTION_HINT = "Нажмите кнопку или напишите: переименовать, удалить или назад."


def category_tasks(user: dict, name: str | None) -> list[dict]:
    """Все задачи пользователя в категории (активные и выполненные); None — без категории."""
    key = normalize(name or "")
    return [t for t in user["tasks"] if normalize(t.get("category") or "") == key]


def tasks_count_text(tasks: list[dict]) -> str:
    """«нет задач», «3 задачи», «3 задачи, активных 1»."""
    if not tasks:
        return "нет задач"
    active = sum(1 for t in tasks if t["status"] == "active")
    text = f"{len(tasks)} {plural(len(tasks), 'задача', 'задачи', 'задач')}"
    return text if active == len(tasks) else f"{text}, активных {active}"


def categories_overview(user: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст со всеми категориями и числом задач, кнопки — только у своих категорий."""
    lines = ["🏷 Категории", "", "Базовые (не меняются):"]
    lines += [f"• {name} — {tasks_count_text(category_tasks(user, name))}" for name in BASE_CATEGORIES]
    custom = db.custom_categories_with_ids(user["user_id"])
    lines += ["", "Ваши:"]
    if custom:
        lines += [f"• {name} — {tasks_count_text(category_tasks(user, name))}" for _, name in custom]
    else:
        lines.append("пока нет. Новая категория появится, если на шаге «Категория» "
                     "при добавлении задачи написать своё название.")
    without = category_tasks(user, None)
    if without:
        lines += ["", f"Без категории — {tasks_count_text(without)}"]
    back = [button("⬅️ К настройкам", "st:home")]
    if not custom:
        return "\n".join(lines), InlineKeyboardMarkup([back])
    lines += ["", "Нажмите на свою категорию, чтобы переименовать или удалить её."]
    # В кнопке — rowid строки в таблице categories: он не меняется, даже если другие удалят
    buttons = [InlineKeyboardButton(f"🏷 {name}", callback_data=f"catm:{rowid}") for rowid, name in custom]
    return "\n".join(lines), InlineKeyboardMarkup(button_rows(buttons) + [back])


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = "") -> None:
    """Показывает список категорий: после нажатия кнопки — в том же сообщении, иначе — новым.
    Это не вопрос диалога, поэтому кнопки списка не запоминаем в kb_message."""
    text, keyboard = categories_overview(get_user(context, update.effective_user.id))
    await show_view(update, context, prefix + text, keyboard)


async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/categories (или «категории») — категории и сколько в них задач."""
    await show_categories(update, context)


async def category_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажата своя категория в списке: спрашиваем, что с ней сделать.
    Срабатывает и посреди другого диалога — он молча завершается."""
    query = update.callback_query
    await query.answer()
    await end_dialog_quietly(context)
    name = db.custom_category_name(update.effective_user.id, int(query.data.split(":", 1)[1]))
    if name is None:
        await show_categories(update, context, "Этой категории уже нет.\n\n")
        return ConversationHandler.END
    return await ask_category_action(update, context, name)


async def ask_category_action(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> int:
    """«Что сделать с категорией?» — переименовать, удалить или назад к списку."""
    context.user_data["cat_edit"] = name
    user = get_user(context, update.effective_user.id)
    await respond(update, context,
                  f"🏷 {name} — {tasks_count_text(category_tasks(user, name))}\nЧто сделать с категорией?",
                  CATEGORY_ACTION_KEYBOARD)
    return CAT_ACTION


async def category_action_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await category_action(update, context, query.data.split(":", 1)[1])


async def category_action_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Что сделать с категорией?»."""
    action = match_phrase(update.message.text, CATEGORY_ACTION_WORDS)
    if action:
        return await category_action(update, context, action)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(CATEGORY_ACTION_HINT)
    return CAT_ACTION


def managed_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Своя категория, с которой сейчас работаем (None, если её уже удалили)."""
    name = context.user_data.get("cat_edit")
    custom = db.custom_categories(update.effective_user.id)
    return name if name in custom else None


async def category_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> int:
    """Переименовать, удалить (с подтверждением) или вернуться к списку."""
    name = managed_category(update, context)
    if action == "back" or name is None:
        context.user_data.pop("cat_edit", None)
        await show_categories(update, context, "" if name else "Этой категории уже нет.\n\n")
        return ConversationHandler.END
    if action == "rename":
        await respond(update, context, f"✏️ Новое название для категории «{name}»?",
                      with_nav(None, back="cat_action"))
        return CAT_RENAME
    tasks = category_tasks(get_user(context, update.effective_user.id), name)
    if tasks:
        moved = plural(len(tasks), "она перейдёт", "они перейдут", "они перейдут")
        details = f"В ней {tasks_count_text(tasks)} — {moved} в «Без категории»."
    else:
        details = "Задач в ней нет."
    await respond(update, context, f"🗑 Удалить категорию «{name}»? {details}", CATEGORY_DELETE_KEYBOARD)
    return CAT_DELETE_CONFIRM


async def category_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Новое название категории. Любой текст подходит как ответ, поэтому сначала проверяем
    текстовые команды («отмена»), иначе их было бы не набрать."""
    state = await try_text_command(update, context)
    if state is not None:
        return state
    old = managed_category(update, context)
    if old is None:
        context.user_data.pop("cat_edit", None)
        await show_categories(update, context, "Этой категории уже нет.\n\n")
        return ConversationHandler.END
    new, error = clean_category_name(update.message.text)
    if not error and new == old:
        error = "Это то же название."
    taken = [c for c in user_categories(update.effective_user.id)
             if normalize(c) == normalize(new) and c != old]
    if not error and taken:
        error = f"Категория «{taken[0]}» уже есть."
    if error:
        await update.message.reply_text(f"{error} Напишите другое название или /cancel.")
        return CAT_RENAME

    count = db.rename_category(update.effective_user.id, old, new)
    context.user_data.pop("cat_edit", None)
    await show_categories(update, context,
                          f"✏️ Категория «{old}» переименована в «{new}». Изменено задач: {count}.\n\n")
    return ConversationHandler.END


async def category_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await category_delete_answer(update, context, query.data == "cdel:yes")


async def category_delete_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом на «Удалить категорию?»: «да», «удалить», «нет», «оставить»…"""
    yes = match_phrase(update.message.text, DELETE_WORDS)
    if yes is not None:
        return await category_delete_answer(update, context, yes)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(YES_NO_HINT)
    return CAT_DELETE_CONFIRM


async def category_delete_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, yes: bool) -> int:
    """Удаляем категорию: её задачи переходят в «Без категории»."""
    name = managed_category(update, context)
    context.user_data.pop("cat_edit", None)
    if name is None:
        await show_categories(update, context, "Этой категории уже нет.\n\n")
    elif not yes:
        await show_categories(update, context, f"Ок, категория «{name}» остаётся.\n\n")
    else:
        count = db.delete_category(update.effective_user.id, name)
        moved = (f" {count} {plural(count, 'задача', 'задачи', 'задач')} теперь без категории."
                 if count else "")
        await show_categories(update, context, f"🗑 Категория «{name}» удалена.{moved}\n\n")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Статистика использования (обезличенная)
# ---------------------------------------------------------------------------
# Что записываем: когда пользователь последний раз заходил (users.last_seen) и какие команды
# вызывал (таблица usage: команда, со слешем или текстом, время). Тексты задач и сообщений
# не записываем. Администратор видит только общие числа — без имён и Telegram ID.

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def record_usage(user_id: int, action: str, source: str) -> None:
    """Записывает использование команды. Ошибка записи статистики не должна мешать боту."""
    try:
        db.log_usage(user_id, action, source)
    except sqlite3.Error as e:
        logger.warning("Не удалось записать статистику: %s", e)


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает на каждое сообщение и нажатие кнопки раньше всех остальных обработчиков
    (группа -1): отмечаем, что пользователь активен, и считаем команды со слешем."""
    user = update.effective_user
    if not user:
        return
    try:
        db.touch_user(user.id)
    except sqlite3.Error as e:
        logger.warning("Не удалось отметить активность: %s", e)
    message = update.message
    if message and message.text and message.text.startswith("/"):
        name = message.text.split()[0][1:].split("@")[0].lower()  # «/list@remindly_bot» -> «list»
        known = name in COMMAND_HANDLERS or name in DIALOG_COMMAND_HANDLERS
        record_usage(user.id, name if known else "unknown", "slash")


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myid — Telegram ID пользователя (его, например, указывают в .env как ADMIN_ID)."""
    await update.message.reply_text(f"🆔 Ваш Telegram ID: {update.effective_user.id}")


# ---------------------------------------------------------------------------
# Отзывы (/feedback)
# ---------------------------------------------------------------------------
# Опрос: оценка 1–5 → что понравилось → что не понравилось → что улучшить → стали бы
# пользоваться постоянно. Текстовые вопросы можно пропустить. Отзыв сохраняется в базе
# и сразу отправляется администратору (ADMIN_ID в .env).

FEEDBACK_AFTER = timedelta(days=2)  # через сколько после начала бот сам предложит отзыв
FEEDBACK_HOURS = (10, 21)           # предлагаем только днём, с 10:00 до 21:00
FEEDBACK_TEXT_LIMIT = 1000

RATING_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton(f"{n} ⭐", callback_data=f"fb:rate:{n}") for n in range(1, 6)
]])
SKIP_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="fb:skip")]])
WOULD_USE = {"yes": "Да", "maybe": "Скорее да", "no": "Нет"}
WOULD_USE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton(label, callback_data=f"fb:use:{key}") for key, label in WOULD_USE.items()
]])
WOULD_USE_WORDS = {"да": "yes", "скорее да": "maybe", "нет": "no", "скорее нет": "no"}
FEEDBACK_OFFER_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📝 Оставить отзыв", callback_data="fb:start")],
    [InlineKeyboardButton("Не сейчас", callback_data="fb:later")],
])
SKIP_WORDS = {"пропустить", "пропуск", "skip", "-", "—"}

# Текстовые вопросы по порядку: (ключ ответа в базе, вопрос)
FEEDBACK_QUESTIONS = [
    ("liked", "👍 Что понравилось?"),
    ("disliked", "👎 Что не понравилось?"),
    ("improve", "💡 Что улучшить?"),
]


def parse_rating(text: str) -> int | None:
    """«4», «4 ⭐», «4/5», «4 из 5» -> 4."""
    m = re.fullmatch(r"([1-5])\s*(?:⭐+|/\s*5|из\s*5)?", text.strip())
    return int(m[1]) if m else None


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/feedback (или «отзыв») — начать опрос."""
    await end_dialog_quietly(context)
    return await start_feedback(update, context)


async def feedback_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Оставить отзыв» в предложении бота. Работает и посреди другого диалога."""
    await update.callback_query.answer()
    await end_dialog_quietly(context)
    return await start_feedback(update, context)


async def feedback_later_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Не сейчас»: больше сами не предлагаем, но напоминаем, как оставить отзыв."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Хорошо! Если захотите — /feedback или просто напишите «отзыв».")


async def start_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_user(context, update.effective_user.id)
    context.user_data["feedback"] = {}
    return await ask_feedback_rating(update, context)


async def ask_feedback_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["feedback"]["step"] = -1  # до текстовых вопросов
    await respond(update, context,
                  "📝 Отзыв о Remindly — 5 коротких вопросов.\n\n⭐ Оцените бота от 1 до 5:",
                  with_nav(RATING_KEYBOARD))
    return FB_RATING


async def feedback_rating_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["feedback"]["rating"] = int(query.data.rsplit(":", 1)[1])
    return await ask_feedback_question(update, context, 0)


async def feedback_rating_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Оценка текстом: «5», «4/5». Ответы тут фиксированные, поэтому текстовые команды
    («отмена», «список») тоже работают."""
    rating = parse_rating(update.message.text)
    if rating:
        context.user_data["feedback"]["rating"] = rating
        return await ask_feedback_question(update, context, 0)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text("Поставьте оценку от 1 до 5 — кнопкой или цифрой.")
    return FB_RATING


async def ask_feedback_question(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> int:
    """Следующий текстовый вопрос, а после них — «Стали бы пользоваться постоянно?»."""
    context.user_data["feedback"]["step"] = index  # «Назад» вернёт к предыдущему вопросу
    if index >= len(FEEDBACK_QUESTIONS):
        await respond(update, context, "🔁 Стали бы пользоваться ботом постоянно?",
                      with_nav(WOULD_USE_KEYBOARD, back="fb"))
        return FB_WOULD_USE
    question = FEEDBACK_QUESTIONS[index][1]
    await respond(update, context, f"{question}\nНапишите ответ или нажмите «Пропустить».",
                  with_nav(SKIP_KEYBOARD, back="fb"))
    return FB_TEXT


async def feedback_skip_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await save_feedback_answer(update, context, None)


async def feedback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ на текстовый вопрос. Здесь любой текст — это ответ: «Напоминания» в ответ на
    «Что понравилось?» не должно открыть /reminders. Поэтому текстовые команды тут
    не работают — кроме «отмена»; прервать опрос можно и через /cancel."""
    text = update.message.text.strip()
    if normalize(text) == "отмена":
        return await cancel(update, context)
    if normalize(text) in SKIP_WORDS:
        return await save_feedback_answer(update, context, None)
    if len(text) > FEEDBACK_TEXT_LIMIT:
        await update.message.reply_text(
            f"Слишком длинно — до {FEEDBACK_TEXT_LIMIT} символов. Сократите, пожалуйста.")
        return FB_TEXT
    return await save_feedback_answer(update, context, text)


async def save_feedback_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               answer: str | None) -> int:
    """Запоминаем ответ (None — пропущен) и задаём следующий вопрос."""
    feedback = context.user_data["feedback"]
    key = FEEDBACK_QUESTIONS[feedback["step"]][0]
    feedback[key] = answer
    return await ask_feedback_question(update, context, feedback["step"] + 1)


async def feedback_use_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await finish_feedback(update, context, query.data.rsplit(":", 1)[1])


async def feedback_use_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ответ текстом: «да», «скорее да», «нет»."""
    would_use = match_phrase(update.message.text, WOULD_USE_WORDS)
    if would_use:
        return await finish_feedback(update, context, would_use)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text("Напишите «да», «скорее да» или «нет», либо нажмите кнопку.")
    return FB_WOULD_USE


def feedback_message(feedback: dict, participant: int) -> str:
    """Отзыв для администратора: без имени и Telegram ID — только номер участника."""
    def answer(key: str) -> str:
        return feedback.get(key) or "— (пропущено)"
    stars = "⭐" * feedback["rating"] + "☆" * (5 - feedback["rating"])
    return (f"📝 Отзыв #{feedback['id']} · участник №{participant}\n"
            f"Оценка: {stars} {feedback['rating']}/5\n"
            f"👍 Понравилось: {answer('liked')}\n"
            f"👎 Не понравилось: {answer('disliked')}\n"
            f"💡 Улучшить: {answer('improve')}\n"
            f"🔁 Пользовался бы постоянно: {WOULD_USE[feedback['would_use']]}")


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Сообщение администраторам. Если отправить не вышло (например, администратор ещё
    не открывал бота), пишем в лог — отзыв всё равно сохранён в базе."""
    if not ADMIN_IDS:
        logger.warning("ADMIN_ID не задан в .env — отзыв сохранён, но никому не отправлен")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except TelegramError as e:
            logger.warning("Не удалось отправить отзыв администратору %s: %s", admin_id, e)


async def finish_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, would_use: str) -> int:
    """Сохраняем отзыв, благодарим и сразу пересылаем администратору."""
    feedback = context.user_data.pop("feedback", {})
    user_id = update.effective_user.id
    feedback["would_use"] = would_use
    feedback["id"] = db.add_feedback(user_id, rating=feedback["rating"], liked=feedback.get("liked"),
                                     disliked=feedback.get("disliked"),
                                     improve=feedback.get("improve"), would_use=would_use)
    await respond(update, context, "🙏 Спасибо за отзыв! Он поможет сделать Remindly лучше.")
    participant = db.participant_numbers().get(user_id, 0)
    await notify_admins(context, feedback_message(feedback, participant))
    return ConversationHandler.END


async def feedback_offer_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в час: тем, кто пользуется ботом 2 дня и ещё не оставлял отзыв, один раз
    предлагаем его оставить. Только днём — чтобы не будить сообщением."""
    if not FEEDBACK_HOURS[0] <= now().hour < FEEDBACK_HOURS[1]:
        return
    started_before = (now() - FEEDBACK_AFTER).isoformat(timespec="seconds")
    for user_id, chat_id in db.users_due_for_feedback(started_before):
        db.mark_feedback_offered(user_id)  # отмечаем сразу: предложение — только один раз
        try:
            await context.bot.send_message(
                chat_id,
                "👋 Вы пользуетесь Remindly уже пару дней. Расскажете, как вам?\n"
                "Это 5 коротких вопросов, текстовые можно пропустить.",
                reply_markup=FEEDBACK_OFFER_KEYBOARD)
        except TelegramError as e:  # например, пользователь заблокировал бота
            logger.warning("Не удалось предложить отзыв %s: %s", user_id, e)


# ---------------------------------------------------------------------------
# Статистика для администратора (/admin)
# ---------------------------------------------------------------------------

ADMIN_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("📥 Отзывы для Excel", callback_data="adm:feedback"),
]])
ADMIN_HELP = """

🔐 Для администратора
/admin — статистика использования и средняя оценка
/admin отзывы — все отзывы файлом для Excel"""
# Как показывать действия в статистике (остальные — как команды: /list, /add…)
USAGE_LABELS = {"quick_add": "задача обычным текстом", "unknown": "неизвестные команды"}
USAGE_TOP = 10


def admin_report() -> str:
    """Сводка для администратора: только общие числа, без имён и текстов задач."""
    week_ago = (now() - timedelta(days=7)).isoformat(timespec="seconds")
    s = db.admin_stats(week_ago)
    lines = [
        "📊 Статистика Remindly",
        "Обезличенно: без имён, Telegram ID и текстов задач.",
        "",
        f"👥 Пользователей: {s['users']}",
        f"🟢 Активных за 7 дней: {s['active_week']}",
        f"📝 Задач создано: {s['created']} (за 7 дней: {s['created_week']})",
        f"✅ Задач выполнено: {s['done']} (за 7 дней: {s['done_week']})",
        "",
    ]
    if s["ratings"]:
        average = sum(s["ratings"]) / len(s["ratings"])
        spread = ", ".join(f"{n}⭐ — {s['ratings'].count(n)}" for n in range(5, 0, -1) if n in s["ratings"])
        lines.append(f"⭐ Средняя оценка: {average:.1f} из 5 (отзывов: {len(s['ratings'])}; {spread})")
        use = ", ".join(f"{label.lower()} — {s['would_use'].get(key, 0)}" for key, label in WOULD_USE.items())
        lines.append(f"🔁 Пользовались бы постоянно: {use}")
    else:
        lines.append("⭐ Отзывов пока нет.")

    lines += ["", "⌨️ Чаще всего используют:"]
    if not s["usage"]:
        lines.append("пока ничего")
    for i, (action, total, by_text) in enumerate(s["usage"][:USAGE_TOP], start=1):
        label = USAGE_LABELS.get(action, f"/{action}")
        lines.append(f"{i}. {label} — {total}" + (f" (текстом — {by_text})" if by_text and action != "quick_add" else ""))
    lines += ["", "Все отзывы файлом: кнопка ниже или /admin отзывы"]
    return "\n".join(lines)


def build_feedback_csv() -> bytes:
    """Все отзывы в CSV для Excel (как /export: UTF-8 с BOM, разделитель «;»)."""
    participants = db.participant_numbers()
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["номер", "дата", "участник", "оценка", "понравилось", "не понравилось",
                     "улучшить", "пользовался бы постоянно"])
    for f in db.all_feedback():
        writer.writerow([
            f["id"], datetime.fromisoformat(f["created_at"]).strftime("%d.%m.%Y %H:%M"),
            f"Участник №{participants.get(f['user_id'], 0)}", f["rating"],
            *(excel_safe(f[key] or "") for key in ("liked", "disliked", "improve")),
            WOULD_USE[f["would_use"]],
        ])
    return buffer.getvalue().encode("utf-8-sig")


async def send_feedback_file(message) -> None:
    count = len(db.all_feedback())
    if not count:
        await message.reply_text("Отзывов пока нет — выгружать нечего.")
        return
    await message.reply_document(
        document=build_feedback_csv(), filename=f"remindly_feedback_{today().strftime('%Y-%m-%d')}.csv",
        caption=f"📥 Отзывы: {count}. Файл открывается в Excel.")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/admin — статистика для администратора, /admin отзывы — файл с отзывами.
    Остальным бот отвечает как на неизвестную команду, чтобы не раскрывать её."""
    if not is_admin(update.effective_user.id):
        await unknown_cmd(update, context)
        return
    if context.args and normalize(" ".join(context.args)) in ("отзывы", "feedback"):
        await send_feedback_file(update.message)
        return
    await update.message.reply_text(admin_report(), reply_markup=ADMIN_KEYBOARD)


async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Отзывы для Excel» под /admin."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("Недоступно.", show_alert=True)
        return
    await query.answer()
    await send_feedback_file(query.message)


# ---------------------------------------------------------------------------
# Статистика и служебные команды
# ---------------------------------------------------------------------------

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — сводные цифры."""
    await update.message.reply_text(stats_text(get_user(context, update.effective_user.id)))


def stats_text(user: dict) -> str:
    """Статистика по задачам (для /stats и кнопки в «Мои задачи»)."""
    tasks = user["tasks"]
    active = [t for t in tasks if t["status"] == "active"]
    done = [t for t in tasks if t["status"] == "done"]
    overdue = [t for t in active if is_overdue(t)]

    # «В срок» считаем среди выполненных задач, у которых был срок
    done_with_due = [t for t in done if t.get("due")]
    on_time = [
        t for t in done_with_due
        if datetime.fromisoformat(t["done_at"]).date() <= task_due(t)
    ]
    on_time_text = (f"{round(100 * len(on_time) / len(done_with_due))}%"
                    if done_with_due else "— (нет выполненных задач со сроком)")

    return ("📊 Статистика\n\n"
            f"Всего задач: {len(tasks)}\n"
            f"Активных: {len(active)}\n"
            f"Выполненных: {len(done)}\n"
            f"Просроченных: {len(overdue)}\n"
            f"Выполнено в срок: {on_time_text}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие. Новому пользователю включается сводка (см. get_user)."""
    user = get_user(context, update.effective_user.id)
    user["chat_id"] = update.effective_chat.id
    db.save_user(user)
    await update.message.reply_text(
        f"👋 Привет, {update.effective_user.first_name}! Я Remindly — помогу не забывать о делах.\n\n"
        "Кнопки внизу:\n"
        "• «Добавить» — новая задача (или просто напишите её текстом)\n"
        "• «Мои задачи» — список; нажмите на задачу, чтобы изменить её, поставить напоминание "
        "или отметить выполненной\n"
        "• «Настройки» — утренняя сводка, категории, файл задач\n"
        "• «Помощь» — подробнее\n\n"
        f"{'По рабочим дням' if user['settings']['digest_mode'] == 'workdays' else 'Каждый день'} "
        f"в {user['settings']['digest_time']} пришлю сводку на день.\n\n"
        "ℹ️ Бот учебный: я собираю обезличенную статистику использования (какими командами "
        "пользуются, сколько задач создают) — без текстов ваших задач.",
        reply_markup=MAIN_KEYBOARD,
    )


HELP_TEXT = """📖 Как пользоваться Remindly

Кнопки внизу экрана:

➕ «Добавить» — новая задача: текст → приоритет → срок → категория → напоминание.
На каждом шаге есть «⬅️ Назад» (введённое сохраняется) и «✖️ Отмена».
Задачу можно добавить и просто написав её текст в чат.

📋 «Мои задачи» — список задач. Фильтры: на сегодня, на неделю, по категории,
по приоритету, с напоминаниями, выполненные. Там же поиск, статистика
и рабочие дни до конца месяца.
Нажмите на задачу — откроется карточка: изменить текст, приоритет, срок,
категорию, напоминание, отметить «Выполнено» или удалить.

⚙️ «Настройки» — утренняя сводка (время, каждый день или только в рабочие дни),
категории (свои можно переименовать и удалить), файл задач для Excel:
выгрузить или загрузить.

⏰ Напоминания: бот предлагает варианты, которые подходят к сроку задачи,
а «Указать время» есть всегда — можно поставить любое время.
📅 Срок — датой, «сегодня», «завтра» или «через N рабочих дней»
(по производственному календарю, с учётом праздников).
💬 На вопросы можно отвечать и текстом, небольшие опечатки не страшны.

Команды: /add — добавить задачу, /list — мои задачи, /settings — настройки,
/feedback — отзыв о боте, /help — эта справка."""


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help. Администратор видит ещё и свои команды."""
    extra = ADMIN_HELP if is_admin(update.effective_user.id) else ""
    await update.message.reply_text(HELP_TEXT + extra, reply_markup=MAIN_KEYBOARD)


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Не знаю такую команду 🤔 Список команд — /help")


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажата кнопка от завершённого диалога (например, после перезапуска бота)."""
    await update.callback_query.answer(
        "Эта кнопка устарела. Начните заново: /add или просто напишите задачу.", show_alert=True
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок: пишем в лог и вежливо сообщаем пользователю."""
    if isinstance(context.error, BadRequest) and "not modified" in str(context.error):
        return  # повторное нажатие той же кнопки — не ошибка
    logger.error("Ошибка при обработке апдейта", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id, "😕 Что-то пошло не так. Попробуйте ещё раз."
            )
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

# Меню команд — только главное. Остальные команды (/today, /done, /digest…) и текстовые
# формы («сделано 3») работают как раньше, но в меню и /help их нет: всё есть в кнопках.
BOT_COMMANDS = [
    BotCommand("add", "Добавить задачу"),
    BotCommand("list", "Мои задачи"),
    BotCommand("settings", "Настройки"),
    BotCommand("feedback", "Оставить отзыв о боте"),
    BotCommand("help", "Помощь"),
]


# Обычные команды: имя -> обработчик. Используются и для команд со слешем, и для текстовых.
COMMAND_HANDLERS = {
    "start": start_cmd,
    "help": help_cmd,
    "list": list_cmd,
    "today": today_cmd,
    "reminders": reminders_cmd,
    "digest": digest_cmd,
    "stats": stats_cmd,
    "history": history_cmd,
    "export": export_cmd,
    "import": import_cmd,
    "workdays": workdays_cmd,
    "categories": categories_cmd,
    "settings": settings_cmd,
    "myid": myid_cmd,
    "admin": admin_cmd,  # только для ADMIN_ID, остальным — «Не знаю такую команду»
}

# Команды, которые сами начинают или завершают диалог (обрабатываются в ConversationHandler)
DIALOG_COMMAND_HANDLERS = {
    "add": add_start,
    "edit": edit_cmd,
    "done": done_cmd,
    "delete": delete_cmd,
    "remind": remind_cmd,
    "find": find_cmd,
    "clear_history": clear_history_cmd,  # «очистить историю»
    "feedback": feedback_cmd,
    "cancel": cancel,
}


async def post_init(application: Application) -> None:
    """Выполняется один раз после запуска: меню команд, восстановление задач планировщика
    и ежечасная проверка, кому пора предложить отзыв."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    for admin_id in ADMIN_IDS:  # администратору — меню с /admin
        try:
            await application.bot.set_my_commands(
                BOT_COMMANDS + [BotCommand("admin", "Статистика и отзывы (админ)")],
                scope=BotCommandScopeChat(admin_id))
        except TelegramError as e:  # например, администратор ещё не открывал бота
            logger.warning("Не удалось настроить меню администратора %s: %s", admin_id, e)
    restore_jobs(application)
    application.job_queue.run_repeating(feedback_offer_job, interval=timedelta(hours=1),
                                        first=timedelta(minutes=1), name="feedback_offer")


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    text_only = filters.TEXT & ~filters.COMMAND

    # Обработчики разбиты на две группы. В каждой группе срабатывает максимум один обработчик,
    # но группы проверяются по очереди. Поэтому команда посреди диалога сначала молча
    # завершает диалог (группа 0), а потом выполняется как обычно (группа 1).

    # --- Группа -1: обезличенная статистика (видит каждое сообщение и нажатие первой) ---
    application.add_handler(TypeHandler(Update, track_activity), group=-1)

    # Кнопки под полем ввода («Добавить», «Мои задачи», …) срабатывают в любой момент,
    # поэтому этот обработчик стоит первым и в начале диалога, и на каждом его шаге
    main_menu = MessageHandler(filters.Text(list(MAIN_MENU)), main_menu_button)
    # Кнопки разделов и навигации: работают и посреди диалога (он завершается)
    section_buttons = [
        CallbackQueryHandler(history_clear_button, pattern=r"^hclear:"),  # очистка истории
        CallbackQueryHandler(category_menu_button, pattern=r"^catm:"),    # своя категория
        CallbackQueryHandler(feedback_start_button, pattern=r"^fb:start$"),  # «Оставить отзыв»
        CallbackQueryHandler(list_callback, pattern=r"^ml:"),      # раздел «Мои задачи»
        CallbackQueryHandler(card_callback, pattern=r"^tc:"),      # карточка задачи
        CallbackQueryHandler(settings_callback, pattern=r"^st:"),  # раздел «Настройки»
        CallbackQueryHandler(back_button, pattern=r"^back:"),      # «⬅️ Назад»
        CallbackQueryHandler(keep_button, pattern=r"^keep:"),      # «➡️ Оставить: …»
        CallbackQueryHandler(cancel_button, pattern=r"^nav:cancel$"),  # «✖️ Отмена»
    ]

    # --- Группа 0: диалоги и кнопки ---
    states = {
            CONFIRM: [
                CallbackQueryHandler(quick_button, pattern=r"^quick:"),
                MessageHandler(text_only, quick_text),
            ],
            TEXT: [MessageHandler(text_only, add_text)],
            PRIORITY: [
                CallbackQueryHandler(priority_button, pattern=r"^prio:"),
                MessageHandler(text_only, priority_text),
            ],
            DUE: [
                CallbackQueryHandler(due_button, pattern=r"^due:"),
                MessageHandler(text_only, due_text),
            ],
            DATE_INPUT: [MessageHandler(text_only, date_entered)],
            WORKDAYS_INPUT: [MessageHandler(text_only, workdays_entered)],
            CATEGORY: [
                CallbackQueryHandler(category_button, pattern=r"^cat:"),
                MessageHandler(text_only, category_text),
            ],
            DELETE_CONFIRM: [
                CallbackQueryHandler(delete_button, pattern=r"^del:"),
                MessageHandler(text_only, delete_text),
            ],
            PICK_TASK: [
                CallbackQueryHandler(pick_button, pattern=r"^pick:"),
                MessageHandler(text_only, pick_text),
            ],
            REMIND_OPTION: [
                CallbackQueryHandler(remind_option_button, pattern=r"^ropt:"),
                MessageHandler(text_only, remind_option_text),
            ],
            REMIND_TIME: [MessageHandler(text_only, remind_time_text)],
            FIND_QUERY: [MessageHandler(text_only, find_query_text)],
            DUPLICATE: [
                CallbackQueryHandler(duplicate_button, pattern=r"^dup:"),
                MessageHandler(text_only, duplicate_text),
            ],
            EDIT_FIELD: [
                CallbackQueryHandler(edit_field_button, pattern=r"^efield:"),
                MessageHandler(text_only, edit_field_text),
            ],
            HISTORY_SCOPE: [MessageHandler(text_only, history_scope_text)],  # кнопки — в fallbacks
            CAT_ACTION: [
                CallbackQueryHandler(category_action_button, pattern=r"^cact:"),
                MessageHandler(text_only, category_action_text),
            ],
            CAT_RENAME: [MessageHandler(text_only, category_rename_text)],
            CAT_DELETE_CONFIRM: [
                CallbackQueryHandler(category_delete_button, pattern=r"^cdel:"),
                MessageHandler(text_only, category_delete_text),
            ],
            FB_RATING: [
                CallbackQueryHandler(feedback_rating_button, pattern=r"^fb:rate:"),
                MessageHandler(text_only, feedback_rating_text),
            ],
            FB_TEXT: [
                CallbackQueryHandler(feedback_skip_button, pattern=r"^fb:skip$"),
                MessageHandler(text_only, feedback_text),
            ],
            FB_WOULD_USE: [
                CallbackQueryHandler(feedback_use_button, pattern=r"^fb:use:"),
                MessageHandler(text_only, feedback_use_text),
            ],
            HISTORY_CONFIRM: [
                CallbackQueryHandler(history_confirm_button, pattern=r"^hconf:"),
                MessageHandler(text_only, history_confirm_text),
            ],
            DIGEST_TIME: [MessageHandler(text_only, digest_time_text)],
    }
    for handlers in states.values():
        handlers.insert(0, main_menu)  # кнопки под полем ввода важнее ответа на вопрос
    states[ConversationHandler.TIMEOUT] = [TypeHandler(Update, dialog_timeout)]

    dialog = ConversationHandler(
        entry_points=[
            main_menu,
            *(CommandHandler(name, callback) for name, callback in DIALOG_COMMAND_HANDLERS.items()),
            *section_buttons,
            MessageHandler(text_only, quick_offer),  # быстрое добавление
        ],
        states=states,
        fallbacks=[
            main_menu,
            # /add, /edit, /done, /delete, /remind, /find, /cancel посреди диалога начинают новый
            *(CommandHandler(name, callback) for name, callback in DIALOG_COMMAND_HANDLERS.items()),
            *section_buttons,
            MessageHandler(filters.COMMAND, end_on_command),  # остальные команды — см. группу 1
        ],
        conversation_timeout=DIALOG_TIMEOUT,
    )
    application.add_handler(dialog)

    # Кнопки, нажатые вне диалога
    application.add_handler(CallbackQueryHandler(delete_button, pattern=r"^del:"))
    application.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^(rdone|snooze):"))
    application.add_handler(CallbackQueryHandler(list_filter_button, pattern=r"^flt:"))
    application.add_handler(CallbackQueryHandler(digest_mode_button, pattern=r"^dmode:"))
    application.add_handler(CallbackQueryHandler(feedback_later_button, pattern=r"^fb:later$"))
    application.add_handler(CallbackQueryHandler(admin_button, pattern=r"^adm:"))
    application.add_handler(CallbackQueryHandler(stale_button))

    # Файл от пользователя — загрузка задач (/import)
    application.add_handler(MessageHandler(filters.Document.ALL, import_document))

    # --- Группа 1: обычные команды (команды из DIALOG_COMMAND_HANDLERS обрабатывает диалог) ---
    for name, callback in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, callback), group=1)

    # Неизвестная команда — последней. Команды диалога исключаем, иначе ответ придёт дважды.
    dialog_commands = filters.Regex(rf"^/({'|'.join(DIALOG_COMMAND_HANDLERS)})(@\w+)?(\s|$)")
    application.add_handler(MessageHandler(filters.COMMAND & ~dialog_commands, unknown_cmd), group=1)

    application.add_error_handler(error_handler)
    return application


def check_railway_volume() -> None:
    """На Railway база должна лежать на подключённом диске (volume): остальные файлы
    контейнера стираются при каждом обновлении бота. Если что-то не так — предупреждаем в логе."""
    if not ON_RAILWAY:
        return
    if not VOLUME_PATH:  # Railway задаёт RAILWAY_VOLUME_MOUNT_PATH, только если диск подключён
        logger.warning("К сервису не подключён volume: база %s сотрётся при следующем обновлении бота. "
                       "Подключите volume с путём /data (в панели: ⌘K → Volume или правый клик по холсту проекта)",
                       DB_FILE)
    elif not DB_FILE.resolve().is_relative_to(Path(VOLUME_PATH).resolve()):
        logger.warning("База %s лежит не на volume (%s) и сотрётся при обновлении бота. "
                       "Удалите переменную DB_FILE или задайте DB_FILE=%s/tasks.db",
                       DB_FILE, VOLUME_PATH, VOLUME_PATH.rstrip("/"))


def webhook_port() -> int:
    """Порт веб-сервера для webhook из переменной PORT (на Railway её задаёт сам Railway)."""
    if not PORT_RAW.isdigit():
        raise SystemExit(f"PORT должен быть числом, а сейчас: {PORT_RAW!r}")
    return int(PORT_RAW)


def run_bot(application: Application) -> None:
    """Webhook, если известен публичный адрес бота (на Railway), иначе — long polling."""
    if WEBHOOK_URL:
        if not WEBHOOK_URL.startswith("https://"):
            raise SystemExit(f"WEBHOOK_URL должен начинаться с https:// — Telegram отправляет "
                             f"webhook только по HTTPS. Сейчас: {WEBHOOK_URL}")
        port = webhook_port()
        logger.info("Режим webhook: Telegram присылает сообщения на %s/%s, веб-сервер слушает порт %s",
                    WEBHOOK_URL, WEBHOOK_PATH, port)
        # Встроенный веб-сервер python-telegram-bot (нужен пакет python-telegram-bot[webhooks]).
        # HTTPS обеспечивает Railway, поэтому сертификат боту не нужен. При запуске бот сам
        # сообщает Telegram адрес webhook (setWebhook) — вручную регистрировать его не надо.
        application.run_webhook(
            listen="0.0.0.0",              # принимать запросы снаружи контейнера
            port=port,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET,   # запросы без этого секрета бот отклоняет (ответ 403)
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=3,           # если Telegram не ответил сразу — ещё 3 попытки
        )
        return
    if ON_RAILWAY:
        logger.warning("У сервиса на Railway нет домена (RAILWAY_PUBLIC_DOMAIN), поэтому бот работает "
                       "без webhook, через long polling. Чтобы включить webhook: Settings → Networking → "
                       "Generate Domain, затем Redeploy")
    logger.info("Режим long polling: бот сам забирает сообщения у Telegram")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def post_shutdown(application: Application) -> None:
    """Бот останавливается (Ctrl+C или Railway перезапускает его при обновлении) — закрываем базу."""
    db.conn.close()
    logger.info("Remindly остановлен")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Локально — скопируйте .env.example в .env и впишите токен, "
                         "на Railway — задайте переменную BOT_TOKEN в настройках сервиса.")
    if ADMIN_ID_RAW and not ADMIN_IDS:
        logger.warning("ADMIN_ID в .env должен быть числом, например ADMIN_ID=123456789")
    if not ADMIN_IDS:
        logger.warning("ADMIN_ID не задан: отзывы сохраняются в базе, но никому не приходят, "
                       "а /admin недоступна. Узнать свой ID: /myid")
    check_railway_volume()
    db.migrate_from_json(JSON_FILE)  # только при первом запуске, если есть старый tasks.json
    logger.info("Remindly запускается. База: %s, календарь: %s", DB_FILE, CALENDAR_URL)
    try:
        run_bot(build_application())
    except InvalidToken:
        # Частая ошибка при размещении: токен скопирован не полностью или не задан на сервере.
        # Код выхода 1 — Railway перезапустит бота (политика «при сбое»), а в логе будет понятно почему.
        logger.error("Telegram не принял BOT_TOKEN — проверьте токен в .env или в переменных Railway")
        raise SystemExit(1)
    except TelegramError as e:
        # Например, Telegram не смог зарегистрировать webhook: домен ещё не готов или указан неверно
        logger.error("Не удалось запустить бота: %s. Проверьте домен сервиса (Settings → Networking) "
                     "и переменную WEBHOOK_URL, если она задана", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
