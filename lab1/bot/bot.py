"""
Remindly — личный Telegram-ассистент для задач.

Возможности:
  • добавление задач (пошаговый диалог /add или просто текстом);
  • приоритеты, сроки, списки /list и /today;
  • напоминания о задачах и ежедневная сводка;
  • статистика выполнения.

Все данные хранятся в JSON-файле (по умолчанию tasks.json рядом с ботом).
Часовой пояс — Europe/Moscow.
"""

import calendar
import json
import logging
import os
import re
import warnings
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
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
DATA_FILE = Path(os.getenv("DATA_FILE", Path(__file__).with_name("tasks.json")))
TZ = ZoneInfo("Europe/Moscow")
DEFAULT_DIGEST_TIME = "09:00"
MAX_TASK_LENGTH = 500
DIALOG_TIMEOUT = 10 * 60  # диалог без ответа завершается через 10 минут

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не засоряем лог HTTP-запросами
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

# Состояния диалогов: добавление задачи, подтверждение удаления,
# выбор задачи кнопками и выбор варианта напоминания
(CONFIRM, TEXT, PRIORITY, DUE, DATE_INPUT, DELETE_CONFIRM,
 PICK_TASK, REMIND_OPTION, REMIND_TIME, DUPLICATE) = range(10)

# Данные текущего диалога в context.user_data — всё это забывается при его завершении
DIALOG_KEYS = ("draft", "delete_id", "pick", "remind_id", "remind_after_add", "remind_prefix")


# ---------------------------------------------------------------------------
# Хранилище (JSON-файл)
# ---------------------------------------------------------------------------

class Storage:
    """Простое хранилище: весь файл держим в памяти и сохраняем после изменений.

    Структура файла:
    {
      "users": {
        "<user_id>": {
          "chat_id": 123,
          "next_id": 4,                       # следующий номер задачи
          "settings": {"digest_enabled": true, "digest_time": "09:00"},
          "tasks": [
            {"id": 1, "text": "...", "priority": "high",
             "due": "2026-10-14" | null,       # срок
             "remind_at": "<ISO datetime>" | null,
             "remind_note": "за сутки до срока" | null,  # выбранный вариант напоминания
             "status": "active" | "done",
             "created_at": "<ISO datetime>",
             "done_at": "<ISO datetime>" | null}
          ]
        }
      }
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = {"users": {}}
        self.load()

    def load(self) -> None:
        """Загружает данные. Повреждённый файл сохраняется как копия, бот стартует с нуля."""
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
                raise ValueError("неверная структура файла")
            self.data = data
        except (OSError, ValueError) as e:  # json.JSONDecodeError — подкласс ValueError
            backup = self.path.with_suffix(".broken.json")
            logger.error("Не удалось прочитать %s (%s). Копия сохранена в %s", self.path, e, backup)
            try:
                self.path.replace(backup)
            except OSError:
                pass

    def save(self) -> None:
        """Атомарная запись: сначала во временный файл, потом подмена.
        Так файл не испортится, если бот упадёт посреди записи."""
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get_user(self, user_id: int) -> tuple[dict, bool]:
        """Возвращает данные пользователя (создаёт при первом обращении) и флаг «создан»."""
        users = self.data["users"]
        key = str(user_id)
        if key in users:
            return users[key], False
        users[key] = {
            "chat_id": user_id,  # в личном чате chat_id совпадает с user_id
            "next_id": 1,
            "settings": {"digest_enabled": True, "digest_time": DEFAULT_DIGEST_TIME},
            "tasks": [],
        }
        return users[key], True


storage = Storage(DATA_FILE)


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


def task_due(task: dict) -> date | None:
    return date.fromisoformat(task["due"]) if task.get("due") else None


def is_overdue(task: dict) -> bool:
    due = task_due(task)
    return task["status"] == "active" and due is not None and due < today()


def task_line(task: dict) -> str:
    """Строка задачи: «#3 🔴 Подготовить отчёт · до 14.10 ⚠️ просрочено»."""
    line = f"#{task['id']} {PRIORITIES[task['priority']]['emoji']} {task['text']}"
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
    user, created = storage.get_user(user_id)
    if created:
        storage.save()
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
        InlineKeyboardButton("✅ Выполнено", callback_data=f"rdone:{task_id}"),
        InlineKeyboardButton("⏰ Отложить на 1 час", callback_data=f"snooze:{task_id}"),
    ]])


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывание напоминания."""
    user_id = context.job.data["user_id"]
    user = storage.data["users"].get(str(user_id))
    task = find_task(user, context.job.data["task_id"]) if user else None
    if not task or task["status"] != "active":
        return  # задачу успели удалить или выполнить
    task["remind_at"] = None
    task["remind_note"] = None
    storage.save()
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
    user = storage.data["users"].get(str(user_id))
    if not user:
        return
    tasks = today_tasks(user)
    if tasks:
        text = "☀️ Доброе утро! Задачи на сегодня:\n\n" + "\n".join(task_line(t) for t in tasks)
    else:
        text = "☀️ Доброе утро! На сегодня задач нет 🎉"
    try:
        await context.bot.send_message(user["chat_id"], text)
    except TelegramError as e:
        logger.warning("Не удалось отправить сводку %s: %s", user_id, e)


def restore_jobs(application: Application) -> None:
    """После перезапуска восстанавливаем сводки и напоминания из файла."""
    reminders = 0
    for key, user in storage.data["users"].items():
        user_id = int(key)
        schedule_digest(application.job_queue, user_id, user)
        for task in user["tasks"]:
            if task["status"] == "active" and task.get("remind_at"):
                schedule_reminder(application.job_queue, user_id, task)
                reminders += 1
    logger.info("Восстановлено: пользователей %d, напоминаний %d",
                len(storage.data["users"]), reminders)


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
])

# Текстовые ответы вместо кнопок: вариант -> значение (сравниваются через match_phrase)
YES_NO_WORDS = {"да": True, "ок": True, "добавить": True, "нет": False, "отмена": False}
DUPLICATE_WORDS = {**YES_NO_WORDS, "создать": True}
DELETE_WORDS = {**YES_NO_WORDS, "удалить": True, "оставить": False}
PRIORITY_WORDS = {p["name"]: key for key, p in PRIORITIES.items()}  # «высокий» -> "high"
DUE_WORDS = {"сегодня": "today", "завтра": "tomorrow", "без срока": "none", "указать дату": "custom"}

# Подсказки, если текстовый ответ не распознан
YES_NO_HINT = "Напишите «да» или «нет», либо нажмите кнопку."
PRIORITY_HINT = "Напишите: высокий, средний или низкий, либо нажмите кнопку."
DUE_HINT = "Напишите: сегодня, завтра, без срока или дату ДД.ММ, либо нажмите кнопку."


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
    "добавить": ("add", []),
    "отмена": ("cancel", []),
    # Без номера — бот покажет задачи кнопками
    "готово": ("done", []),
    "выполнено": ("done", []),
    "сделано": ("done", []),
    "удалить": ("delete", []),
    "напомни": ("remind", []),
    "напомнить": ("remind", []),
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
    "напомни": ("remind", REMIND_RE),
    "напомнить": ("remind", REMIND_RE),
    "сводка": ("digest", TIME_RE),
}


def parse_text_command(text: str) -> tuple[str, list[str]] | None:
    """«Лист» -> ("list", []), «удалить 3» -> ("delete", ["3"]).

    Командой считается только сообщение, которое целиком совпадает с вариантом:
    «Список покупок» или «Сегодня позвонить маме» — это задачи, а не команды.
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
    if name == "add":
        return name, words[1:]  # текст задачи сохраняем как есть, с регистром
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
    if name in DIALOG_COMMAND_HANDLERS:  # /add, /done, /delete, /remind, /cancel ведут диалог сами
        return await DIALOG_COMMAND_HANDLERS[name](update, context)
    await end_dialog_quietly(context)
    await COMMAND_HANDLERS[name](update, context)
    return ConversationHandler.END


async def try_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Если сообщение — текстовая команда, выполняет её и возвращает новое состояние, иначе None."""
    command = parse_text_command(update.message.text)
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


# --- Добавление задачи -------------------------------------------------------

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
    await update.message.reply_text("📝 Напишите текст задачи (или /cancel для отмены).")
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
    return await start_new_task(update, context, text, ask_confirm=True)


QUICK_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Добавить", callback_data="quick:yes"),
    InlineKeyboardButton("Отмена", callback_data="quick:no"),
]])

DUPLICATE_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("Создать", callback_data="dup:yes"),
    InlineKeyboardButton("Отмена", callback_data="dup:no"),
]])


def find_duplicate(user: dict, text: str) -> dict | None:
    """Активная задача с тем же текстом — без учёта регистра, лишних пробелов
    и знаков препинания в конце. Выполненные задачи не сравниваем."""
    key = normalize(text)
    return next((t for t in active_tasks(user) if normalize(t["text"]) == key), None)


async def start_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         text: str, ask_confirm: bool) -> int:
    """Текст задачи получен (/add, /add <текст> или быстрое добавление).

    Если такая активная задача уже есть — спрашиваем, создавать ли повтор
    (при быстром добавлении этот вопрос заменяет «Сохранить как задачу?»).
    Иначе быстрое добавление спрашивает «Сохранить как задачу?», а /add — сразу приоритет.
    """
    context.user_data["draft"] = {"text": text}
    duplicate = find_duplicate(get_user(context, update.effective_user.id), text)
    if duplicate:
        line = task_line(duplicate)
        dot = "" if line.endswith((".", "!", "?", "…")) else "."  # без «ТЕСТ!.»
        await respond(update, context, f"Такая задача уже есть: {line}{dot} Всё равно создать?",
                      DUPLICATE_KEYBOARD)
        return DUPLICATE
    if ask_confirm:
        await respond(update, context, f"Сохранить как задачу?\n«{text}»", QUICK_KEYBOARD)
        return CONFIRM
    await respond(update, context, "Какой приоритет?", PRIORITY_KEYBOARD)
    return PRIORITY


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
    """Создаём повтор (дальше — приоритет и срок) или отменяем."""
    if not yes:
        context.user_data.pop("draft", None)
        await respond(update, context, "Ок, не создаю.")
        return ConversationHandler.END
    text = context.user_data["draft"]["text"]
    await respond(update, context, f"📝 «{text}»\nКакой приоритет?", PRIORITY_KEYBOARD)
    return PRIORITY


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
    text = context.user_data["draft"]["text"]
    await respond(update, context, f"📝 «{text}»\nКакой приоритет?", PRIORITY_KEYBOARD)
    return PRIORITY


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
    context.user_data["draft"]["priority"] = priority
    p = PRIORITIES[priority]
    await respond(update, context, f"{p['emoji']} Приоритет: {p['name']}. Какой срок?", DUE_KEYBOARD)
    return DUE


async def due_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: срок выбран кнопкой."""
    query = update.callback_query
    await query.answer()
    return await set_due(update, context, query.data.split(":", 1)[1])


async def due_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: срок написан текстом — словом («завтра») или сразу датой («14.10»)."""
    # «сегодня» здесь — это срок, а не команда /today: ответ на вопрос проверяется первым
    choice = match_phrase(update.message.text, DUE_WORDS)
    if choice is not None:
        return await set_due(update, context, choice)
    due = parse_date(normalize(update.message.text))
    if due is not None:
        return await finish_add(update, context, due)
    state = await try_text_command(update, context)
    if state is not None:
        return state
    await update.message.reply_text(DUE_HINT)
    return DUE


async def set_due(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> int:
    if choice == "custom":
        await respond(update, context, "📅 Введите дату: ДД.ММ или ДД.ММ.ГГГГ, например 14.10")
        return DATE_INPUT
    due = {"today": today(), "tomorrow": today() + timedelta(days=1), "none": None}[choice]
    return await finish_add(update, context, due)


async def date_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3 (вариант): после «Указать дату» ввели дату текстом."""
    due = parse_date(normalize(update.message.text))
    if due is None:
        state = await try_text_command(update, context)
        if state is not None:
            return state
        await update.message.reply_text("Не понял дату. Пример: 14.10 или 14.10.2026")
        return DATE_INPUT
    return await finish_add(update, context, due)


def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE,
               due: date | None) -> tuple[str, dict]:
    """Сохраняет черновик как задачу. Возвращает текст подтверждения и саму задачу."""
    draft = context.user_data.pop("draft", {})
    user = get_user(context, update.effective_user.id)
    task = {
        "id": user["next_id"],  # номер никогда не переиспользуется
        "text": draft["text"],
        "priority": draft["priority"],
        "due": due.isoformat() if due else None,
        "remind_at": None,
        "remind_note": None,
        "status": "active",
        "created_at": now().isoformat(timespec="seconds"),
        "done_at": None,
    }
    user["next_id"] += 1
    user["tasks"].append(task)
    storage.save()

    p = PRIORITIES[task["priority"]]
    result = f"Задача #{task['id']} добавлена: {p['emoji']} {p['name']}, "
    result += f"срок {fmt_date(due)}" if due else "без срока"
    if due and due < today():
        result += "\n⚠️ Эта дата уже прошла — задача сразу просрочена."
    return result, task


async def finish_add(update: Update, context: ContextTypes.DEFAULT_TYPE, due: date | None) -> int:
    """Последний шаг добавления: сохраняем задачу и сразу спрашиваем, напомнить ли о ней."""
    confirmation, task = save_draft(update, context, due)
    context.user_data["remind_after_add"] = True
    context.user_data["remind_prefix"] = f"{confirmation}\n\n"  # чтобы не потерять подтверждение
    return await ask_remind_option(update, context, task, f"{confirmation}\n\nНапомнить о задаче?")


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
        InlineKeyboardButton("Да", callback_data=f"del:yes:{task_id}"),
        InlineKeyboardButton("Нет", callback_data=f"del:no:{task_id}"),
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
    if not yes:
        await respond(update, context, "Ок, задача остаётся.")
        return ConversationHandler.END
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    task = find_task(user, task_id) if task_id is not None else None
    if not task:
        await respond(update, context, f"Задача #{task_id} уже удалена.")
        return ConversationHandler.END
    user["tasks"].remove(task)
    cancel_jobs(context.job_queue, reminder_job_name(user_id, task_id))
    storage.save()
    await respond(update, context, f"🗑 Задача #{task_id} удалена.")
    return ConversationHandler.END


# --- Выход из диалога ---------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — прервать диалог."""
    had_dialog = any(key in context.user_data for key in DIALOG_KEYS)
    await end_dialog_quietly(context)
    await update.message.reply_text("Отменено." if had_dialog else "Нечего отменять 🙂")
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

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — все активные задачи."""
    tasks = active_tasks(get_user(context, update.effective_user.id))
    if not tasks:
        await update.message.reply_text("Активных задач нет 🎉 Добавить: /add")
        return
    await update.message.reply_text(
        f"📋 Активные задачи ({len(tasks)}):\n\n" + "\n".join(task_line(t) for t in tasks)
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today — задачи на сегодня и просроченные."""
    tasks = today_tasks(get_user(context, update.effective_user.id))
    if not tasks:
        await update.message.reply_text("На сегодня задач нет 🎉")
        return
    await update.message.reply_text("📅 На сегодня:\n\n" + "\n".join(task_line(t) for t in tasks))


def complete_task(context: ContextTypes.DEFAULT_TYPE, user_id: int, task: dict) -> None:
    """Отмечает задачу выполненной и снимает её напоминание."""
    task["status"] = "done"
    task["done_at"] = now().isoformat(timespec="seconds")
    task["remind_at"] = None
    task["remind_note"] = None
    cancel_jobs(context.job_queue, reminder_job_name(user_id, task["id"]))
    storage.save()


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
# Напоминания
# ---------------------------------------------------------------------------

REMIND_EXAMPLE = "Пример: /remind 3 18:30 или /remind 3 14.10 18:30"
CUSTOM_TIME_EXAMPLE = "Пример: 18:30 или 14.10 18:30"
REMIND_HOUR = time(10, 0)  # во сколько приходят напоминания «в день срока», «за сутки» и т. п.
PICK_LIMIT = 10            # сколько задач показывать кнопками
BUTTON_TEXT_LIMIT = 30     # сколько символов текста задачи помещается на кнопку


def parse_remind_moment(parts: list[str], example: str) -> tuple[datetime | None, str | None]:
    """«18:30» или «14.10 18:30» -> момент напоминания.

    Если указано только время и оно уже прошло — напоминаем завтра.
    Возвращает (момент, None) или (None, текст ошибки).
    """
    if len(parts) not in (1, 2):
        return None, f"Не понял время. {example}"
    remind_time = parse_time(parts[-1])
    if remind_time is None:
        return None, f"Не понял время. {example}"
    if len(parts) == 2:
        remind_date = parse_date(parts[0])
        if remind_date is None:
            return None, f"Не понял дату. {example}"
        when = datetime.combine(remind_date, remind_time, tzinfo=TZ)
        if when <= now():
            return None, "Это время уже прошло. Укажите момент в будущем."
        return when, None
    when = datetime.combine(today(), remind_time, tzinfo=TZ)
    if when <= now():
        when += timedelta(days=1)
    return when, None


def looks_like_time(parts: list[str]) -> bool:
    """Похоже ли сообщение на время («18:30», «14.10 18:30»), даже если в нём ошибка («25:00»)."""
    return bool(parts) and len(parts) <= 2 and re.fullmatch(TIME_RE, parts[-1]) is not None


def set_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, task: dict,
                 when: datetime, note: str | None = None) -> str:
    """Ставит напоминание (старое заменяется: у задачи оно одно) и возвращает подтверждение."""
    task["remind_at"] = when.isoformat(timespec="seconds")
    task["remind_note"] = note
    storage.save()
    schedule_reminder(context.job_queue, user_id, task)
    text = f"⏰ Напомню о задаче #{task['id']} {fmt_when(when)}"
    return f"{text} ({note})" if note else text


def clear_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, task: dict) -> bool:
    """Снимает напоминание с задачи. Возвращает True, если оно было."""
    had_reminder = bool(task.get("remind_at"))
    task["remind_at"] = None
    task["remind_note"] = None
    cancel_jobs(context.job_queue, reminder_job_name(user_id, task["id"]))
    storage.save()
    return had_reminder


def month_before(d: date) -> date:
    """То же число предыдущего месяца, а если такого числа нет — последний день месяца.
    Например, 15.11 -> 15.10, 31.03 -> 28.02 (или 29.02 в високосный год)."""
    year, month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


# Варианты, привязанные к сроку задачи
DUE_OPTION_KEYS = ("due", "day", "week", "month")


def remind_options(task: dict) -> dict[str, tuple[str, datetime | None, str | None]]:
    """Варианты напоминания для задачи: ключ -> (текст кнопки, момент, пояснение).

    Всегда: через час, завтра в 10:00, через неделю.
    Если у задачи есть срок, ещё: в день срока, за сутки, за неделю, за месяц до срока
    (в 10:00 выбранного дня). И в конце — «Указать время» и «Не напоминать».
    Варианты показываем все, даже если время какого-то уже прошло:
    при его выборе бот объяснит это и попросит выбрать другой.
    """
    current = now()
    tomorrow = datetime.combine(today() + timedelta(days=1), REMIND_HOUR, tzinfo=TZ)
    options = {
        "hour": ("Через час", current + timedelta(hours=1), "через час"),
        "tomorrow": ("Завтра в 10:00", tomorrow, None),  # день и время и так видны
        "week_later": ("Через неделю", current + timedelta(days=7), "через неделю"),
    }
    due = task_due(task)
    if due:
        # На кнопках короткие подписи: ширину кнопок Telegram задать не даёт, и при двух
        # колонках «За сутки до срока» обрезалось бы. Полное название — в пояснении:
        # оно видно в подтверждении, в /reminders и в предупреждении о прошедшем времени.
        by_due = [
            ("due", "В день срока", due, "в день срока"),
            ("day", "За сутки", due - timedelta(days=1), "за сутки до срока"),
            ("week", "За неделю", due - timedelta(days=7), "за неделю до срока"),
            ("month", "За месяц", month_before(due), "за месяц до срока"),
        ]
        for key, label, day, note in by_due:
            options[key] = (label, datetime.combine(day, REMIND_HOUR, tzinfo=TZ), note)
    options["custom"] = ("Указать время", None, None)
    options["none"] = ("Не напоминать", None, None)
    return options


def remind_keyboard(options: dict) -> InlineKeyboardMarkup:
    """Кнопки вариантов по две в ряд, «Не напоминать» — отдельной строкой внизу."""
    buttons = [InlineKeyboardButton(label, callback_data=f"ropt:{key}")
               for key, (label, _, _) in options.items() if key != "none"]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(options["none"][0], callback_data="ropt:none")])
    return InlineKeyboardMarkup(rows)


def remind_option_words(options: dict) -> dict[str, str]:
    """Текстовые ответы: названия кнопок («за сутки до срока» -> "day") и короткие формы.
    Варианты срока распознаём всегда — чтобы объяснить, если у задачи срока нет."""
    words = {normalize(label): key for key, (label, _, _) in options.items()}
    words.update({
        "завтра": "tomorrow",
        "в день срока": "due",
        "за сутки": "day",
        "за неделю": "week",
        "за месяц": "month",
        "за сутки до срока": "day",
        "за неделю до срока": "week",
        "за месяц до срока": "month",
        "нет": "none",
        "не надо": "none",
    })
    return words


def due_options_note(task: dict) -> str:
    """Строка под вопросом: кнопки «За сутки», «За неделю», «За месяц» считаются от срока."""
    return "\nВарианты «за …» — до срока" if task_due(task) else ""


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
        return await ask_remind_option(update, context, task, f"Когда напомнить?\n{task_line(task)}")
    when, error = parse_remind_moment(time_parts, REMIND_EXAMPLE)
    if error:
        await respond(update, context, error)
        return ConversationHandler.END
    await respond(update, context, set_reminder(context, user_id, task, when))
    return ConversationHandler.END


# --- Выбор задачи кнопками (/done, /delete, /remind без номера) -----------------------

PICK_PROMPTS = {
    "done": "Какую задачу отметить выполненной?\nНажмите кнопку или напишите номер.",
    "delete": "Какую задачу удалить?\nНажмите кнопку или напишите номер.",
    "remind": ("Для какой задачи поставить напоминание?\n"
               "Нажмите кнопку или напишите номер — можно сразу со временем: 1 11:43"),
}
PICK_HINTS = {
    "done": "Нажмите кнопку с задачей или напишите её номер, например 3.",
    "delete": "Нажмите кнопку с задачей или напишите её номер, например 3.",
    "remind": "Нажмите кнопку с задачей или напишите номер, например 3 или 3 18:30.",
}


def button_label(task: dict) -> str:
    """«#4 Подготовить отчёт по лаб…»: номер и начало текста — не больше 30 символов."""
    text = task["text"]
    if len(text) > BUTTON_TEXT_LIMIT:
        text = text[:BUTTON_TEXT_LIMIT - 1].rstrip() + "…"
    return f"#{task['id']} {text}"


async def ask_pick_task(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> int:
    """Показывает активные задачи кнопками (в порядке /list) для done / delete / remind."""
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
    await respond(update, context, prompt, keyboard)
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
    return ConversationHandler.END


# --- Выбор варианта напоминания -----------------------------------------------------

async def ask_remind_option(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            task: dict, question: str) -> int:
    """Показывает варианты напоминания для задачи."""
    context.user_data["remind_id"] = task["id"]
    await respond(update, context, question + due_options_note(task),
                  remind_keyboard(remind_options(task)))
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
    for key in ("remind_id", "remind_after_add", "remind_prefix"):
        context.user_data.pop(key, None)
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
        if key in DUE_OPTION_KEYS:
            return await ask_remind_again(update, context, task,
                                          "У задачи нет срока, этот вариант не подходит. "
                                          "Выберите другой вариант.")
        return await ask_remind_again(update, context, task, "Выберите другой вариант.")
    label, when, note = options[key]
    if when is not None and when <= now():
        name = note[0].upper() + note[1:] if note else label  # «За неделю до срока»
        return await ask_remind_again(update, context, task,
                                      f"Время для варианта «{name}» уже прошло "
                                      f"({fmt_when(when)}). Выберите другой вариант.")
    if key == "custom":
        prefix = context.user_data.get("remind_prefix", "") if update.callback_query else ""
        await respond(update, context,
                      f"{prefix}🕒 Напишите время: ЧЧ:ММ или ДД.ММ ЧЧ:ММ. {CUSTOM_TIME_EXAMPLE}")
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
    await respond(update, context, text, remind_keyboard(remind_options(task)))
    return REMIND_OPTION


async def apply_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            parts: list[str]) -> int:
    """Напоминание на время, написанное текстом: «18:30» или «14.10 18:30»."""
    task = task_in_remind_dialog(update, context)
    if not task:
        return await finish_remind_dialog(update, context, "Задача уже удалена или выполнена.")
    when, error = parse_remind_moment(parts, CUSTOM_TIME_EXAMPLE)
    if error:
        await update.message.reply_text(error)
        return REMIND_TIME
    result = set_reminder(context, update.effective_user.id, task, when)
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
        storage.save()
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
    await update.message.reply_text(f"⏰ Напоминания ({len(tasks)}):\n\n" + "\n".join(lines))


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/digest, /digest on|off, /digest 08:30."""
    user_id = update.effective_user.id
    user = get_user(context, user_id)
    settings = user["settings"]
    arg = context.args[0].lower() if len(context.args) == 1 else None
    example = "Пример: /digest on, /digest off или /digest 08:30"

    if not context.args:
        status = (f"включена, приходит в {settings['digest_time']}"
                  if settings["digest_enabled"] else "выключена")
        await update.message.reply_text(f"Ежедневная сводка {status}.\n{example}")
        return

    if arg == "off":
        settings["digest_enabled"] = False
        reply = "🔕 Ежедневная сводка выключена."
    elif arg == "on":
        settings["digest_enabled"] = True
        reply = f"🔔 Ежедневная сводка включена, приходит в {settings['digest_time']}."
    elif arg and parse_time(arg):
        settings["digest_time"] = parse_time(arg).strftime("%H:%M")
        settings["digest_enabled"] = True
        reply = f"🔔 Сводка будет приходить в {settings['digest_time']}."
    else:
        await update.message.reply_text(f"Не понял параметр. {example}")
        return

    storage.save()
    schedule_digest(context.job_queue, user_id, user)
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Статистика и служебные команды
# ---------------------------------------------------------------------------

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — сводные цифры."""
    tasks = get_user(context, update.effective_user.id)["tasks"]
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

    await update.message.reply_text(
        "📊 Статистика\n\n"
        f"Всего задач: {len(tasks)}\n"
        f"Активных: {len(active)}\n"
        f"Выполненных: {len(done)}\n"
        f"Просроченных: {len(overdue)}\n"
        f"Выполнено в срок: {on_time_text}"
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие. Новому пользователю включается сводка (см. get_user)."""
    user = get_user(context, update.effective_user.id)
    user["chat_id"] = update.effective_chat.id
    storage.save()
    await update.message.reply_text(
        f"👋 Привет, {update.effective_user.first_name}! Я Remindly — помогу не забывать о делах.\n\n"
        "• Напишите задачу текстом или используйте /add\n"
        "• /list — все задачи, /today — на сегодня\n"
        "• /remind 3 18:30 — напомню о задаче #3\n"
        f"• Каждый день в {user['settings']['digest_time']} пришлю сводку. Все команды — /help"
    )


HELP_TEXT = """📖 Команды Remindly

/add — добавить задачу пошагово
/add Купить хлеб — то же, но сразу с текстом
/list — все активные задачи
/today — задачи на сегодня и просроченные
/done — выбрать задачу кнопками и отметить выполненной
/done 3 — отметить задачу #3 выполненной
/delete — выбрать задачу кнопками и удалить
/delete 3 — удалить задачу #3
/remind — выбрать задачу кнопками и вариант напоминания
/remind 3 — варианты напоминания для задачи #3
/remind 3 18:30 — напомнить сегодня (если время прошло — завтра)
/remind 3 14.10 18:30 — напомнить в конкретный день
/reminders — задачи с напоминаниями
/digest — статус ежедневной сводки
/digest on / /digest off — включить / выключить сводку
/digest 08:30 — изменить время сводки
/stats — статистика
/cancel — прервать текущий диалог

💬 Команды можно писать и обычным текстом:
«список», «лист», «задачи», «мои задачи» — /list
«сегодня», «на сегодня» — /today
«напоминания» — /reminders
«статистика» — /stats
«помощь», «команды» — /help
«сводка», «сводка вкл», «сводка выкл», «сводка 08:30» — /digest
«добавить», «добавить Купить хлеб» — /add
«готово», «выполнено», «сделано» (можно с номером: «готово 3») — /done
«удалить», «удалить 3» — /delete
«напомни», «напомни 3», «напомни 3 18:30», «напомни 3 14.10 18:30» — /remind
«отмена» — /cancel
«привет», «здравствуйте», «начать» — /start

⏰ Варианты напоминания: через час, завтра в 10:00, через неделю; если у задачи есть срок —
ещё в день срока, за сутки, за неделю и за месяц до срока (в 10:00). Всегда можно указать
время или отказаться. После добавления задачи я сам предложу поставить напоминание.

🔁 Если такая задача уже есть среди активных, я переспрошу, создавать ли ещё одну.

💡 Любой другой текст я предложу сохранить как задачу.
На вопросы с кнопками можно отвечать текстом: «да», «высокий», «завтра», «14.10».
Небольшие опечатки не страшны: «звтра», «срдений», «спсок»."""


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


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

BOT_COMMANDS = [
    BotCommand("add", "Добавить задачу"),
    BotCommand("list", "Все активные задачи"),
    BotCommand("today", "Задачи на сегодня"),
    BotCommand("done", "Отметить задачу выполненной"),
    BotCommand("delete", "Удалить задачу"),
    BotCommand("remind", "Поставить напоминание"),
    BotCommand("reminders", "Задачи с напоминаниями"),
    BotCommand("digest", "Ежедневная сводка: on / off / 08:30"),
    BotCommand("stats", "Статистика"),
    BotCommand("help", "Список команд"),
    BotCommand("cancel", "Прервать диалог"),
    BotCommand("start", "Начать"),
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
}

# Команды, которые сами начинают или завершают диалог (обрабатываются в ConversationHandler)
DIALOG_COMMAND_HANDLERS = {
    "add": add_start,
    "done": done_cmd,
    "delete": delete_cmd,
    "remind": remind_cmd,
    "cancel": cancel,
}


async def post_init(application: Application) -> None:
    """Выполняется один раз после запуска: меню команд и восстановление задач планировщика."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    restore_jobs(application)


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .post_init(post_init)
        .build()
    )

    text_only = filters.TEXT & ~filters.COMMAND

    # Обработчики разбиты на две группы. В каждой группе срабатывает максимум один обработчик,
    # но группы проверяются по очереди. Поэтому команда посреди диалога сначала молча
    # завершает диалог (группа 0), а потом выполняется как обычно (группа 1).

    # --- Группа 0: диалоги и кнопки ---
    dialog = ConversationHandler(
        entry_points=[
            *(CommandHandler(name, callback) for name, callback in DIALOG_COMMAND_HANDLERS.items()),
            MessageHandler(text_only, quick_offer),  # быстрое добавление
        ],
        states={
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
            DUPLICATE: [
                CallbackQueryHandler(duplicate_button, pattern=r"^dup:"),
                MessageHandler(text_only, duplicate_text),
            ],
            ConversationHandler.TIMEOUT: [TypeHandler(Update, dialog_timeout)],
        },
        fallbacks=[
            # /add, /done, /delete, /remind, /cancel посреди диалога начинают новый диалог
            *(CommandHandler(name, callback) for name, callback in DIALOG_COMMAND_HANDLERS.items()),
            MessageHandler(filters.COMMAND, end_on_command),  # остальные команды — см. группу 1
        ],
        conversation_timeout=DIALOG_TIMEOUT,
    )
    application.add_handler(dialog)

    # Кнопки, нажатые вне диалога
    application.add_handler(CallbackQueryHandler(delete_button, pattern=r"^del:"))
    application.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^(rdone|snooze):"))
    application.add_handler(CallbackQueryHandler(stale_button))

    # --- Группа 1: обычные команды (команды из DIALOG_COMMAND_HANDLERS обрабатывает диалог) ---
    for name, callback in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, callback), group=1)

    # Неизвестная команда — последней. Команды диалога исключаем, иначе ответ придёт дважды.
    dialog_commands = filters.Regex(rf"^/({'|'.join(DIALOG_COMMAND_HANDLERS)})(@\w+)?(\s|$)")
    application.add_handler(MessageHandler(filters.COMMAND & ~dialog_commands, unknown_cmd), group=1)

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")
    logger.info("Remindly запускается, данные: %s", DATA_FILE)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
