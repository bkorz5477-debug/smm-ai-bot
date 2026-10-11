import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import KICKED, MEMBER, ChatMemberUpdatedFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    PreCheckoutQuery,
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# ====================================================
# КОНФИГ (всё из Environment на Render)
# ====================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # только для голосовых (Whisper)
DATA_FILE = os.getenv("DATA_FILE", "user_data.json")

if not BOT_TOKEN:
    sys.exit("BOT_TOKEN не задан")
if not DEEPSEEK_API_KEY:
    sys.exit("DEEPSEEK_API_KEY не задан")

ADMIN_TELEGRAM_ID = 985130256
VIP_USER_IDS = [985130256]

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
whisper_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

DEFAULT_FREE_ATTEMPTS = 3
TG_LIMIT = 3800
LINE = "━━━━━━━━━━━━━━━━━━"

# ====================================================
# ХРАНЕНИЕ ДАННЫХ
# ====================================================
DATE_FIELDS = ("sub_until", "attempts_spent_at", "gift_until", "trial_until")
TZ = ZoneInfo("Europe/Kyiv")  # для отложенных публикаций
TRIAL_DAYS = 3


# Если задан DATABASE_URL — всё хранится в PostgreSQL и переживает деплои.
# Если нет — в JSON-файлах (удобно для запуска на своём компьютере).
DATABASE_URL = os.getenv("DATABASE_URL")
db_pool = None            # пул соединений с базой, создаётся в main()
_pending: set[str] = set()  # что изменилось и ждёт записи в базу


def serialize_user(info: dict) -> dict:
    item = dict(info)
    for field in DATE_FIELDS:
        if item.get(field):
            item[field] = item[field].isoformat()
    return item


def deserialize_user(info: dict) -> dict:
    for field in DATE_FIELDS:
        if info.get(field):
            info[field] = datetime.fromisoformat(info[field])
    return info


def load_data() -> dict:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.error(f"Не удалось прочитать {DATA_FILE}: {e}")
        return {}
    return {int(uid): deserialize_user(info) for uid, info in raw.items()}


def save_data():
    if DATABASE_URL:
        _pending.add("users")  # запишется в базу в ближайшие секунды
        return
    try:
        serializable = {str(uid): serialize_user(info) for uid, info in user_data.items()}
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        logging.error(f"Не удалось сохранить данные: {e}")


user_data = load_data()

# ====================================================
# 📊 АНАЛИТИКА: события по дням, оплаты, инструменты
# ====================================================
ANALYTICS_FILE = os.getenv("ANALYTICS_FILE", "analytics.json")
KEEP_DAYS = 120  # сколько дней хранить подневную статистику


def load_analytics() -> dict:
    try:
        with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("days", {})
    data.setdefault("payments", [])
    data.setdefault("tools", {})
    return deserialize_analytics(data)


def deserialize_analytics(data: dict) -> dict:
    data.setdefault("days", {})
    data.setdefault("payments", [])
    data.setdefault("tools", {})
    for day in data["days"].values():
        day["active"] = set(day.get("active", []))
    return data


def serialize_analytics() -> dict:
    return {
        **analytics,
        "days": {k: {**v, "active": list(v["active"])} for k, v in analytics["days"].items()},
    }


def save_analytics():
    if DATABASE_URL:
        _pending.add("analytics")
        return
    try:
        serializable = serialize_analytics()
        tmp = ANALYTICS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
        os.replace(tmp, ANALYTICS_FILE)
    except Exception as e:
        logging.error(f"Не удалось сохранить аналитику: {e}")


analytics = load_analytics()


def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def day_bucket(key: str | None = None) -> dict:
    key = key or today_key()
    if key not in analytics["days"]:
        analytics["days"][key] = {"new": 0, "gens": 0, "stars": 0, "active": set()}
        # чистим слишком старые дни
        for old in sorted(analytics["days"])[:-KEEP_DAYS]:
            analytics["days"].pop(old, None)
    return analytics["days"][key]


def track_active(user_id: int):
    day_bucket()["active"].add(user_id)
    if user_id in user_data:
        user_data[user_id]["last_seen"] = today_key()


def track_new_user():
    day_bucket()["new"] += 1


def track_generation(tool: str):
    day_bucket()["gens"] += 1
    analytics["tools"][tool] = analytics["tools"].get(tool, 0) + 1


def track_payment(user_id: int, payload: str, stars: int):
    day_bucket()["stars"] += stars
    analytics["payments"].append({
        "user": user_id, "payload": payload, "stars": stars,
        "date": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
    })
    analytics["payments"] = analytics["payments"][-5000:]
    save_analytics()


# ====================================================
# 🗄 БАЗА ДАННЫХ PostgreSQL
# ====================================================
# Устройство простое и надёжное: бот держит данные в памяти (быстро),
# а все изменения каждые 3 секунды записывает в базу.
# В базу пишутся только изменившиеся пользователи — это экономно даже при тысячах людей.
_user_hashes: dict[int, int] = {}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


async def db_init():
    """Подключается к базе, создаёт таблицы и загружает данные."""
    global db_pool
    import asyncpg  # импорт здесь — чтобы без базы бот работал и без этой библиотеки

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
    async with db_pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS users (id BIGINT PRIMARY KEY, data JSONB NOT NULL)")
        await conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value JSONB NOT NULL)")
        user_rows = await conn.fetch("SELECT id, data FROM users")
        kv_rows = await conn.fetch("SELECT key, value FROM kv")

    kv = {r["key"]: json.loads(r["value"]) for r in kv_rows}

    if user_rows:
        user_data.clear()
        for r in user_rows:
            raw = r["data"]
            user_data[r["id"]] = deserialize_user(json.loads(raw))
            _user_hashes[r["id"]] = hash(_dump(json.loads(raw)))
    else:
        _pending.add("users")  # база пустая — переносим то, что было в файлах

    if "cases" in kv:
        cases[:] = kv["cases"]
    else:
        _pending.add("cases")
    if "settings" in kv:
        settings.clear()
        settings.update({**SETTINGS_DEFAULTS, **kv["settings"]})
    else:
        _pending.add("settings")
    if "analytics" in kv:
        analytics.clear()
        analytics.update(deserialize_analytics(kv["analytics"]))
    else:
        _pending.add("analytics")

    logging.info(f"База данных подключена: пользователей {len(user_data)}, кейсов {len(cases)}")
    await db_flush()


async def db_flush():
    """Записывает в базу всё, что изменилось с прошлого раза."""
    if not db_pool or not _pending:
        return
    pending = set(_pending)
    _pending.clear()
    try:
        async with db_pool.acquire() as conn:
            if "users" in pending:
                rows, new_hashes = [], {}
                for uid, info in list(user_data.items()):
                    payload = _dump(serialize_user(info))
                    h = hash(payload)
                    if _user_hashes.get(uid) != h:
                        rows.append((uid, payload))
                        new_hashes[uid] = h
                if rows:
                    await conn.executemany(
                        "INSERT INTO users (id, data) VALUES ($1, $2::jsonb) "
                        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                        rows,
                    )
                    _user_hashes.update(new_hashes)
            blobs = {
                "cases": lambda: cases,
                "settings": lambda: settings,
                "analytics": serialize_analytics,
            }
            for key, getter in blobs.items():
                if key in pending:
                    await conn.execute(
                        "INSERT INTO kv (key, value) VALUES ($1, $2::jsonb) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        key, _dump(getter()),
                    )
    except Exception as e:
        _pending.update(pending)  # не получилось — попробуем в следующий раз
        logging.error(f"Ошибка записи в базу: {e}")


async def db_loop():
    while True:
        await asyncio.sleep(3)
        await db_flush()


async def flush_loop():
    """Сохраняет активность раз в минуту, чтобы не писать на каждое нажатие."""
    while True:
        await asyncio.sleep(60)
        save_analytics()
        save_data()


@dp.update.outer_middleware()
async def activity_middleware(handler, event, data):
    """Отмечает пользователя активным при любом действии: сообщение, кнопка, оплата."""
    user = data.get("event_from_user")
    if user and not user.is_bot:
        track_active(user.id)
    return await handler(event, data)

# ====================================================
# ИНСТРУМЕНТЫ — сгруппированы по темам
# ====================================================
CATEGORIES = {
    "posts": ("📝 Посты и тексты", "Готовые тексты под любую площадку"),
    "video": ("🎬 Reels и Stories", "Сценарии, хуки и прогревы"),
    "strategy": ("📈 Стратегия и продажи", "Контент-план, воронки, консалтинг"),
    "visual": ("🎨 Визуал и AI-арт", "Промпты и стиль ленты"),
    "audience": ("💬 Аудитория и конкуренты", "Ответы на отзывы, разбор конкурентов"),
}

TOOLS = {
    # --- Посты ---
    "ig_post": {
        "cat": "posts", "name": "📸 Пост Instagram", "pro": False,
        "desc": "Хук, сторителлинг, CTA и хэштеги",
        "hint": "Напишите тему поста или пару слов о продукте",
        "prompt": "Ты топовый Instagram-копирайтер. Напиши пост: цепляющий хук в первой строке, ценность или история, призыв к действию. В конце — 5–10 релевантных хэштегов и короткая идея для визуала.",
    },
    "carousel": {
        "cat": "posts", "name": "🎠 Карусель", "pro": False,
        "desc": "Сценарий 7–10 слайдов + подпись",
        "hint": "Напишите тему карусели",
        "prompt": "Создай сценарий карусели на 7–10 слайдов: слайд 1 — хук-обложка, далее по одной мысли на слайд, последний — CTA. Для каждого слайда: заголовок, текст до 25 слов, подсказка по визуалу. В конце — подпись к посту.",
    },
    "tg_post": {
        "cat": "posts", "name": "✈️ Пост Telegram", "pro": False,
        "desc": "Ёмкий пост для канала",
        "hint": "Напишите тему поста",
        "prompt": "Напиши пост для Telegram-канала: сильное первое предложение, короткие абзацы, эмодзи-маркеры, в конце вопрос к аудитории или CTA.",
    },
    "omni": {
        "cat": "posts", "name": "🌐 Кросс-постинг", "pro": False,
        "desc": "Одна тема → 5 площадок",
        "hint": "Напишите тему — адаптирую под Instagram, Facebook, Telegram, LinkedIn и X",
        "prompt": "Ты SMM-архитектор. Адаптируй тему под 5 площадок: Instagram, Facebook, Telegram, LinkedIn, X. Учитывай длину и стиль каждой платформы.",
    },
    # --- Видео ---
    "reels": {
        "cat": "video", "name": "🎬 Сценарий Reels", "pro": False,
        "desc": "Таймкоды, хук, раскадровка",
        "hint": "Напишите тему или идею ролика",
        "prompt": "Ты сценарист вирусных Reels/TikTok. Напиши сценарий с таймкодами: хук 0–3 сек, развитие, кульминация, CTA. Для каждой сцены — что в кадре, текст на экране, что говорить.",
    },
    "hooks": {
        "cat": "video", "name": "🪝 10 хуков", "pro": False,
        "desc": "Первые 3 секунды, которые держат",
        "hint": "Напишите тему видео или поста",
        "prompt": "Придумай 10 хуков для первых 3 секунд видео или первой строки поста по типам: боль, выгода, интрига, провокация, цифры. Отметь 3 самых сильных и объясни почему.",
    },
    "abtest": {
        "cat": "video", "name": "🧪 A/B заголовки", "pro": False,
        "desc": "3 варианта под разные психотипы",
        "hint": "Напишите ваш текущий заголовок или тему",
        "prompt": "Ты таргетолог. Создай 3 A/B варианта заголовка: 1) Боль/FOMO, 2) Выгода/ROI, 3) Интрига. Для каждого — для какой аудитории он сработает лучше.",
    },
    "stories": {
        "cat": "video", "name": "📱 Прогрев в Stories", "pro": False,
        "desc": "Серия 5–7 Stories к продаже",
        "hint": "Напишите, что продаёте",
        "prompt": "Создай серию из 5–7 Stories с прогревом к продаже: текст на экране, интерактив (опрос, вопрос, слайдер), что показать в кадре. Последняя Stories — продажа с CTA.",
    },
    # --- Стратегия ---
    "content_plan": {
        "cat": "strategy", "name": "📅 Контент-план 30 дней", "pro": True,
        "desc": "План на месяц: формат, тема, цель",
        "hint": "Опишите нишу и цель на месяц (охваты, продажи, запуск)",
        "prompt": "Составь контент-план на 30 дней списком по дням: формат, тема, цель (охват / доверие / продажа), хук. Соблюдай баланс 40% охват / 40% доверие / 20% продажи.",
    },
    "funnel": {
        "cat": "strategy", "name": "💰 Воронка продаж", "pro": True,
        "desc": "От подписчика до покупателя",
        "hint": "Опишите продукт и его цену",
        "prompt": "Ты CMO. Разработай воронку продаж через соцсети: лид-магнит, прогрев по шагам, оффер, работа с возражениями, дожим. Дай конкретные тексты для ключевых точек.",
    },
    "consultant": {
        "cat": "strategy", "name": "🏛 AI-консультант", "pro": True,
        "desc": "Стратегический разбор как у CMO",
        "hint": "Опишите ситуацию: проект, цифры, что не работает",
        "prompt": "Ты элитный CMO. Проведи стратегический аудит ситуации: диагноз, 3 главные точки роста, план действий на 2 недели с конкретными шагами.",
    },
    "bio": {
        "cat": "strategy", "name": "👤 Шапка профиля", "pro": False,
        "desc": "3 варианта шапки + Highlights",
        "hint": "Кто вы и что предлагаете?",
        "prompt": "Напиши 3 варианта шапки профиля Instagram (до 150 символов): кто вы, для кого, результат, CTA. Плюс названия для 5 обложек Highlights.",
    },
    # --- Визуал ---
    "midjourney": {
        "cat": "visual", "name": "🎨 Промпты Midjourney", "pro": False,
        "desc": "Готовые промпты 9:16 и 1:1",
        "hint": "Опишите, какое изображение нужно",
        "prompt": "Ты AI-арт директор. Напиши 2 детальных промпта на английском для Midjourney v6 (--ar 9:16 и --ar 1:1) и коротко объясни идею каждого.",
    },
    "visual_grid": {
        "cat": "visual", "name": "🧩 Стиль ленты", "pro": False,
        "desc": "Палитра, шрифты, сетка 9 постов",
        "hint": "Опишите нишу и настроение бренда",
        "prompt": "Разработай визуальную концепцию ленты Instagram: палитра (HEX), шрифты, стиль фото, схема чередования 9 постов, идеи для обложек.",
    },
    # --- Аудитория и конкуренты ---
    "reply_comment": {
        "cat": "audience", "name": "💬 Ответ на отзыв", "pro": False,
        "desc": "3 варианта ответа, даже на негатив",
        "hint": "Вставьте комментарий или отзыв клиента",
        "prompt": "Ты комьюнити-менеджер бренда. Напиши 3 варианта ответа на комментарий или отзыв: официальный, тёплый, с лёгким юмором. Если отзыв негативный — признай проблему, предложи решение и переведи диалог в личные сообщения.",
    },
    "competitor": {
        "cat": "audience", "name": "🕵️ Разбор конкурента", "pro": False,
        "desc": "Что работает у них и как сделать лучше",
        "hint": "Вставьте текст поста конкурента",
        "prompt": "Разбери пост конкурента: какие приёмы использованы (хук, триггеры, оффер, CTA), что сильно, что слабо. Затем дай 3 идеи, как сделать лучше для нашего бренда, и пример нашего поста на ту же тему.",
    },
    "hashtags": {
        "cat": "audience", "name": "#️⃣ Хэштеги и SEO", "pro": False,
        "desc": "30 тегов по частотности + ключи",
        "hint": "Напишите тему поста или нишу",
        "prompt": "Подбери 30 хэштегов в трёх группах: высокочастотные, среднечастотные, нишевые и локальные. Добавь 5 ключевых слов для поиска внутри соцсетей и совет, сколько тегов ставить.",
    },
    "crisis": {
        "cat": "audience", "name": "🚨 Антикризис", "pro": True,
        "desc": "Пост и план реакции на 48 часов",
        "hint": "Опишите, что случилось",
        "prompt": "Ты PR-специалист. Подготовь антикризисный пост от имени бренда и пошаговый план реакции на 48 часов: что говорить, чего избегать, как работать с комментариями.",
    },
    # --- Аудит (отдельная кнопка в главном меню) ---
    "audit": {
        "cat": None, "name": "🔍 Аудит текста / ДЗ", "pro": False,
        "desc": "Оценка, ошибки и улучшенная версия",
        "hint": "Пришлите текст поста, сценарий или домашнее задание",
        "prompt": "Ты строгий SMM арт-директор и ментор. Проведи аудит текста: 1) оценка 1–10 по Хуку, Пользе и CTA, 2) сильные стороны, 3) ошибки и слабые места, 4) идеальная доработанная версия текста.",
    },
}

TONES = {
    "friendly": "😊 Дружелюбный",
    "expert": "🎓 Экспертный",
    "bold": "🔥 Дерзкий",
    "premium": "💎 Премиальный",
}

LANG_INSTRUCTION = {
    "uk": "Відповідай українською мовою.",
    "ru": "Отвечай на русском языке.",
    "en": "Answer in English.",
}

UI = {
    "uk": {
        "hello": "👋 Вітаю, <b>{name}</b>!",
        "create": "✍️ Створити контент", "audit": "🔍 Аудит тексту",
        "brand": "🎯 Мій бренд", "academy": "🎓 Академія",
        "profile": "👤 Профіль", "pro": "💎 PRO-доступ",
        "bonus": "🎁 +Генерації", "lang": "🌐 Мова", "partner": "🤝 Співпраця",
        "saved": "📂 Мої матеріали", "channel": "📤 Мій канал",
        "cases": "🔥 Розбори постів: кращі та провальні",
    },
    "ru": {
        "hello": "👋 Здравствуйте, <b>{name}</b>!",
        "create": "✍️ Создать контент", "audit": "🔍 Аудит текста",
        "brand": "🎯 Мой бренд", "academy": "🎓 Академия",
        "profile": "👤 Профиль", "pro": "💎 PRO-доступ",
        "bonus": "🎁 +Генерации", "lang": "🌐 Язык", "partner": "🤝 Сотрудничество",
        "saved": "📂 Мои материалы", "channel": "📤 Мой канал",
        "cases": "🔥 Разборы постов: лучшие и провальные",
    },
    "en": {
        "hello": "👋 Hello, <b>{name}</b>!",
        "create": "✍️ Create content", "audit": "🔍 Text audit",
        "brand": "🎯 My brand", "academy": "🎓 Academy",
        "profile": "👤 Profile", "pro": "💎 PRO access",
        "bonus": "🎁 +Credits", "lang": "🌐 Language", "partner": "🤝 Partnership",
        "saved": "📂 My library", "channel": "📤 My channel",
        "cases": "🔥 Post breakdowns: best & worst",
    },
}

# ====================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ====================================================
def esc(text: str) -> str:
    return html.escape(text or "")


def md_to_html(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", r"<pre>\1</pre>", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    return text


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts or [""]


async def safe_edit(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        # сообщение нельзя отредактировать (например, это счёт) — шлём новое
        await message.answer(text, reply_markup=reply_markup)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def grid(buttons: list, cols: int = 2) -> list:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def get_user_info(user_id: int) -> dict:
    if user_id not in user_data:
        user_data[user_id] = {
            "attempts": DEFAULT_FREE_ATTEMPTS,
            "sub_until": None,
            "lang": "uk",
            "ref_count": 0,
            "referred_by": None,
            "attempts_spent_at": None,
        }
    u = user_data[user_id]
    # новые поля + миграция пользователей со старой версии
    u.setdefault("tool", "audit")
    u.setdefault("brand", {})
    u.setdefault("total_gens", 0)
    u.setdefault("last_prompt", None)
    u.setdefault("last_answer", None)
    u.setdefault("vip", False)        # бесплатный доступ навсегда (выдал владелец)
    u.setdefault("gift_until", None)  # бесплатный доступ на срок (выдал владелец)
    u.setdefault("name", "")
    u.setdefault("trial_used", False)
    u.setdefault("trial_until", None)
    u.setdefault("saved", [])       # сохранённые материалы
    u.setdefault("channel", None)   # {"id": ..., "title": ...} — канал для автопостинга
    u.setdefault("scheduled", [])   # отложенные публикации
    u.setdefault("notify", True)    # уведомления о новых разборах
    u.setdefault("ads", True)       # согласие на рекламные рассылки
    u.setdefault("blocked", False)  # пользователь заблокировал бота
    u.setdefault("blocked_at", "")  # когда заблокировал (строка)
    u.setdefault("username", "")
    u.setdefault("joined", datetime.now(TZ).strftime("%Y-%m-%d"))
    u.setdefault("last_seen", u["joined"])
    u.setdefault("source", "direct")   # откуда пришёл: direct / referral / promo_channel / src_*
    u.setdefault("paid_stars", 0)      # сколько звёзд заплатил всего
    u.setdefault("cases_solved", [])  # id кейсов, решённых в практике
    u.setdefault("practice_date", "")
    u.setdefault("practice_count", 0)
    if u["tool"] not in TOOLS:
        u["tool"] = "audit"
    return u


def gift_active(u: dict) -> bool:
    return bool(u.get("gift_until") and u["gift_until"] > datetime.now())


def trial_active(u: dict) -> bool:
    return bool(u.get("trial_until") and u["trial_until"] > datetime.now())


def is_sub_active(user_id: int, u: dict) -> bool:
    if user_id in VIP_USER_IDS or u.get("vip") or gift_active(u) or trial_active(u):
        return True
    if u["sub_until"] and u["sub_until"] > datetime.now():
        return True
    if u["sub_until"]:
        u["sub_until"] = None
        save_data()
    return False


def status_line(user_id: int, u: dict) -> str:
    if user_id in VIP_USER_IDS or u.get("vip"):
        return "👑 <b>VIP</b> · безлимит"
    paid = u["sub_until"] if u["sub_until"] and u["sub_until"] > datetime.now() else None
    gift = u["gift_until"] if gift_active(u) else None
    until = max(d for d in (paid, gift) if d) if (paid or gift) else None
    if until:
        return f"💎 <b>PRO</b> до {until:%d.%m.%Y} · безлимит"
    if trial_active(u):
        return f"🎁 <b>Пробный PRO</b> до {u['trial_until']:%d.%m %H:%M} · безлимит"
    return f"🟡 <b>Free</b> · осталось генераций: <b>{u['attempts']}</b>"


async def notify_admin(error_text: str, user_id: int = None):
    try:
        msg = f"🚨 <b>ОШИБКА В БОТЕ</b>\nUser: <code>{user_id}</code>\n\n<code>{esc(error_text[:3000])}</code>"
        await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=msg)
    except Exception as e:
        logging.error(f"Failed to notify admin: {e}")


# ====================================================
# ЭКРАНЫ (текст + клавиатура)
# ====================================================
def main_menu(user: types.User) -> tuple[str, InlineKeyboardMarkup]:
    u = get_user_info(user.id)
    t = UI[u["lang"]]
    tool = TOOLS[u["tool"]]
    brand = u["brand"]

    text = (
        f"{t['hello'].format(name=esc(user.first_name))}\n"
        f"<b>SMM Architecture AI Ultra</b> — ваш AI-маркетолог 24/7.\n\n"
        f"{status_line(user.id, u)}\n"
        f"🛠 Инструмент: <b>{tool['name']}</b>\n"
    )
    if brand:
        text += f"🎯 Бренд: <b>{esc(brand.get('niche', ''))}</b>\n"
    text += f"\n{LINE}\n💬 Просто отправьте текст или 🎙 голосовое — я сразу начну работу."
    if not brand:
        text += "\n\n💡 <i>Настройте «Мой бренд» за 30 секунд — тексты станут точнее и в вашем стиле.</i>"

    rows = [[btn(t["create"], "create")], [btn(t["cases"], "cases")]]
    if not u["trial_used"] and not is_sub_active(user.id, u):
        rows.append([btn(f"🎁 {TRIAL_DAYS} дня PRO бесплатно", "trial")])
    if not brand:
        rows.append([btn("⚡️ Настроить бренд за 30 сек", "brand_setup")])
    rows += [
        [btn(t["audit"], "tool_audit"), btn(t["brand"], "brand")],
        [btn(t["saved"], "saved"), btn(t["channel"], "channel")],
        [btn(t["academy"], "academy_menu"), btn(t["profile"], "profile")],
        [btn(t["pro"], "buy_menu"), btn(t["bonus"], "ref_menu")],
        [btn(t["partner"], "partner"), btn(t["lang"], "lang_menu")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def back_main_row() -> list:
    return [btn("🏠 Главное меню", "back_to_main")]


def result_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [btn("🔄 Ещё вариант", "act_more"), btn("✂️ Короче", "act_short")],
        [btn("🔥 Сильнее продажа", "act_sell"), btn("😊 Проще и живее", "act_simple")],
        [btn("📤 В мой канал", "pub"), btn("⭐ Сохранить", "save")],
        [btn("🛠 Другой инструмент", "create"), btn("🏠 Меню", "back_to_main")],
    )


def limit_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [btn("🎁 Акция: 10 генераций — 30 ⭐", "buy_pack_30")],
        [btn("💎 PRO безлимит на месяц — 500 ⭐", "buy_sub_month")],
        [btn("👥 Пригласить друга (+5 бесплатно)", "ref_menu")],
    )


def pro_upsell(tool_name: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🔒 <b>{tool_name}</b> — инструмент PRO\n{LINE}\n\n"
        "В PRO вы получаете:\n"
        "• 📅 Контент-план на 30 дней\n"
        "• 💰 Воронки продаж с готовыми текстами\n"
        "• 🏛 AI-консультант уровня CMO\n"
        "• ⏰ Отложенный постинг в ваш канал\n"
        "• 🚨 Антикризисные посты\n"
        "• ♾ Безлимит на все инструменты\n\n"
        "Одна генерация контент-плана экономит часы работы SMM-специалиста."
    )
    return text, kb(
        [btn("💎 Открыть PRO — 500 ⭐ / мес", "buy_sub_month")],
        [btn("👑 VIP на год — 1000 ⭐", "buy_sub_year")],
        [btn("◀️ Назад", "create")],
    )


# ====================================================
# ГЛАВНОЕ МЕНЮ И НАВИГАЦИЯ
# ====================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    is_new = user_id not in user_data
    u = get_user_info(user_id)
    u["name"] = message.from_user.full_name[:64]
    u["blocked"] = False  # вернулся — снова доступен для сообщений
    u["username"] = message.from_user.username or ""
    save_data()

    args = message.text.split()
    payload = args[1] if len(args) > 1 else ""

    # источник трафика — фиксируем один раз, при первом запуске
    if is_new:
        if payload.startswith("case_"):
            u["source"] = "promo_channel"
        elif payload.startswith("src_"):
            u["source"] = payload[:40]  # трекинговая ссылка рекламной кампании
        elif payload.isdigit():
            u["source"] = "referral"
        track_new_user()
        save_data()
        save_analytics()

    # переход по кнопке из промо-канала: /start case_12 → сразу открываем разбор
    if payload.startswith("case_"):
        text, markup = main_menu(message.from_user)
        await message.answer(text, reply_markup=markup)
        c = find_case(int(payload.removeprefix("case_"))) if payload.removeprefix("case_").isdigit() else None
        if c and not c.get("queued"):
            await show_case(message, c)
        return

    if is_new and payload.isdigit():
        try:
            referrer_id = int(payload)
            if referrer_id != user_id and referrer_id in user_data:
                u["referred_by"] = referrer_id
                ref_user = user_data[referrer_id]
                ref_user["attempts"] += 5
                ref_user["ref_count"] += 1
                save_data()
                await bot.send_message(referrer_id, "🎉 <b>Новый реферал!</b> Вам начислено <b>+5 генераций</b>!")
        except Exception as e:
            logging.error(f"Ref error: {e}")

    text, markup = main_menu(message.from_user)
    await message.answer(text, reply_markup=markup)


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = main_menu(message.from_user)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = main_menu(callback.from_user)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "create")
async def create_menu(callback: types.CallbackQuery):
    text = f"✍️ <b>Что создаём?</b>\n{LINE}\n\n"
    for name, desc in CATEGORIES.values():
        text += f"{name}\n<i>{desc}</i>\n\n"
    buttons = [btn(name, f"cat_{code}") for code, (name, _) in CATEGORIES.items()]
    rows = grid(buttons, 2) + [back_main_row()]
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("cat_"))
async def category_menu(callback: types.CallbackQuery):
    cat = callback.data.removeprefix("cat_")
    if cat not in CATEGORIES:
        await callback.answer()
        return
    u = get_user_info(callback.from_user.id)
    sub = is_sub_active(callback.from_user.id, u)
    name, _ = CATEGORIES[cat]

    text = f"{name}\n{LINE}\n\n"
    buttons = []
    for key, tool in TOOLS.items():
        if tool["cat"] != cat:
            continue
        lock = " 🔒" if tool["pro"] and not sub else ""
        text += f"{tool['name']}{lock} — <i>{tool['desc']}</i>\n"
        buttons.append(btn(tool["name"] + lock, f"tool_{key}"))

    rows = grid(buttons, 2) + [[btn("◀️ Категории", "create"), btn("🏠 Меню", "back_to_main")]]
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("tool_"))
async def select_tool(callback: types.CallbackQuery):
    key = callback.data.removeprefix("tool_")
    if key not in TOOLS:
        await callback.answer()
        return
    user_id = callback.from_user.id
    u = get_user_info(user_id)
    tool = TOOLS[key]

    if tool["pro"] and not is_sub_active(user_id, u):
        text, markup = pro_upsell(tool["name"])
        await safe_edit(callback.message, text, markup)
        await callback.answer()
        return

    u["tool"] = key
    save_data()
    back = f"cat_{tool['cat']}" if tool["cat"] else "back_to_main"
    text = (
        f"✅ <b>{tool['name']}</b>\n<i>{tool['desc']}</i>\n{LINE}\n\n"
        f"📝 {tool['hint']}.\n\n"
        "Можно текстом или 🎙 голосовым."
    )
    await safe_edit(callback.message, text, kb([btn("◀️ Назад", back), btn("🏠 Меню", "back_to_main")]))
    await callback.answer("Инструмент выбран ✅")


# ====================================================
# МОЙ БРЕНД (персонализация — главная ценность для платящих)
# ====================================================
class BrandSetup(StatesGroup):
    niche = State()
    audience = State()
    tone = State()
    samples = State()  # примеры постов автора для копирования стиля


MAX_SAMPLES = 3


CANCEL_KB = kb([btn("✖️ Отмена", "back_to_main")])


@dp.callback_query(F.data == "brand")
async def brand_menu(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    b = u["brand"]
    if not b:
        text = (
            f"🎯 <b>Мой бренд</b>\n{LINE}\n\n"
            "Расскажите о себе один раз — и каждый текст будет написан под вашу нишу, "
            "аудиторию и стиль. Без этого ИИ пишет «в целом», с этим — как ваш личный SMM-щик."
        )
        markup = kb([btn("⚡️ Настроить за 30 сек", "brand_setup")], back_main_row())
    else:
        text = (
            f"🎯 <b>Мой бренд</b>\n{LINE}\n\n"
            f"🏷 Ниша: <b>{esc(b.get('niche'))}</b>\n"
            f"👥 Аудитория: <b>{esc(b.get('audience'))}</b>\n"
            f"🎙 Тон: <b>{TONES.get(b.get('tone'), '—')}</b>\n"
            f"📚 Примеров вашего стиля: <b>{len(b.get('samples', []))}/{MAX_SAMPLES}</b>\n\n"
            "✅ Все тексты учитывают эти данные."
        )
        if not b.get("samples"):
            text += "\n\n💡 <i>Пришлите 1–3 своих поста — и бот будет писать вашим голосом, а не «как ИИ».</i>"
        markup = kb(
            [btn("📚 Обучить на моих постах", "brand_samples")],
            [btn("✏️ Изменить", "brand_setup"), btn("🗑 Сбросить", "brand_reset")],
            back_main_row(),
        )
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "brand_setup")
async def brand_setup_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BrandSetup.niche)
    await safe_edit(
        callback.message,
        "🎯 <b>Шаг 1 из 3 — Ниша</b>\n\nЧем вы занимаетесь?\n<i>Например: «маникюр в Киеве», «онлайн-курсы английского», «кофейня»</i>",
        CANCEL_KB,
    )
    await callback.answer()


@dp.message(Command("brand"))
async def brand_setup_cmd(message: types.Message, state: FSMContext):
    await state.set_state(BrandSetup.niche)
    await message.answer(
        "🎯 <b>Шаг 1 из 3 — Ниша</b>\n\nЧем вы занимаетесь?\n<i>Например: «маникюр в Киеве», «онлайн-курсы английского», «кофейня»</i>",
        reply_markup=CANCEL_KB,
    )


@dp.message(BrandSetup.niche, F.text)
async def brand_niche(message: types.Message, state: FSMContext):
    await state.update_data(niche=message.text[:150])
    await state.set_state(BrandSetup.audience)
    await message.answer(
        "👥 <b>Шаг 2 из 3 — Аудитория</b>\n\nКто ваши клиенты?\n<i>Например: «девушки 25–40, ценят качество»</i>",
        reply_markup=CANCEL_KB,
    )


@dp.message(BrandSetup.audience, F.text)
async def brand_audience(message: types.Message, state: FSMContext):
    await state.update_data(audience=message.text[:150])
    await state.set_state(BrandSetup.tone)
    buttons = [btn(name, f"tone_{code}") for code, name in TONES.items()]
    await message.answer(
        "🎙 <b>Шаг 3 из 3 — Тон</b>\n\nКак должен звучать ваш бренд?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=grid(buttons, 2) + [[btn("✖️ Отмена", "back_to_main")]]),
    )


@dp.callback_query(BrandSetup.tone, F.data.startswith("tone_"))
async def brand_tone(callback: types.CallbackQuery, state: FSMContext):
    tone = callback.data.removeprefix("tone_")
    data = await state.get_data()
    await state.clear()
    u = get_user_info(callback.from_user.id)
    old_samples = u["brand"].get("samples", []) if u["brand"] else []
    u["brand"] = {
        "niche": data.get("niche", ""), "audience": data.get("audience", ""),
        "tone": tone, "samples": old_samples,
    }
    save_data()
    await callback.answer("Бренд сохранён ✅")
    text, markup = main_menu(callback.from_user)
    await safe_edit(callback.message, "🎉 <b>Готово! Бренд сохранён.</b>\n\n" + text, markup)


@dp.callback_query(F.data == "brand_samples")
async def brand_samples_start(callback: types.CallbackQuery, state: FSMContext):
    u = get_user_info(callback.from_user.id)
    if not u["brand"]:
        await callback.answer("Сначала настройте бренд 🙂", show_alert=True)
        return
    u["brand"]["samples"] = []
    save_data()
    await state.set_state(BrandSetup.samples)
    await safe_edit(
        callback.message,
        f"📚 <b>Обучение на ваших постах</b>\n{LINE}\n\n"
        f"Пришлите до {MAX_SAMPLES} своих лучших постов — по одному сообщению.\n"
        "Можно просто переслать их из своего канала.\n\n"
        "Бот запомнит вашу лексику, ритм и манеру и будет писать так же.",
        kb([btn("✖️ Отмена", "brand")]),
    )
    await callback.answer()


@dp.message(BrandSetup.samples, F.text | F.caption)
async def brand_sample_add(message: types.Message, state: FSMContext):
    u = get_user_info(message.from_user.id)
    samples = u["brand"].setdefault("samples", [])
    samples.append((message.text or message.caption)[:1500])
    save_data()
    if len(samples) >= MAX_SAMPLES:
        await state.clear()
        await message.answer(
            "✅ <b>Готово! Бот изучил ваш стиль.</b>\n\nТеперь все тексты будут звучать как вы.",
            reply_markup=kb([btn("✍️ Создать контент", "create")], back_main_row()),
        )
        return
    await message.answer(
        f"👍 Пример {len(samples)}/{MAX_SAMPLES} сохранён. Пришлите ещё или завершите.",
        reply_markup=kb([btn("✅ Готово", "brand_samples_done")]),
    )


@dp.callback_query(BrandSetup.samples, F.data == "brand_samples_done")
async def brand_samples_done(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        callback.message,
        "✅ <b>Стиль сохранён!</b> Теперь тексты будут звучать как вы.",
        kb([btn("✍️ Создать контент", "create")], back_main_row()),
    )
    await callback.answer()


@dp.callback_query(F.data == "brand_reset")
async def brand_reset(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    u["brand"] = {}
    save_data()
    await callback.answer("Бренд сброшен")
    text, markup = main_menu(callback.from_user)
    await safe_edit(callback.message, text, markup)


# ====================================================
# ПРОФИЛЬ И РЕФЕРАЛЫ
# ====================================================
async def profile_screen(user: types.User) -> tuple[str, InlineKeyboardMarkup]:
    u = get_user_info(user.id)
    b = u["brand"]
    text = (
        f"👤 <b>Профиль</b>\n{LINE}\n\n"
        f"{status_line(user.id, u)}\n"
        f"📊 Создано материалов: <b>{u['total_gens']}</b>\n"
        f"🧩 Решено кейсов: <b>{len(u['cases_solved'])}</b>\n"
        f"👥 Приглашено друзей: <b>{u['ref_count']}</b>\n"
        f"🎯 Бренд: <b>{esc(b.get('niche')) if b else 'не настроен'}</b>\n"
    )
    return text, kb(
        [btn("💎 Улучшить до PRO", "buy_menu")],
        [btn("🎯 Мой бренд", "brand"), btn("🎁 +Генерации", "ref_menu")],
        back_main_row(),
    )


@dp.callback_query(F.data == "profile")
async def profile_cb(callback: types.CallbackQuery):
    text, markup = await profile_screen(callback.from_user)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.message(Command("profile"))
async def profile_cmd(message: types.Message):
    text, markup = await profile_screen(message.from_user)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "ref_menu")
async def ref_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    u = get_user_info(user_id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={user_id}"
    text = (
        f"🎁 <b>Бесплатные генерации</b>\n{LINE}\n\n"
        "Приглашайте коллег — за каждого друга <b>+5 генераций</b>.\n\n"
        f"Ваша ссылка (нажмите, чтобы скопировать):\n<code>{link}</code>\n\n"
        f"👥 Приглашено: <b>{u['ref_count']}</b>"
    )
    share = InlineKeyboardButton(
        text="📤 Отправить другу",
        url=f"https://t.me/share/url?url={link}&text=AI-маркетолог, который пишет посты и сценарии за секунды",
    )
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=[[share], back_main_row()]))
    await callback.answer()


# ====================================================
# ЯЗЫК
# ====================================================
@dp.callback_query(F.data == "lang_menu")
async def lang_menu(callback: types.CallbackQuery):
    await safe_edit(
        callback.message,
        "🌐 <b>Мова / Язык / Language</b>\n\nНа этом языке будет меню и ответы ИИ:",
        kb(
            [btn("🇺🇦 Українська", "set_lang_uk"), btn("🇷🇺 Русский", "set_lang_ru")],
            [btn("🇬🇧 English", "set_lang_en")],
            back_main_row(),
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_lang_"))
async def set_language(callback: types.CallbackQuery):
    lang = callback.data.removeprefix("set_lang_")
    if lang in UI:
        u = get_user_info(callback.from_user.id)
        u["lang"] = lang
        save_data()
    text, markup = main_menu(callback.from_user)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


# ====================================================
# SMM АКАДЕМИЯ
# ====================================================
@dp.callback_query(F.data == "academy_menu")
async def academy_menu(callback: types.CallbackQuery):
    await safe_edit(
        callback.message,
        f"🎓 <b>SMM Академия</b>\n{LINE}\n\nУчитесь и сразу применяйте знания в боте.",
        kb(
            [btn("📘 Уроки", "course_menu"), btn("🧪 Тесты", "test_menu")],
            [btn("📝 Сдать ДЗ на проверку", "tool_audit")],
            back_main_row(),
        ),
    )
    await callback.answer()


LESSONS = {
    "lesson_1": ("📘 Вирусность", "📘 <b>УРОК 1: Алгоритмы и вирусный контент</b>\n\n• <b>Хук (0–3 сек):</b> визуал и заголовок должны зацепить глаз.\n• <b>Польза:</b> 3 чётких решения без «воды».\n• <b>CTA:</b> просите написать кодовое слово в директ."),
    "lesson_2": ("🤖 Промптинг", "🤖 <b>УРОК 2: Работа с AI</b>\n\nФормула промпта: <b>[Ниша] + [ЦА] + [Проблема] + [Формат]</b>.\n\n💡 Настройте «Мой бренд» — и бот подставит нишу и ЦА автоматически."),
    "lesson_3": ("💰 Клиенты", "💰 <b>УРОК 3: Первые клиенты за 3 дня</b>\n\n1. Найдите 5 бизнесов.\n2. Сделайте бесплатный аудит через <b>AI-консультанта</b>.\n3. Предложите ведение за $150–300/мес."),
    "lesson_4": ("🚀 Воронки", "🚀 <b>УРОК 4: Авто-воронки</b>\n\nКарусель с обещанием полезного файла за комментарий + авто-ответ в директ = поток заявок."),
}


@dp.callback_query(F.data == "course_menu")
async def course_menu(callback: types.CallbackQuery):
    buttons = [btn(title, key) for key, (title, _) in LESSONS.items()]
    rows = grid(buttons, 2) + [[btn("◀️ Академия", "academy_menu")]]
    await safe_edit(callback.message, "📚 <b>Уроки</b>\n\nВыберите тему:", InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.in_(set(LESSONS)))
async def show_lesson(callback: types.CallbackQuery):
    await safe_edit(
        callback.message,
        LESSONS[callback.data][1],
        kb([btn("✍️ Применить в боте", "create")], [btn("◀️ К урокам", "course_menu")]),
    )
    await callback.answer()


QUIZZES = {
    "quiz_1": ("🎯 Хуки", "Какая главная задача первых 3 секунд Reels?",
               ["Поздороваться и представиться", "Вызвать эмоцию или показать проблему", "Сразу предложить купить"], 1),
    "quiz_2": ("🧠 Офферы", "Какой оффер лучше для холодного клиента?",
               ["«Купите курс за $500»", "«Качественный SMM для всех»", "«Бесплатный аудит + план роста охватов»"], 2),
    "quiz_3": ("📊 Форматы", "Что лучше удерживает внимание?",
               ["Длинные посты без визуала", "Карусели со структурой и динамичные Reels", "Фото со стока без текста"], 1),
}


@dp.callback_query(F.data == "test_menu")
async def test_menu(callback: types.CallbackQuery):
    buttons = [btn(title, key) for key, (title, *_) in QUIZZES.items()]
    rows = grid(buttons, 3) + [[btn("◀️ Академия", "academy_menu")]]
    await safe_edit(callback.message, "🧪 <b>Тесты</b>\n\nВыберите тему:", InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.in_(set(QUIZZES)))
async def show_quiz(callback: types.CallbackQuery):
    _, question, options, right = QUIZZES[callback.data]
    text = f"❓ <b>{question}</b>\n\n" + "\n".join(f"{'ABC'[i]}) {o}" for i, o in enumerate(options))
    buttons = [btn("ABC"[i], f"ans_{callback.data}_{i}") for i in range(len(options))]
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=[buttons]))
    await callback.answer()


@dp.callback_query(F.data.startswith("ans_"))
async def quiz_answer(callback: types.CallbackQuery):
    # формат: ans_quiz_1_2  →  quiz="quiz_1", idx=2
    quiz, idx = callback.data.removeprefix("ans_").rsplit("_", 1)
    idx = int(idx)
    if quiz not in QUIZZES:
        await callback.answer()
        return
    right = QUIZZES[quiz][3]
    if idx == right:
        text = "🎉 <b>Верно!</b>\n\nВы понимаете современные механики. Примените знания на практике 👇"
    else:
        text = f"❌ <b>Не совсем.</b> Правильный ответ: <b>{'ABC'[right]}</b>.\n\nРешают первые секунды внимания и попадание в боль клиента."
    await safe_edit(
        callback.message, text,
        kb([btn("✍️ Создать контент", "create")], [btn("🧪 Другие тесты", "test_menu")]),
    )
    await callback.answer()


# ====================================================
# ОПЛАТА (Telegram Stars)
# ====================================================
BUY_TEXT = (
    f"💎 <b>PRO-доступ</b>\n{LINE}\n\n"
    "♾ Безлимитные генерации\n"
    "📅 Контент-план на 30 дней\n"
    "💰 Воронки продаж с текстами\n"
    "🏛 AI-консультант уровня CMO\n"
    "⏰ Отложенный постинг в ваш канал\n"
    "🚨 Антикризисные посты\n"
    "🎙 Голосовой ввод\n\n"
    "<b>Тарифы:</b>\n"
    "📦 10 генераций — <b>50 ⭐</b>\n"
    "💎 PRO месяц — <s>700</s> <b>500 ⭐</b>\n"
    "👑 VIP год — <s>3500</s> <b>1000 ⭐</b> · выгода 70%\n\n"
    "<i>Оплата Telegram Stars — безопасно, прямо в Telegram.</i>"
)


def buy_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [btn("👑 VIP на год — 1000 ⭐ 🔥", "buy_sub_year")],
        [btn("💎 PRO месяц — 500 ⭐", "buy_sub_month")],
        [btn("📦 10 генераций — 50 ⭐", "buy_pack_50")],
        back_main_row(),
    )


@dp.message(Command("buy"))
async def buy_command(message: types.Message):
    await message.answer(BUY_TEXT, reply_markup=buy_keyboard())


@dp.callback_query(F.data == "buy_menu")
async def send_buy_menu(callback: types.CallbackQuery):
    await safe_edit(callback.message, BUY_TEXT, buy_keyboard())
    await callback.answer()


PRODUCTS = {
    "buy_pack_50": ("pack_50", "📦 10 генераций", "Пополнение баланса на 10 генераций.", 50),
    "buy_pack_30": ("pack_30", "🎁 Акция: 10 генераций", "Пополнение баланса по акции.", 30),
    "buy_sub_month": ("sub_month", "💎 PRO на 30 дней", "Безлимит и все PRO-инструменты.", 500),
    "buy_sub_year": ("sub_year", "👑 VIP на 365 дней", "Безлимит и все PRO-инструменты на год.", 1000),
}


@dp.callback_query(F.data.in_(set(PRODUCTS)))
async def send_invoice(callback: types.CallbackQuery):
    payload, title, description, stars = PRODUCTS[callback.data]
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def process_pay(message: types.Message):
    u = get_user_info(message.from_user.id)
    payment = message.successful_payment
    payload = payment.invoice_payload
    logging.info(
        f"PAYMENT user={message.from_user.id} payload={payload} "
        f"stars={payment.total_amount} charge_id={payment.telegram_payment_charge_id}"
    )
    u["paid_stars"] = u.get("paid_stars", 0) + payment.total_amount
    track_payment(message.from_user.id, payload, payment.total_amount)
    if payload in ("pack_50", "pack_30"):
        u["attempts"] += 10
        text = "🎉 <b>Оплата прошла!</b> +10 генераций на балансе."
    elif payload in ("sub_month", "sub_year"):
        days = 30 if payload == "sub_month" else 365
        start = u["sub_until"] if u["sub_until"] and u["sub_until"] > datetime.now() else datetime.now()
        u["sub_until"] = start + timedelta(days=days)
        text = f"👑 <b>PRO активирован до {u['sub_until']:%d.%m.%Y}!</b>\n\nВсе инструменты открыты 🔓"
    else:
        text = "✅ Оплата получена."
    save_data()
    await message.answer(text, reply_markup=kb([btn("✍️ Создать контент", "create")]))
    try:
        await bot.send_message(
            ADMIN_TELEGRAM_ID,
            f"💰 Оплата: {payment.total_amount} ⭐ · {payload} · <code>{message.from_user.id}</code>",
        )
    except Exception:
        pass


# ====================================================
# /id — пользователь узнаёт свой ID, чтобы отправить его владельцу
# ====================================================
@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(
        f"🆔 Ваш ID: <code>{message.from_user.id}</code>\n\n<i>Нажмите на число, чтобы скопировать.</i>"
    )


# ====================================================
# АДМИН-ПАНЕЛЬ (только для владельца)
# ====================================================
IsAdmin = F.from_user.id == ADMIN_TELEGRAM_ID


class AdminFlow(StatesGroup):
    grant_id = State()
    revoke_id = State()
    reply = State()  # ответ на заявку о сотрудничестве


GIFT_PERIODS = {
    "7": ("7 дней", 7),
    "30": ("30 дней", 30),
    "365": ("1 год", 365),
    "forever": ("навсегда", None),
}


def admin_panel_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("🎁 Выдать доступ", "adm_grant"), btn("⛔️ Забрать доступ", "adm_revoke")],
        [btn("📋 Кому выдан доступ", "adm_list"), btn("📊 Статистика", "adm_stats")],
        [btn("🔥 Разборы постов (кейсы)", "adm_cases")],
        [btn("📣 Рекламная рассылка", "adm_bc")],
        back_main_row(),
    )


ADMIN_TEXT = (
    f"🛠 <b>Админ-панель</b>\n{LINE}\n\n"
    "Выдавайте друзьям бесплатный PRO по их ID.\n"
    "Друг узнаёт свой ID командой /id в боте.\n\n"
    "<b>Быстрые команды:</b>\n"
    "<code>/give 123456789</code> — доступ навсегда\n"
    "<code>/give 123456789 30</code> — на 30 дней\n"
    "<code>/remove 123456789</code> — забрать доступ"
)


def parse_user_id(text: str) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() and 5 <= len(text) <= 15 else None


def grant_access(target_id: int, period_key: str) -> str:
    u = get_user_info(target_id)
    label, days = GIFT_PERIODS[period_key]
    if days is None:
        u["vip"] = True
    else:
        start = u["gift_until"] if gift_active(u) else datetime.now()
        u["gift_until"] = start + timedelta(days=days)
        label = f"до {u['gift_until']:%d.%m.%Y}"
    save_data()
    return label


def revoke_access(target_id: int) -> bool:
    """Забирает только подаренный доступ. Оплаченную подписку не трогает."""
    if target_id not in user_data:
        return False
    u = user_data[target_id]
    had = bool(u.get("vip") or gift_active(u))
    u["vip"] = False
    u["gift_until"] = None
    save_data()
    return had


async def notify_gift(target_id: int, label: str) -> bool:
    try:
        await bot.send_message(
            target_id,
            f"🎁 <b>Вам подарен PRO-доступ {label}!</b>\n\nВсе инструменты открыты, генерации без ограничений 🔓",
            reply_markup=kb([btn("✍️ Начать", "create")]),
        )
        return True
    except Exception:
        return False


async def do_grant(target_id: int, period_key: str) -> str:
    if target_id == ADMIN_TELEGRAM_ID:
        return "😄 У вас и так вечный VIP."
    label = grant_access(target_id, period_key)
    name = esc(user_data[target_id].get("name")) or "без имени"
    notified = await notify_gift(target_id, label)
    text = f"✅ Доступ выдан: <code>{target_id}</code> ({name}) — <b>{label}</b>"
    if not notified:
        text += "\n\n⚠️ Пользователь ещё не запускал бота. Доступ включится, как только он нажмёт /start."
    return text


def do_revoke(target_id: int) -> str:
    if target_id == ADMIN_TELEGRAM_ID:
        return "⛔️ Владельца удалить нельзя."
    if revoke_access(target_id):
        return f"⛔️ Подаренный доступ забран у <code>{target_id}</code>."
    return f"ℹ️ У <code>{target_id}</code> не было подаренного доступа."


@dp.message(Command("admin"), IsAdmin)
async def admin_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(ADMIN_TEXT, reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_panel", IsAdmin)
async def admin_panel_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, ADMIN_TEXT, admin_panel_kb())
    await callback.answer()


# --- выдать доступ через кнопки ---
@dp.callback_query(F.data == "adm_grant", IsAdmin)
async def admin_grant_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.grant_id)
    await safe_edit(
        callback.message,
        "🎁 <b>Выдать доступ</b>\n\nОтправьте ID пользователя одним сообщением.\n<i>Друг может узнать свой ID командой /id</i>",
        kb([btn("✖️ Отмена", "adm_panel")]),
    )
    await callback.answer()


@dp.message(AdminFlow.grant_id, IsAdmin, F.text)
async def admin_grant_id(message: types.Message, state: FSMContext):
    target_id = parse_user_id(message.text)
    if not target_id:
        await message.answer("❌ Это не похоже на ID. Отправьте только цифры, например <code>123456789</code>.")
        return
    await state.clear()
    name = esc(user_data.get(target_id, {}).get("name")) or "ещё не заходил в бота"
    buttons = [btn(label.capitalize(), f"adm_give_{target_id}_{key}") for key, (label, _) in GIFT_PERIODS.items()]
    await message.answer(
        f"👤 <code>{target_id}</code> ({name})\n\nНа какой срок выдать PRO?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=grid(buttons, 2) + [[btn("✖️ Отмена", "adm_panel")]]),
    )


@dp.callback_query(F.data.startswith("adm_give_"), IsAdmin)
async def admin_give(callback: types.CallbackQuery):
    target, period = callback.data.removeprefix("adm_give_").split("_", 1)
    text = await do_grant(int(target), period)
    await safe_edit(callback.message, text, kb([btn("🛠 Админ-панель", "adm_panel")]))
    await callback.answer()


# --- забрать доступ через кнопки ---
@dp.callback_query(F.data == "adm_revoke", IsAdmin)
async def admin_revoke_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.revoke_id)
    await safe_edit(
        callback.message,
        "⛔️ <b>Забрать доступ</b>\n\nОтправьте ID пользователя.\n<i>Оплаченная подписка при этом не пропадёт — только подарок.</i>",
        kb([btn("📋 Выбрать из списка", "adm_list")], [btn("✖️ Отмена", "adm_panel")]),
    )
    await callback.answer()


@dp.message(AdminFlow.revoke_id, IsAdmin, F.text)
async def admin_revoke_id(message: types.Message, state: FSMContext):
    target_id = parse_user_id(message.text)
    if not target_id:
        await message.answer("❌ Отправьте только цифры ID.")
        return
    await state.clear()
    await message.answer(do_revoke(target_id), reply_markup=kb([btn("🛠 Админ-панель", "adm_panel")]))


# --- список подаренных доступов ---
def admin_list_view() -> tuple[str, InlineKeyboardMarkup]:
    gifted = [
        (uid, u) for uid, u in user_data.items()
        if uid != ADMIN_TELEGRAM_ID and (u.get("vip") or gift_active(u))
    ]
    if not gifted:
        return "📋 Сейчас ни у кого нет подаренного доступа.", kb([btn("◀️ Назад", "adm_panel")])

    text = f"📋 <b>Подаренный доступ ({len(gifted)})</b>\n{LINE}\n\n"
    rows = []
    for uid, u in gifted[:30]:
        term = "навсегда" if u.get("vip") else f"до {u['gift_until']:%d.%m.%Y}"
        text += f"• <code>{uid}</code> {esc(u.get('name'))} — {term}\n"
        rows.append([btn(f"⛔️ Забрать у {u.get('name') or uid}"[:40], f"adm_rm_{uid}")])
    rows.append([btn("◀️ Назад", "adm_panel")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_list", IsAdmin)
async def admin_list(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = admin_list_view()
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_rm_"), IsAdmin)
async def admin_remove_from_list(callback: types.CallbackQuery):
    revoke_access(int(callback.data.removeprefix("adm_rm_")))
    text, markup = admin_list_view()
    await safe_edit(callback.message, text, markup)
    await callback.answer("Доступ забран ⛔️")


# --- 📊 профессиональная аналитика ---
def last_days(n: int) -> list[str]:
    today = datetime.now(TZ).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def active_in(days: list[str]) -> set:
    result = set()
    for d in days:
        result |= analytics["days"].get(d, {}).get("active", set())
    return result


def day_sum(field: str, days: list[str]) -> int:
    return sum(analytics["days"].get(d, {}).get(field, 0) for d in days)


def sparkline(values: list[int]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    top = max(values) or 1
    return "".join(bars[min(7, round(v / top * 7))] if v else "▁" for v in values)


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


def analytics_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("📈 Рост и активность", "an_growth"), btn("💰 Деньги", "an_money")],
        [btn("🔻 Воронка продаж", "an_funnel"), btn("🛠 Инструменты", "an_tools")],
        [btn("🔗 Источники трафика", "an_sources"), btn("🚫 Блокировки", "adm_blocked")],
        [btn("📥 Выгрузить базу (Excel)", "an_export")],
        [btn("🔄 Обновить", "adm_stats"), btn("◀️ Админ-панель", "adm_panel")],
    )


BACK_AN = kb([btn("◀️ Аналитика", "adm_stats")])


@dp.callback_query(F.data == "adm_stats", IsAdmin)
async def admin_stats(callback: types.CallbackQuery):
    users = list(user_data.values())
    total = len(users)
    d1, d7, d30 = last_days(1), last_days(7), last_days(30)
    dau, wau, mau = len(active_in(d1)), len(active_in(d7)), len(active_in(d30))
    payers = sum(1 for u in users if u.get("paid_stars"))
    paid_now = sum(1 for u in users if u.get("sub_until") and u["sub_until"] > datetime.now())
    blocked = sum(1 for u in users if u.get("blocked"))
    text = (
        f"📊 <b>Аналитика</b> · {datetime.now(TZ):%d.%m.%Y %H:%M}\n{LINE}\n\n"
        f"👥 <b>Пользователи:</b> {total}\n"
        f"   🆕 сегодня +{day_sum('new', d1)} · 7 дн +{day_sum('new', d7)} · 30 дн +{day_sum('new', d30)}\n\n"
        f"🔥 <b>Активность</b>\n"
        f"   DAU {dau} · WAU {wau} · MAU {mau}\n"
        f"   Вовлечённость (DAU/MAU): {pct(dau, mau)}\n\n"
        f"💰 <b>Выручка</b>\n"
        f"   сегодня {day_sum('stars', d1)} ⭐ · 7 дн {day_sum('stars', d7)} ⭐ · 30 дн {day_sum('stars', d30)} ⭐\n"
        f"   всего {sum(u.get('paid_stars', 0) for u in users)} ⭐\n\n"
        f"💎 <b>Платящие:</b> {payers} ({pct(payers, total)} от всех)\n"
        f"   активных PRO сейчас: {paid_now}\n\n"
        f"✍️ Генераций: сегодня {day_sum('gens', d1)} · 7 дн {day_sum('gens', d7)}\n"
        f"🚫 Заблокировали: {blocked} ({pct(blocked, total)})"
    )
    await safe_edit(callback.message, text, analytics_kb())
    await callback.answer()


@dp.callback_query(F.data == "an_growth", IsAdmin)
async def an_growth(callback: types.CallbackQuery):
    days = last_days(14)
    new = [analytics["days"].get(d, {}).get("new", 0) for d in days]
    act = [len(analytics["days"].get(d, {}).get("active", set())) for d in days]
    gens = [analytics["days"].get(d, {}).get("gens", 0) for d in days]

    # удержание: из пришедших 7–30 дней назад — кто заходил за последние 7 дней
    today = datetime.now(TZ).date()

    def joined_date(u: dict):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):  # второй формат — от старой версии бота
            try:
                return datetime.strptime(u.get("joined", ""), fmt).date()
            except ValueError:
                pass
        return today

    cohort = [u for u in user_data.values() if 7 <= (today - joined_date(u)).days <= 30]
    week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    retained = sum(1 for u in cohort if u.get("last_seen", "") >= week_ago)

    rows = "\n".join(
        f"<code>{d[8:]}.{d[5:7]}</code>  🆕{n:>3}  🔥{a:>4}  ✍️{g:>4}"
        for d, n, a, g in list(zip(days, new, act, gens))[-7:]
    )
    text = (
        f"📈 <b>Рост и активность · 14 дней</b>\n{LINE}\n\n"
        f"🆕 Новые:    {sparkline(new)}  ({sum(new)})\n"
        f"🔥 Активные: {sparkline(act)}\n"
        f"✍️ Генерации: {sparkline(gens)}  ({sum(gens)})\n\n"
        f"<b>Последние 7 дней:</b>\n{rows}\n\n"
        f"🔁 <b>Удержание</b> (пришли 7–30 дн назад, заходили на этой неделе):\n"
        f"   {retained} из {len(cohort)} — <b>{pct(retained, len(cohort))}</b>"
    )
    await safe_edit(callback.message, text, BACK_AN)
    await callback.answer()


PAYLOAD_NAMES = {
    "pack_50": "📦 Пакет 50 ⭐", "pack_30": "🎁 Акция 30 ⭐",
    "sub_month": "💎 PRO месяц", "sub_year": "👑 VIP год",
}


@dp.callback_query(F.data == "an_money", IsAdmin)
async def an_money(callback: types.CallbackQuery):
    pays = analytics["payments"]
    days = last_days(14)
    stars = [analytics["days"].get(d, {}).get("stars", 0) for d in days]
    by_product: dict[str, list[int]] = {}
    for p in pays:
        by_product.setdefault(p["payload"], []).append(p["stars"])
    products = "\n".join(
        f"   {PAYLOAD_NAMES.get(k, k)}: {len(v)} шт · {sum(v)} ⭐"
        for k, v in sorted(by_product.items(), key=lambda x: -sum(x[1]))
    ) or "   пока нет оплат"
    payers = {p["user"] for p in pays}
    repeat = sum(1 for uid in payers if sum(1 for p in pays if p["user"] == uid) > 1)
    total_users = len(user_data) or 1
    revenue = sum(p["stars"] for p in pays)
    recent = "\n".join(
        f"   {p['date'][5:10].replace('-', '.')} {p['date'][11:]} · {PAYLOAD_NAMES.get(p['payload'], p['payload'])} · "
        f"{esc(user_data.get(p['user'], {}).get('name', '')) or p['user']}"
        for p in reversed(pays[-8:])
    ) or "   —"
    text = (
        f"💰 <b>Деньги</b>\n{LINE}\n\n"
        f"📊 Выручка за 14 дней: {sparkline(stars)}  ({sum(stars)} ⭐)\n\n"
        f"<b>По продуктам (всё время):</b>\n{products}\n\n"
        f"🧾 Оплат: <b>{len(pays)}</b> · платящих: <b>{len(payers)}</b>\n"
        f"🔁 Купили повторно: <b>{repeat}</b> ({pct(repeat, len(payers))})\n"
        f"📈 ARPU (на пользователя): <b>{revenue / total_users:.1f} ⭐</b>\n"
        f"💎 ARPPU (на платящего): <b>{revenue / len(payers) if payers else 0:.1f} ⭐</b>\n\n"
        f"<b>Последние оплаты:</b>\n{recent}"
    )
    await safe_edit(callback.message, text, BACK_AN)
    await callback.answer()


@dp.callback_query(F.data == "an_funnel", IsAdmin)
async def an_funnel(callback: types.CallbackQuery):
    users = list(user_data.values())
    steps = [
        ("👋 Запустили бота", len(users)),
        ("✍️ Первая генерация", sum(1 for u in users if u.get("total_gens", 0) >= 1)),
        ("🔥 3+ генерации", sum(1 for u in users if u.get("total_gens", 0) >= 3)),
        ("🎯 Настроили бренд", sum(1 for u in users if u.get("brand"))),
        ("🧪 Взяли пробный PRO", sum(1 for u in users if u.get("trial_used"))),
        ("💎 Заплатили", sum(1 for u in users if u.get("paid_stars"))),
    ]
    top = steps[0][1] or 1
    lines = []
    prev = None
    for name, value in steps:
        bar = "█" * max(1, round(value / top * 12)) if value else "·"
        step_conv = f" ({pct(value, prev)} от пред.)" if prev is not None else ""
        lines.append(f"{name}\n<code>{bar}</code> <b>{value}</b> · {pct(value, top)}{step_conv}")
        prev = value
    # где теряем больше всего
    drops = [(steps[i][0], steps[i - 1][1] - steps[i][1]) for i in range(1, len(steps))]
    worst = max(drops, key=lambda x: x[1]) if drops else None
    hint = f"\n\n⚠️ Больше всего теряем на шаге: <b>{worst[0]}</b> (−{worst[1]})" if worst and worst[1] else ""
    text = f"🔻 <b>Воронка продаж</b>\n{LINE}\n\n" + "\n\n".join(lines) + hint
    await safe_edit(callback.message, text, BACK_AN)
    await callback.answer()


@dp.callback_query(F.data == "an_tools", IsAdmin)
async def an_tools(callback: types.CallbackQuery):
    usage = sorted(analytics["tools"].items(), key=lambda x: -x[1])
    total = sum(v for _, v in usage) or 1
    names = {**{k: t["name"] for k, t in TOOLS.items()}, "case_practice": "🧩 Практика кейсов"}
    lines = "\n".join(
        f"{i}. {names.get(k, k)} — <b>{v}</b> ({pct(v, total)})" for i, (k, v) in enumerate(usage[:15], 1)
    ) or "Пока нет данных"
    unused = [t["name"] for k, t in TOOLS.items() if k not in analytics["tools"]]
    text = f"🛠 <b>Популярность инструментов</b>\n{LINE}\n\n{lines}"
    if unused and usage:
        text += "\n\n💤 <b>Не использовали ни разу:</b>\n" + ", ".join(unused)
    await safe_edit(callback.message, text, BACK_AN)
    await callback.answer()


SOURCE_NAMES = {"direct": "🔍 Напрямую", "referral": "👥 Рефералы", "promo_channel": "📢 Промо-канал"}


@dp.callback_query(F.data == "an_sources", IsAdmin)
async def an_sources(callback: types.CallbackQuery):
    stats: dict[str, dict] = {}
    for u in user_data.values():
        s = stats.setdefault(u.get("source", "direct"), {"users": 0, "payers": 0, "stars": 0})
        s["users"] += 1
        if u.get("paid_stars"):
            s["payers"] += 1
            s["stars"] += u["paid_stars"]
    lines = "\n".join(
        f"{SOURCE_NAMES.get(k, '🔗 ' + k.removeprefix('src_'))}: <b>{v['users']}</b> чел · "
        f"оплатили {v['payers']} ({pct(v['payers'], v['users'])}) · {v['stars']} ⭐"
        for k, v in sorted(stats.items(), key=lambda x: -x[1]["users"])
    )
    me = await bot.me()
    text = (
        f"🔗 <b>Источники трафика</b>\n{LINE}\n\n{lines}\n\n"
        f"<b>Как отследить рекламу:</b>\n"
        f"Давайте каждой площадке свою ссылку — бот покажет, сколько людей пришло и сколько заплатило:\n"
        f"<code>https://t.me/{me.username}?start=src_insta</code>\n"
        f"<code>https://t.me/{me.username}?start=src_blogger_anna</code>\n"
        f"<i>После src_ — любое название латиницей.</i>"
    )
    await safe_edit(callback.message, text, BACK_AN)
    await callback.answer()


@dp.callback_query(F.data == "an_export", IsAdmin)
async def an_export(callback: types.CallbackQuery):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "ID", "Имя", "Username", "Пришёл", "Последний визит", "Источник", "Статус",
        "Генераций", "Заплатил ⭐", "Бренд", "Язык", "Заблокировал",
    ])
    for uid, u in user_data.items():
        status = "VIP" if u.get("vip") else "PRO" if is_sub_active(uid, u) else "Free"
        writer.writerow([
            uid, u.get("name", ""), u.get("username", ""), u.get("joined", ""), u.get("last_seen", ""),
            u.get("source", ""), status, u.get("total_gens", 0), u.get("paid_stars", 0),
            (u.get("brand") or {}).get("niche", ""), u.get("lang", ""),
            u.get("blocked_at") or ("да" if u.get("blocked") else ""),
        ])
    data = buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel правильно показал кириллицу
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"users_{today_key()}.csv"),
        caption=f"📥 База пользователей: {len(user_data)} чел.",
    )
    await callback.answer()


# --- кто заблокировал бота ---
def mark_blocked(user_id: int):
    u = get_user_info(user_id)
    if not u["blocked"]:
        u["blocked"] = True
        u["blocked_at"] = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def on_user_blocked(event: types.ChatMemberUpdated):
    """Telegram сообщает боту сразу, как только пользователь его заблокировал."""
    if event.chat.type != "private":
        return
    mark_blocked(event.from_user.id)
    u = get_user_info(event.from_user.id)
    u["name"] = u["name"] or event.from_user.full_name[:64]
    u["username"] = event.from_user.username or u["username"]
    save_data()


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_user_unblocked(event: types.ChatMemberUpdated):
    # при первом запуске это событие приходит раньше /start — нового пользователя тут не создаём,
    # иначе /start не узнает, что он новый (потеряются реферал и источник трафика)
    if event.chat.type != "private" or event.from_user.id not in user_data:
        return
    user_data[event.from_user.id]["blocked"] = False
    save_data()


def _blocked_sort_key(item):
    try:
        return datetime.strptime(item[1].get("blocked_at", ""), "%d.%m.%Y %H:%M")
    except ValueError:
        return datetime.min


@dp.callback_query(F.data == "adm_blocked", IsAdmin)
async def admin_blocked_list(callback: types.CallbackQuery):
    blocked = sorted(
        ((uid, u) for uid, u in user_data.items() if u.get("blocked")),
        key=_blocked_sort_key, reverse=True,
    )
    if not blocked:
        await safe_edit(callback.message, "🎉 Никто не заблокировал бота.", kb([btn("◀️ Статистика", "adm_stats")]))
        await callback.answer()
        return

    zero_gens = sum(1 for _, u in blocked if not u.get("total_gens"))
    had_pro = sum(1 for _, u in blocked if u.get("sub_until") or u.get("trial_used"))
    text = (
        f"🚫 <b>Заблокировали бота: {len(blocked)}</b>\n{LINE}\n\n"
        f"📉 Ушли, не создав ни одного материала: <b>{zero_gens}</b>\n"
        f"💎 Были на PRO или пробном: <b>{had_pro}</b>\n\n"
        "<b>Последние:</b>\n"
    )
    for uid, u in blocked[:25]:
        username = f" @{esc(u['username'])}" if u.get("username") else ""
        text += (
            f"• {esc(u.get('name')) or 'Без имени'}{username} · <code>{uid}</code>\n"
            f"   🚫 {u.get('blocked_at') or 'дата неизвестна'} · ✍️ {u.get('total_gens', 0)} генераций\n"
        )
    if len(blocked) > 25:
        text += f"\n…и ещё {len(blocked) - 25}"
    await safe_edit(callback.message, text, kb([btn("◀️ Статистика", "adm_stats")]))
    await callback.answer()


# --- быстрые команды ---
@dp.message(Command("give"), IsAdmin)
async def admin_give_cmd(message: types.Message):
    parts = message.text.split()
    target_id = parse_user_id(parts[1]) if len(parts) > 1 else None
    period = parts[2] if len(parts) > 2 else "forever"
    if not target_id or period not in GIFT_PERIODS:
        await message.answer("Формат: <code>/give ID</code> или <code>/give ID 7|30|365</code>")
        return
    await message.answer(await do_grant(target_id, period))


@dp.message(Command("remove"), IsAdmin)
async def admin_remove_cmd(message: types.Message):
    parts = message.text.split()
    target_id = parse_user_id(parts[1]) if len(parts) > 1 else None
    if not target_id:
        await message.answer("Формат: <code>/remove ID</code>")
        return
    await message.answer(do_revoke(target_id))


# ====================================================
# СОТРУДНИЧЕСТВО (заявки приходят владельцу)
# ====================================================
PARTNER_TYPES = {
    "ads": ("📢 Реклама в боте", "Размещение вашего продукта или канала для нашей аудитории"),
    "affiliate": ("💸 Партнёрская программа", "Приводите клиентов и получайте % с их оплат"),
    "agency": ("🏢 Для агентств и команд", "Доступ для команды, оптовые тарифы, интеграции"),
    "other": ("💡 Другое предложение", "Коллаборации, взаимный пиар, идеи"),
}
PARTNER_COOLDOWN = timedelta(minutes=10)
partner_last_sent: dict[int, datetime] = {}  # защита от спама заявками


class PartnerFlow(StatesGroup):
    message = State()


def partner_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = f"🤝 <b>Сотрудничество</b>\n{LINE}\n\nОткрыты к партнёрству. Выберите формат:\n\n"
    for name, desc in PARTNER_TYPES.values():
        text += f"{name}\n<i>{desc}</i>\n\n"
    text += "Заявку рассмотрим лично и ответим прямо здесь, в боте."
    buttons = [btn(name, f"partner_{code}") for code, (name, _) in PARTNER_TYPES.items()]
    return text, InlineKeyboardMarkup(inline_keyboard=grid(buttons, 2) + [back_main_row()])


@dp.callback_query(F.data == "partner")
async def partner_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = partner_screen()
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.message(Command("partner"))
async def partner_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = partner_screen()
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("partner_"))
async def partner_choose(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.removeprefix("partner_")
    if code not in PARTNER_TYPES:
        await callback.answer()
        return
    last = partner_last_sent.get(callback.from_user.id)
    if last and datetime.now() - last < PARTNER_COOLDOWN:
        await callback.answer("Заявка уже отправлена — скоро ответим 🙌", show_alert=True)
        return
    await state.set_state(PartnerFlow.message)
    await state.update_data(partner_type=code)
    await safe_edit(
        callback.message,
        f"{PARTNER_TYPES[code][0]}\n{LINE}\n\n"
        "Опишите предложение одним сообщением:\n"
        "• кто вы и что за проект\n"
        "• что предлагаете\n"
        "• ссылка на канал / сайт\n"
        "• как с вами связаться (если не через этот чат)",
        kb([btn("✖️ Отмена", "partner")]),
    )
    await callback.answer()


@dp.message(PartnerFlow.message, F.text)
async def partner_submit(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    user = message.from_user
    type_name = PARTNER_TYPES.get(data.get("partner_type"), ("💡 Другое",))[0]
    partner_last_sent[user.id] = datetime.now()

    username = f"@{user.username}" if user.username else "нет username"
    admin_text = (
        f"🤝 <b>Новая заявка на сотрудничество</b>\n{LINE}\n\n"
        f"📌 Формат: <b>{type_name}</b>\n"
        f"👤 {esc(user.full_name)} · {esc(username)}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"💬 {esc(message.text[:3000])}"
    )
    rows = [[btn("✉️ Ответить через бота", f"adm_reply_{user.id}")]]
    if user.username:
        rows.append([InlineKeyboardButton(text="💬 Написать в личку", url=f"https://t.me/{user.username}")])
    try:
        await bot.send_message(ADMIN_TELEGRAM_ID, admin_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await message.answer(
            "✅ <b>Заявка отправлена!</b>\n\nМы ответим в этом чате в ближайшее время. Спасибо за интерес 🙌",
            reply_markup=kb(back_main_row()),
        )
    except Exception as e:
        logging.error(f"Partner request failed: {e}")
        partner_last_sent.pop(user.id, None)
        await message.answer("⚠️ Не удалось отправить заявку. Попробуйте позже.")


# --- владелец отвечает на заявку прямо через бота ---
@dp.callback_query(F.data.startswith("adm_reply_"), IsAdmin)
async def admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    target_id = int(callback.data.removeprefix("adm_reply_"))
    await state.set_state(AdminFlow.reply)
    await state.update_data(reply_to=target_id)
    await callback.message.answer(
        f"✉️ Напишите ответ для <code>{target_id}</code> — я перешлю его от имени бота.",
        reply_markup=kb([btn("✖️ Отмена", "adm_panel")]),
    )
    await callback.answer()


@dp.message(AdminFlow.reply, IsAdmin, F.text)
async def admin_reply_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    target_id = data.get("reply_to")
    try:
        await bot.send_message(
            target_id,
            f"📩 <b>Ответ от команды SMM Architecture AI</b>\n{LINE}\n\n{esc(message.text)}",
            reply_markup=kb([btn("🤝 Написать ещё", "partner")]),
        )
        await message.answer(f"✅ Ответ отправлен <code>{target_id}</code>.")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить: пользователь мог заблокировать бота.\n<code>{esc(str(e))}</code>")


# ====================================================
# ПРОБНЫЙ PRO
# ====================================================
@dp.callback_query(F.data == "trial")
async def activate_trial(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    if u["trial_used"]:
        await callback.answer("Пробный период уже был использован", show_alert=True)
        return
    u["trial_used"] = True
    u["trial_until"] = datetime.now() + timedelta(days=TRIAL_DAYS)
    save_data()
    await callback.answer("PRO активирован 🎉")
    await safe_edit(
        callback.message,
        f"🎉 <b>Пробный PRO на {TRIAL_DAYS} дня активирован!</b>\n{LINE}\n\n"
        "Открыто всё:\n"
        "• ♾ Безлимитные генерации\n"
        "• 📅 Контент-план на 30 дней\n"
        "• 💰 Воронки продаж и 🏛 AI-консультант\n"
        "• ⏰ Отложенный постинг в ваш канал\n\n"
        "💡 Начните с контент-плана — он задаст темы на весь месяц.",
        kb([btn("📅 Сделать контент-план", "tool_content_plan")], [btn("✍️ Все инструменты", "create")]),
    )


# ====================================================
# МОИ МАТЕРИАЛЫ (сохранённые результаты)
# ====================================================
MAX_SAVED = 30


@dp.callback_query(F.data == "save")
async def save_result(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    if not u["last_answer"]:
        await callback.answer("Нечего сохранять", show_alert=True)
        return
    if any(item["text"] == u["last_answer"] for item in u["saved"]):
        await callback.answer("Уже сохранено ⭐")
        return
    title = re.sub(r"[*#_`]", "", u["last_answer"]).strip().split("\n")[0][:40] or "Без названия"
    u["saved"].insert(0, {
        "title": title,
        "tool": u["tool"],
        "text": u["last_answer"],
        "date": datetime.now().strftime("%d.%m"),
    })
    del u["saved"][MAX_SAVED:]
    save_data()
    await callback.answer("Сохранено в «Мои материалы» ⭐")


def saved_list_view(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    if not u["saved"]:
        return (
            f"📂 <b>Мои материалы</b>\n{LINE}\n\nПока пусто. Нажмите «⭐ Сохранить» под любым результатом.",
            kb([btn("✍️ Создать контент", "create")], back_main_row()),
        )
    text = f"📂 <b>Мои материалы ({len(u['saved'])})</b>\n{LINE}\n\nНажмите, чтобы открыть:"
    rows = [
        [btn(f"{TOOLS.get(item['tool'], {}).get('name', '📄')[:2]} {item['date']} · {item['title']}"[:60], f"open_{i}")]
        for i, item in enumerate(u["saved"][:15])
    ]
    rows.append(back_main_row())
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "saved")
async def saved_list(callback: types.CallbackQuery):
    text, markup = saved_list_view(get_user_info(callback.from_user.id))
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("open_"))
async def open_saved(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    idx = int(callback.data.removeprefix("open_"))
    if idx >= len(u["saved"]):
        await callback.answer("Материал не найден")
        return
    item = u["saved"][idx]
    # делаем его «текущим», чтобы работали кнопки доработки и публикации
    u["last_answer"] = item["text"]
    u["last_prompt"] = u["last_prompt"] or item["title"]
    u["tool"] = item["tool"] if item["tool"] in TOOLS else u["tool"]
    save_data()
    body = md_to_html(item["text"][:TG_LIMIT])
    await callback.message.answer(
        body,
        reply_markup=kb(
            [btn("📤 В мой канал", "pub"), btn("🗑 Удалить", f"del_{idx}")],
            [btn("◀️ К материалам", "saved")],
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del_"))
async def delete_saved(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    idx = int(callback.data.removeprefix("del_"))
    if idx < len(u["saved"]):
        u["saved"].pop(idx)
        save_data()
    text, markup = saved_list_view(u)
    await safe_edit(callback.message, text, markup)
    await callback.answer("Удалено")


# ====================================================
# МОЙ КАНАЛ: подключение, публикация, отложенный постинг
# ====================================================
class ChannelSetup(StatesGroup):
    waiting = State()


CHANNEL_HOWTO = (
    "1️⃣ Добавьте этого бота в администраторы канала с правом «Публикация сообщений».\n"
    "2️⃣ Перешлите сюда любой пост из канала — или отправьте его @username."
)


@dp.callback_query(F.data == "channel")
async def channel_menu(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    ch = u["channel"]
    if ch:
        pending = len(u["scheduled"])
        text = (
            f"📤 <b>Мой канал</b>\n{LINE}\n\n"
            f"✅ Подключён: <b>{esc(ch['title'])}</b>\n"
            f"⏰ В очереди публикаций: <b>{pending}</b>\n\n"
            "Под любым результатом нажмите «📤 В мой канал» — опубликую сразу или по расписанию."
        )
        rows = []
        if pending:
            rows.append([btn("⏰ Очередь публикаций", "queue")])
        rows += [[btn("🔄 Сменить канал", "channel_connect"), btn("🔌 Отключить", "channel_off")], back_main_row()]
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        text = (
            f"📤 <b>Автопостинг в ваш канал</b>\n{LINE}\n\n"
            "Публикуйте готовые посты в свой Telegram-канал в один клик — "
            "сразу или в нужное время.\n\n" + CHANNEL_HOWTO
        )
        markup = kb([btn("🔗 Подключить канал", "channel_connect")], back_main_row())
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "channel_connect")
async def channel_connect(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ChannelSetup.waiting)
    await safe_edit(
        callback.message,
        f"🔗 <b>Подключение канала</b>\n{LINE}\n\n{CHANNEL_HOWTO}",
        kb([btn("✖️ Отмена", "channel")]),
    )
    await callback.answer()


@dp.message(ChannelSetup.waiting)
async def channel_receive(message: types.Message, state: FSMContext):
    chat = None
    origin_chat = getattr(message.forward_origin, "chat", None) if message.forward_origin else None
    try:
        if origin_chat and origin_chat.type == "channel":
            chat = origin_chat
        elif message.text:
            ref = message.text.strip()
            ref = ref.replace("https://t.me/", "@").replace("t.me/", "@")
            if not ref.startswith("@") and not ref.lstrip("-").isdigit():
                ref = "@" + ref
            chat = await bot.get_chat(ref)
    except Exception:
        chat = None

    if not chat or chat.type != "channel":
        await message.answer("❌ Не нашёл канал. Перешлите пост из канала или отправьте его @username.")
        return

    try:
        me = await bot.me()
        bot_member = await bot.get_chat_member(chat.id, me.id)
        user_member = await bot.get_chat_member(chat.id, message.from_user.id)
    except Exception:
        await message.answer("❌ Бот не видит канал. Сначала добавьте его в администраторы канала.")
        return

    if bot_member.status != "administrator" or not getattr(bot_member, "can_post_messages", False):
        await message.answer("❌ Дайте боту права администратора с разрешением «Публикация сообщений» и попробуйте снова.")
        return
    # защита: публиковать можно только в канал, где пользователь сам админ
    if user_member.status not in ("creator", "administrator"):
        await message.answer("❌ Вы не администратор этого канала.")
        return

    await state.clear()
    u = get_user_info(message.from_user.id)
    u["channel"] = {"id": chat.id, "title": chat.title or "Канал"}
    save_data()
    await message.answer(
        f"✅ <b>Канал «{esc(chat.title)}» подключён!</b>\n\nТеперь под каждым результатом есть кнопка «📤 В мой канал».",
        reply_markup=kb([btn("✍️ Создать пост", "create")], back_main_row()),
    )


@dp.callback_query(F.data == "channel_off")
async def channel_off(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    u["channel"] = None
    u["scheduled"] = []
    save_data()
    await callback.answer("Канал отключён")
    text, markup = main_menu(callback.from_user)
    await safe_edit(callback.message, text, markup)


async def post_to_channel(channel_id: int, text: str):
    for chunk in split_text(text):
        try:
            await bot.send_message(channel_id, md_to_html(chunk))
        except TelegramBadRequest:
            await bot.send_message(channel_id, chunk, parse_mode=None)


def schedule_options() -> dict:
    now = datetime.now(TZ)
    today_19 = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if today_19 <= now:
        today_19 += timedelta(days=1)
    tomorrow_10 = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return {
        "1h": ("⏱ Через 1 час", now + timedelta(hours=1)),
        "19": (f"🌆 {'Сегодня' if today_19.date() == now.date() else 'Завтра'} в 19:00", today_19),
        "10": ("🌅 Завтра в 10:00", tomorrow_10),
    }


@dp.callback_query(F.data == "pub")
async def publish_menu(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    if not u["last_answer"]:
        await callback.answer("Сначала создайте материал", show_alert=True)
        return
    if not u["channel"]:
        await callback.message.answer(
            f"📤 <b>Сначала подключите канал</b>\n\n{CHANNEL_HOWTO}",
            reply_markup=kb([btn("🔗 Подключить канал", "channel_connect")]),
        )
        await callback.answer()
        return
    rows = [[btn("🚀 Опубликовать сейчас", "pub_now")]]
    lock = "" if is_sub_active(callback.from_user.id, u) else " 🔒"
    rows += [[btn(label + lock, f"pub_at_{key}")] for key, (label, _) in schedule_options().items()]
    await callback.message.answer(
        f"📤 Публикация в <b>{esc(u['channel']['title'])}</b>\n\nКогда публикуем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data == "pub_now")
async def publish_now(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    if not u["channel"] or not u["last_answer"]:
        await callback.answer("Нечего публиковать", show_alert=True)
        return
    try:
        await post_to_channel(u["channel"]["id"], u["last_answer"])
    except Exception as e:
        logging.error(f"Publish error: {e}")
        await safe_edit(callback.message, "⚠️ Не удалось опубликовать. Проверьте, что бот всё ещё админ канала.")
        await callback.answer()
        return
    await safe_edit(callback.message, f"✅ Опубликовано в <b>{esc(u['channel']['title'])}</b>!")
    await callback.answer("Готово 🚀")


@dp.callback_query(F.data.startswith("pub_at_"))
async def publish_later(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    u = get_user_info(user_id)
    if not is_sub_active(user_id, u):
        text, markup = pro_upsell("⏰ Отложенный постинг")
        await safe_edit(callback.message, text, markup)
        await callback.answer()
        return
    option = schedule_options().get(callback.data.removeprefix("pub_at_"))
    if not option or not u["channel"] or not u["last_answer"]:
        await callback.answer("Не получилось запланировать", show_alert=True)
        return
    label, when = option
    u["scheduled"].append({"at": when.isoformat(), "text": u["last_answer"], "channel": u["channel"]["id"]})
    save_data()
    await safe_edit(
        callback.message,
        f"⏰ Запланировано на <b>{when:%d.%m в %H:%M}</b> (Киев) в <b>{esc(u['channel']['title'])}</b>.",
        kb([btn("📋 Очередь публикаций", "queue")]),
    )
    await callback.answer("Запланировано ✅")


def queue_screen(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    if not u["scheduled"]:
        return "⏰ Очередь пуста.", kb([btn("◀️ Мой канал", "channel")])
    text = f"⏰ <b>Очередь публикаций</b>\n{LINE}\n\n"
    rows = []
    for item in sorted(u["scheduled"], key=lambda x: x["at"]):
        when = datetime.fromisoformat(item["at"])
        preview = re.sub(r"[*#_`]", "", item["text"]).strip().split("\n")[0][:30]
        text += f"• <b>{when:%d.%m %H:%M}</b> — {esc(preview)}\n"
        rows.append([btn(f"✖️ Отменить {when:%d.%m %H:%M}", f"unq_{item['at']}")])
    rows.append([btn("◀️ Мой канал", "channel")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "queue")
async def queue_view(callback: types.CallbackQuery):
    text, markup = queue_screen(get_user_info(callback.from_user.id))
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("unq_"))
async def queue_cancel(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    at = callback.data.removeprefix("unq_")
    u["scheduled"] = [item for item in u["scheduled"] if item["at"] != at]
    save_data()
    text, markup = queue_screen(u)
    await safe_edit(callback.message, text, markup)
    await callback.answer("Публикация отменена")


async def scheduler_loop():
    """Каждые 30 секунд публикует посты, время которых наступило."""
    while True:
        await asyncio.sleep(30)
        now = datetime.now(TZ)
        for user_id, u in list(user_data.items()):
            queue = u.get("scheduled") or []
            due = [item for item in queue if datetime.fromisoformat(item["at"]) <= now]
            if not due:
                continue
            u["scheduled"] = [item for item in queue if item not in due]
            save_data()
            for item in due:
                try:
                    await post_to_channel(item["channel"], item["text"])
                    await bot.send_message(user_id, "✅ Запланированный пост опубликован в вашем канале!")
                except Exception as e:
                    logging.error(f"Scheduled post failed for {user_id}: {e}")
                    try:
                        await bot.send_message(
                            user_id,
                            "⚠️ Не удалось опубликовать запланированный пост. Проверьте, что бот — админ канала.",
                        )
                    except Exception:
                        pass


# ====================================================
# 🔥 РАЗБОРЫ ПОСТОВ: лучшие / провальные / практика
# ====================================================
CASES_FILE = os.getenv("CASES_FILE", "cases.json")
FREE_PRACTICE_PER_DAY = 1


def load_cases() -> list:
    try:
        with open(CASES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logging.error(f"Не удалось прочитать {CASES_FILE}: {e}")
        return []


def save_cases():
    if DATABASE_URL:
        _pending.add("cases")
        return
    try:
        tmp = CASES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False)
        os.replace(tmp, CASES_FILE)
    except Exception as e:
        logging.error(f"Не удалось сохранить кейсы: {e}")


cases: list[dict] = load_cases()

# --- настройки автопубликации ---
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")
MORNING_HOUR = 9    # ☀️ топовый пост (по Киеву)
EVENING_HOUR = 19   # 🌙 разбор ошибок (по Киеву)
SETTINGS_DEFAULTS = {"auto_enabled": True, "last_morning": "", "last_evening": "", "promo_channel": None}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return {**SETTINGS_DEFAULTS, **json.load(f)}
    except Exception:
        return dict(SETTINGS_DEFAULTS)


def save_settings():
    if DATABASE_URL:
        _pending.add("settings")
        return
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Не удалось сохранить настройки: {e}")


settings = load_settings()

CASE_KINDS = {"best": "🏆 Удачный", "worst": "❌ Неудачный", "practice": "🧩 Практика"}
PERIODS = {"day": "дня", "week": "недели", "month": "месяца"}

ANALYSIS_PROMPTS = {
    "best": (
        "Ты топовый SMM-эксперт. Разбери, почему этот пост удачный: 1) общая оценка 1–10; "
        "2) хук — чем цепляет; 3) структура и подача; 4) психологические триггеры; "
        "5) CTA и вовлечение; 6) три приёма, которые стоит взять себе; 7) что всё же можно улучшить."
    ),
    "worst": (
        "Ты строгий SMM-эксперт. Разбери, почему этот пост неудачный: 1) общая оценка 1–10; "
        "2) каждую ошибку отдельно — где именно она и почему вредит (хук, структура, оффер, CTA, "
        "грамотность, тон); 3) что всё-таки сделано хорошо, если есть; 4) исправленная версия поста."
    ),
    "practice": (
        "Ты SMM-эксперт. Сделай эталонный разбор поста: оценка 1–10, сильные стороны, ошибки с "
        "объяснением, 3 главных вывода и улучшенная версия."
    ),
}

GRADE_PROMPT = (
    "Ты ментор SMM-академии. Сравни ответ студента с эталонным разбором эксперта и дай обратную связь. "
    "Структура ответа:\n"
    "🎯 Оценка ответа студента: X/10\n"
    "✅ Что студент заметил верно\n"
    "❌ Что упустил или где ошибся — и почему это важно\n"
    "💡 Правильные выводы по кейсу (3–5 пунктов)\n"
    "📈 Что прокачать студенту дальше\n"
    "Будь доброжелательным, но честным. Не пересказывай эталон целиком."
)

GEN_CASE_PROMPTS = {
    "best": "Придумай реалистичный сильный пост для Instagram: мощный хук, польза, история, чёткий CTA.",
    "worst": "Придумай реалистичный слабый пост для Instagram с 4–6 типичными ошибками новичков: скучное начало, вода, нет оффера, слабый CTA, канцелярит. Ошибки не помечай.",
    "practice": "Придумай реалистичный неоднозначный пост для Instagram: в нём есть и сильные стороны, и заметные ошибки. Ничего не помечай.",
}
NICHES = [
    "кофейня", "салон красоты", "онлайн-школа английского", "фитнес-тренер", "магазин одежды",
    "стоматология", "юрист", "кондитерская на заказ", "агентство недвижимости", "психолог",
    "автосервис", "доставка цветов", "фотограф", "IT-курсы", "детский центр",
]


def case_title(c: dict) -> str:
    if c["kind"] == "best":
        return f"🏆 Лучший пост {PERIODS.get(c.get('period'), '')}".strip()
    if c["kind"] == "worst":
        return f"❌ Провальный пост {PERIODS.get(c.get('period'), '')}".strip()
    return "🧩 Кейс для практики"


def find_case(case_id: int) -> dict | None:
    return next((c for c in cases if c["id"] == case_id), None)


def published_cases() -> list[dict]:
    """Кейсы, видимые пользователям (без стоящих в очереди)."""
    return [c for c in cases if not c.get("queued")]


def latest_case(kind: str, period: str | None = None) -> dict | None:
    for c in reversed(published_cases()):
        if c["kind"] == kind and (period is None or c.get("period") == period):
            return c
    return None


def case_question(c: dict) -> str:
    if c["kind"] == "best":
        return "Как думаете, почему он сработал?"
    if c["kind"] == "worst":
        return "Найдёте все ошибки?"
    return "Что здесь хорошо, а что плохо? Какую оценку поставите?"


async def ai_complete(system: str, user: str) -> str:
    response = await ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return response.choices[0].message.content or ""


async def send_long(message: types.Message, text: str, markup=None):
    """Отправляет ответ ИИ частями, клавиатура — под последней частью."""
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        try:
            await message.answer(md_to_html(chunk), reply_markup=markup if is_last else None)
        except TelegramBadRequest:
            await message.answer(chunk, reply_markup=markup if is_last else None, parse_mode=None)


# ---------- пользовательская часть ----------
CASES_INTRO = (
    f"🔥 <b>Разборы постов</b>\n{LINE}\n\n"
    "Реальные публикации — лучшие и провальные — с разбором от эксперта: "
    "что сработало, где ошибка и как сделать правильно.\n\n"
    "🧩 <b>Практика:</b> оцените кейс сами — ИИ-ментор покажет, где вы правы, а что упустили."
)


def cases_menu_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("🏆 Пост дня", "cs_best_day"), btn("📅 Пост недели", "cs_best_week")],
        [btn("🗓 Пост месяца", "cs_best_month"), btn("❌ Разбор ошибок", "cs_worst")],
        [btn("🧩 Практика: оцени кейс сам", "cs_practice")],
        [btn("📚 Архив разборов", "cs_archive"), btn("🏠 Меню", "back_to_main")],
    )


@dp.callback_query(F.data == "cases")
async def cases_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, CASES_INTRO, cases_menu_kb())
    await callback.answer()


async def show_case(message: types.Message, c: dict):
    if c.get("photo"):
        try:
            await message.answer_photo(c["photo"])
        except Exception:
            pass
    question = case_question(c)
    label = "\n<i>📝 Учебный пример — совпадения с реальными брендами случайны</i>" if c.get("ai") else ""
    text = (
        f"{case_title(c)} · {c.get('date', '')}{label}\n{LINE}\n\n"
        f"{esc(c['text'][:3300])}\n\n{LINE}\n🤔 <b>{question}</b>\n"
        "Сначала подумайте сами — так навык растёт быстрее."
    )
    await message.answer(
        text,
        reply_markup=kb(
            [btn("✍️ Дать свою оценку", f"cs_try_{c['id']}")],
            [btn("🔍 Показать разбор эксперта", f"cs_show_{c['id']}")],
            [btn("◀️ Разборы", "cases")],
        ),
    )


CASE_BUTTONS = {
    "cs_best_day": ("best", "day"),
    "cs_best_week": ("best", "week"),
    "cs_best_month": ("best", "month"),
    "cs_worst": ("worst", None),
}


@dp.callback_query(F.data.in_(set(CASE_BUTTONS)))
async def open_case_category(callback: types.CallbackQuery):
    kind, period = CASE_BUTTONS[callback.data]
    c = latest_case(kind, period)
    if not c:
        await callback.answer("Скоро здесь появится новый разбор 🔥", show_alert=True)
        return
    await show_case(callback.message, c)
    await callback.answer()


@dp.callback_query(F.data == "cs_practice")
async def open_practice(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    # сначала нерешённые кейсы практики, потом любые нерешённые
    pool = [c for c in reversed(published_cases()) if c["id"] not in u["cases_solved"]]
    pool.sort(key=lambda c: c["kind"] != "practice")
    if not pool:
        await callback.answer("Вы решили все кейсы 🏆 Новые появятся скоро!", show_alert=True)
        return
    await show_case(callback.message, pool[0])
    await callback.answer()


@dp.callback_query(F.data.startswith("cs_open_"))
async def open_case_by_id(callback: types.CallbackQuery):
    c = find_case(int(callback.data.removeprefix("cs_open_")))
    if not c:
        await callback.answer("Разбор не найден", show_alert=True)
        return
    await show_case(callback.message, c)
    await callback.answer()


@dp.callback_query(F.data == "cs_archive")
async def cases_archive(callback: types.CallbackQuery):
    if not cases:
        await callback.answer("Архив пока пуст", show_alert=True)
        return
    rows = [
        [btn(f"{case_title(c)} · {c.get('date', '')}"[:60], f"cs_open_{c['id']}")]
        for c in list(reversed(published_cases()))[:15]
    ]
    rows.append([btn("◀️ Разборы", "cases")])
    await safe_edit(
        callback.message,
        f"📚 <b>Архив разборов</b>\n{LINE}\n\nПоследние кейсы:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cs_show_"))
async def show_case_analysis(callback: types.CallbackQuery):
    c = find_case(int(callback.data.removeprefix("cs_show_")))
    if not c:
        await callback.answer("Разбор не найден", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(f"🔍 <b>Разбор эксперта</b> · {case_title(c)}\n{LINE}")
    await send_long(
        callback.message,
        c["analysis"],
        kb([btn("🧩 Попрактиковаться на другом кейсе", "cs_practice")], [btn("◀️ Разборы", "cases")]),
    )


class CaseTry(StatesGroup):
    answer = State()


def practice_allowed(user_id: int, u: dict) -> bool:
    if is_sub_active(user_id, u):
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    if u["practice_date"] != today:
        u["practice_date"], u["practice_count"] = today, 0
    return u["practice_count"] < FREE_PRACTICE_PER_DAY


@dp.callback_query(F.data.startswith("cs_try_"))
async def case_try_start(callback: types.CallbackQuery, state: FSMContext):
    case_id = int(callback.data.removeprefix("cs_try_"))
    u = get_user_info(callback.from_user.id)
    if not practice_allowed(callback.from_user.id, u):
        await callback.message.answer(
            "🧩 <b>Бесплатная практика на сегодня использована</b>\n\n"
            f"В Free — {FREE_PRACTICE_PER_DAY} проверка ментора в день. В PRO — без ограничений.",
            reply_markup=kb(
                [btn("🔍 Посмотреть разбор эксперта", f"cs_show_{case_id}")],
                [btn("💎 Открыть PRO", "buy_menu")],
            ),
        )
        await callback.answer()
        return
    await state.set_state(CaseTry.answer)
    await state.update_data(case_id=case_id)
    await callback.message.answer(
        "✍️ <b>Ваша оценка</b>\n\nНапишите одним сообщением:\n"
        "• что в посте сделано хорошо\n"
        "• какие ошибки видите\n"
        "• какую оценку 1–10 поставите\n\n"
        "ИИ-ментор сравнит ваш ответ с разбором эксперта.",
        reply_markup=kb([btn("✖️ Отмена", "cases")]),
    )
    await callback.answer()


@dp.message(CaseTry.answer, F.text)
async def case_try_grade(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    c = find_case(data.get("case_id", -1))
    if not c:
        await message.answer("Кейс не найден.", reply_markup=kb([btn("◀️ Разборы", "cases")]))
        return
    user_id = message.from_user.id
    u = get_user_info(user_id)
    status_msg = await message.answer("🧑‍🏫 Ментор проверяет ваш ответ...")
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        feedback = await ai_complete(
            GRADE_PROMPT + " " + LANG_INSTRUCTION.get(u["lang"], "") + FORMAT_HINT,
            f"ПОСТ:\n{c['text']}\n\nЭТАЛОННЫЙ РАЗБОР ЭКСПЕРТА:\n{c['analysis']}\n\nОТВЕТ СТУДЕНТА:\n{message.text[:3000]}",
        )
    except Exception as e:
        logging.error(f"Grade error: {e}")
        await notify_admin(f"Grade: {e}", user_id)
        await safe_edit(status_msg, "⚠️ Ментор временно недоступен. Попробуйте позже — попытка не списана.")
        return

    if not is_sub_active(user_id, u):
        u["practice_count"] += 1
    track_generation("case_practice")
    if c["id"] not in u["cases_solved"]:
        u["cases_solved"].append(c["id"])
    save_data()
    await safe_edit(status_msg, f"🧑‍🏫 <b>Обратная связь ментора</b>\n{LINE}")
    await send_long(
        message,
        feedback,
        kb(
            [btn("🔍 Полный разбор эксперта", f"cs_show_{c['id']}")],
            [btn("🧩 Следующий кейс", "cs_practice"), btn("◀️ Разборы", "cases")],
        ),
    )


@dp.callback_query(F.data == "notify_off")
async def notify_off(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    u["notify"] = False
    save_data()
    await callback.answer("Уведомления о разборах отключены", show_alert=True)


# ---------- админ-часть: добавление кейсов ----------
class AdminCase(StatesGroup):
    text = State()
    kind = State()
    period = State()
    review = State()
    custom = State()


def admin_cases_kb() -> InlineKeyboardMarkup:
    auto = "✅ ВКЛ" if settings["auto_enabled"] else "⛔️ ВЫКЛ"
    return kb(
        [btn("➕ Добавить реальный пост", "adm_case_add")],
        [btn("✨ Сгенерировать кейс ИИ", "adm_case_gen")],
        [btn(f"🤖 Автопубликация: {auto}", "adm_auto_toggle")],
        [btn("▶️ Тест: утро", "adm_auto_morning"), btn("▶️ Тест: вечер", "adm_auto_evening")],
        [btn("📢 Промо-канал", "adm_promo"), btn("🗂 Управлять", "adm_case_list")],
        [btn("◀️ Админ-панель", "adm_panel")],
    )


def admin_cases_text() -> str:
    pub = published_cases()
    counts = {k: sum(1 for c in pub if c["kind"] == k) for k in CASE_KINDS}
    queued = {k: sum(1 for c in cases if c.get("queued") and c["kind"] == k) for k in ("best", "worst")}
    promo = settings.get("promo_channel")
    return (
        f"🔥 <b>Разборы постов</b>\n{LINE}\n\n"
        f"🏆 Удачных: <b>{counts['best']}</b> · ❌ Неудачных: <b>{counts['worst']}</b> · "
        f"🧩 Практика: <b>{counts['practice']}</b>\n\n"
        f"🤖 <b>Автопубликация</b> {'включена' if settings['auto_enabled'] else 'выключена'}:\n"
        f"☀️ {MORNING_HOUR}:00 — топовый пост (в очереди реальных: {queued['best']})\n"
        f"🌙 {EVENING_HOUR}:00 — разбор ошибок (в очереди реальных: {queued['worst']})\n"
        "<i>Если очередь пуста — ИИ сам создаёт учебный кейс.</i>\n\n"
        f"📢 Промо-канал: <b>{esc(promo['title']) if promo else 'не подключён'}</b>\n\n"
        "💡 <i>Реальные посты публикуйте с разрешения автора, в провальных — скрывайте имя и бренд.</i>"
    )


@dp.callback_query(F.data == "adm_cases", IsAdmin)
async def admin_cases(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, admin_cases_text(), admin_cases_kb())
    await callback.answer()


@dp.callback_query(F.data == "adm_case_add", IsAdmin)
async def admin_case_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminCase.text)
    await safe_edit(
        callback.message,
        "➕ <b>Новый кейс</b>\n\nПришлите текст поста. Можно фото с подписью или переслать пост из канала.",
        kb([btn("✖️ Отмена", "adm_cases")]),
    )
    await callback.answer()


def kind_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("🏆 Удачный", "adm_ck_best"), btn("❌ Неудачный", "adm_ck_worst")],
        [btn("🧩 Для практики", "adm_ck_practice")],
        [btn("✖️ Отмена", "adm_cases")],
    )


def period_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("Пост дня", "adm_cp_day"), btn("Пост недели", "adm_cp_week"), btn("Пост месяца", "adm_cp_month")],
        [btn("✖️ Отмена", "adm_cases")],
    )


@dp.message(AdminCase.text, IsAdmin)
async def admin_case_text(message: types.Message, state: FSMContext):
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("❌ Нужен текст поста. Пришлите текст или фото с подписью.")
        return
    photo = message.photo[-1].file_id if message.photo else None
    await state.update_data(text=text[:3500], photo=photo)
    await state.set_state(AdminCase.kind)
    await message.answer("Какой это кейс?", reply_markup=kind_kb())


@dp.callback_query(F.data == "adm_case_gen", IsAdmin)
async def admin_case_gen(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminCase.kind)
    await state.update_data(generate=True)
    await safe_edit(callback.message, "✨ Какой кейс сгенерировать?", kind_kb())
    await callback.answer()


@dp.callback_query(AdminCase.kind, F.data.startswith("adm_ck_"), IsAdmin)
async def admin_case_kind(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.removeprefix("adm_ck_")
    await state.update_data(kind=kind)
    await callback.answer()
    if kind == "practice":
        await state.update_data(period=None)
        await admin_case_prepare(callback.message, state)
        return
    await state.set_state(AdminCase.period)
    await safe_edit(callback.message, f"{CASE_KINDS[kind]} — куда публикуем?", period_kb())


@dp.callback_query(AdminCase.period, F.data.startswith("adm_cp_"), IsAdmin)
async def admin_case_period(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(period=callback.data.removeprefix("adm_cp_"))
    await callback.answer()
    await admin_case_prepare(callback.message, state)


async def admin_case_prepare(message: types.Message, state: FSMContext):
    """Генерирует пост (если нужно) и экспертный разбор, показывает превью."""
    data = await state.get_data()
    lang = LANG_INSTRUCTION.get(get_user_info(ADMIN_TELEGRAM_ID)["lang"], "")
    status = await message.answer("⏳ Готовлю экспертный разбор...")
    try:
        if data.get("generate") and not data.get("text"):
            niche = random.choice(NICHES)
            post = await ai_complete(
                GEN_CASE_PROMPTS[data["kind"]] + f" Ниша: {niche}. Выдай только текст поста. " + lang,
                "Сгенерируй пост.",
            )
            await state.update_data(text=post[:3500], photo=None)
            data["text"] = post[:3500]
        analysis = await ai_complete(ANALYSIS_PROMPTS[data["kind"]] + " " + lang + FORMAT_HINT, data["text"])
    except Exception as e:
        logging.error(f"Case prepare error: {e}")
        await safe_edit(status, "⚠️ ИИ недоступен, попробуйте ещё раз.", kb([btn("◀️ Кейсы", "adm_cases")]))
        await state.clear()
        return
    await state.update_data(analysis=analysis)
    await state.set_state(AdminCase.review)
    await safe_edit(status, f"👀 <b>Превью кейса</b>\n{LINE}\n\n{esc(data['text'][:1500])}")
    await send_long(message, analysis, case_review_kb())


def case_review_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("✅ Опубликовать сейчас", "adm_cpub")],
        [btn("📥 В очередь автопубликации", "adm_cqueue")],
        [btn("🔄 Другой разбор", "adm_cregen"), btn("✏️ Свой разбор", "adm_cown")],
        [btn("✖️ Отмена", "adm_cases")],
    )


@dp.callback_query(AdminCase.review, F.data == "adm_cregen", IsAdmin)
async def admin_case_regen(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await admin_case_prepare(callback.message, state)


@dp.callback_query(AdminCase.review, F.data == "adm_cown", IsAdmin)
async def admin_case_own(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminCase.custom)
    await callback.message.answer("✏️ Пришлите свой разбор одним сообщением — он заменит разбор ИИ.")
    await callback.answer()


@dp.message(AdminCase.custom, IsAdmin, F.text)
async def admin_case_own_text(message: types.Message, state: FSMContext):
    await state.update_data(analysis=message.text)
    await state.set_state(AdminCase.review)
    await message.answer("✅ Разбор заменён.", reply_markup=case_review_kb())


@dp.callback_query(AdminCase.review, F.data == "adm_cpub", IsAdmin)
async def admin_case_publish(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    case = {
        "id": max((c["id"] for c in cases), default=0) + 1,
        "kind": data["kind"],
        "period": data.get("period"),
        "text": data["text"],
        "photo": data.get("photo"),
        "analysis": data["analysis"],
        "date": datetime.now(TZ).strftime("%d.%m.%Y"),
    }
    cases.append(case)
    save_cases()
    await callback.message.answer(
        f"✅ Опубликовано: <b>{case_title(case)}</b>\n\nРазослать уведомление всем пользователям?",
        reply_markup=kb(
            [btn("🔔 Разослать всем", f"adm_cbc_{case['id']}")],
            [btn("Не надо", "adm_cases")],
        ),
    )
    await callback.answer("Опубликовано ✅")


async def broadcast_case(case: dict, intro: str | None = None):
    sent = failed = 0
    markup = kb([btn("👀 Смотреть разбор", f"cs_open_{case['id']}")], [btn("🔕 Не присылать", "notify_off")])
    text = intro or f"🔥 <b>Новый разбор: {case_title(case)}</b>"
    text += f"\n\n{case_question(case)}"
    for user_id, u in list(user_data.items()):
        if not u.get("notify", True) or u.get("blocked"):
            continue
        try:
            await bot.send_message(user_id, text, reply_markup=markup)
            sent += 1
        except TelegramForbiddenError:
            mark_blocked(user_id)
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # лимит Telegram ~30 сообщений/сек
    save_data()
    await bot.send_message(ADMIN_TELEGRAM_ID, f"📬 Рассылка завершена: доставлено {sent}, не доставлено {failed}.")


@dp.callback_query(AdminCase.review, F.data == "adm_cqueue", IsAdmin)
async def admin_case_queue(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data["kind"] == "practice":
        await callback.answer("Кейсы практики публикуются сразу — нажмите «Опубликовать»", show_alert=True)
        return
    await state.clear()
    cases.append({
        "id": max((c["id"] for c in cases), default=0) + 1,
        "kind": data["kind"],
        "period": "day",
        "text": data["text"],
        "photo": data.get("photo"),
        "analysis": data["analysis"],
        "date": "",
        "queued": True,
    })
    save_cases()
    slot = f"☀️ утром в {MORNING_HOUR}:00" if data["kind"] == "best" else f"🌙 вечером в {EVENING_HOUR}:00"
    await callback.message.answer(
        f"📥 Кейс в очереди — выйдет в ближайший слот {slot} вместо сгенерированного.",
        reply_markup=kb([btn("◀️ Кейсы", "adm_cases")]),
    )
    await callback.answer("В очереди ✅")


# ---------- автопубликация: утро — топ, вечер — ошибки ----------
async def create_auto_case(kind: str) -> dict:
    """Берёт реальный кейс из очереди, а если её нет — ИИ создаёт учебный."""
    today = datetime.now(TZ).strftime("%d.%m.%Y")
    queued = next((c for c in cases if c.get("queued") and c["kind"] == kind), None)
    if queued:
        queued["queued"] = False
        queued["date"] = today
        save_cases()
        return queued

    lang = LANG_INSTRUCTION.get(get_user_info(ADMIN_TELEGRAM_ID)["lang"], "")
    # не повторяем нишу последних кейсов
    recent = {c.get("niche") for c in cases[-6:]}
    niche = random.choice([n for n in NICHES if n not in recent] or NICHES)
    post = await ai_complete(
        GEN_CASE_PROMPTS[kind] + f" Ниша: {niche}. Выдай только текст поста. " + lang,
        "Сгенерируй пост.",
    )
    analysis = await ai_complete(ANALYSIS_PROMPTS[kind] + " " + lang + FORMAT_HINT, post)
    case = {
        "id": max((c["id"] for c in cases), default=0) + 1,
        "kind": kind,
        "period": "day",
        "text": post[:3500],
        "photo": None,
        "analysis": analysis,
        "date": today,
        "ai": True,
        "niche": niche,
    }
    cases.append(case)
    save_cases()
    return case


async def post_to_promo(case: dict, intro: str):
    promo = settings.get("promo_channel")
    if not promo:
        return
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=case_{case['id']}"
    label = "\n<i>📝 Учебный пример</i>" if case.get("ai") else ""
    text = (
        f"{intro}{label}\n{LINE}\n\n{esc(case['text'][:3000])}\n\n{LINE}\n"
        f"🤔 <b>{case_question(case)}</b>\nПолный разбор эксперта — в боте 👇"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Смотреть разбор", url=link)],
    ])
    try:
        if case.get("photo"):
            await bot.send_photo(promo["id"], case["photo"])
        await bot.send_message(promo["id"], text, reply_markup=markup)
    except Exception as e:
        logging.error(f"Promo post failed: {e}")
        await notify_admin(f"Промо-канал: {e}")


AUTO_SLOTS = {
    "morning": ("best", "☀️ <b>Вдохновение дня:</b> топовый пост и почему он работает"),
    "evening": ("worst", "🌙 <b>Разбор ошибок дня:</b> почему этот пост провалился"),
}


async def publish_auto(slot: str):
    kind, intro = AUTO_SLOTS[slot]
    case = await create_auto_case(kind)
    await post_to_promo(case, intro)
    await broadcast_case(case, intro)


async def auto_cases_loop():
    """Раз в минуту проверяет, не пора ли утренней или вечерней публикации."""
    while True:
        await asyncio.sleep(60)
        if not settings["auto_enabled"]:
            continue
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        for slot, hour, key in (("morning", MORNING_HOUR, "last_morning"), ("evening", EVENING_HOUR, "last_evening")):
            # окно 3 часа: если бот перезапустился днём, вчерашний слот не догоняем
            if hour <= now.hour < hour + 3 and settings[key] != today:
                settings[key] = today
                save_settings()
                try:
                    await publish_auto(slot)
                except Exception as e:
                    logging.error(f"Auto publish {slot} failed: {e}")
                    await notify_admin(f"Автопубликация ({slot}): {e}")


@dp.callback_query(F.data == "adm_auto_toggle", IsAdmin)
async def admin_auto_toggle(callback: types.CallbackQuery):
    settings["auto_enabled"] = not settings["auto_enabled"]
    save_settings()
    await safe_edit(callback.message, admin_cases_text(), admin_cases_kb())
    await callback.answer("Автопубликация " + ("включена ✅" if settings["auto_enabled"] else "выключена ⛔️"))


@dp.callback_query(F.data.in_({"adm_auto_morning", "adm_auto_evening"}), IsAdmin)
async def admin_auto_test(callback: types.CallbackQuery):
    slot = callback.data.removeprefix("adm_auto_")
    await callback.answer("Запускаю — это займёт до минуты ⏳")

    async def run():
        try:
            await publish_auto(slot)
        except Exception as e:
            await notify_admin(f"Тест автопубликации: {e}")

    asyncio.create_task(run())


class AdminPromo(StatesGroup):
    waiting = State()


@dp.callback_query(F.data == "adm_promo", IsAdmin)
async def admin_promo(callback: types.CallbackQuery, state: FSMContext):
    promo = settings.get("promo_channel")
    await state.set_state(AdminPromo.waiting)
    rows = [[btn("🔌 Отключить промо-канал", "adm_promo_off")]] if promo else []
    rows.append([btn("✖️ Отмена", "adm_cases")])
    await safe_edit(
        callback.message,
        f"📢 <b>Промо-канал</b>\n{LINE}\n\n"
        "Сюда бот каждый день будет публиковать кейсы с кнопкой «Смотреть разбор» — "
        "люди переходят в бот, и это бесплатная реклама.\n\n"
        "1️⃣ Добавьте бота в админы канала с правом публикации.\n"
        "2️⃣ Перешлите сюда пост из канала или отправьте его @username.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.message(AdminPromo.waiting, IsAdmin)
async def admin_promo_receive(message: types.Message, state: FSMContext):
    chat = None
    origin_chat = getattr(message.forward_origin, "chat", None) if message.forward_origin else None
    try:
        if origin_chat and origin_chat.type == "channel":
            chat = origin_chat
        elif message.text:
            ref = message.text.strip().replace("https://t.me/", "@").replace("t.me/", "@")
            chat = await bot.get_chat(ref if ref.startswith("@") or ref.lstrip("-").isdigit() else "@" + ref)
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id) if chat else None
    except Exception:
        member = None
    if not chat or chat.type != "channel" or not member or member.status != "administrator" \
            or not getattr(member, "can_post_messages", False):
        await message.answer("❌ Не получилось. Проверьте, что бот — админ канала с правом публикации.")
        return
    await state.clear()
    settings["promo_channel"] = {"id": chat.id, "title": chat.title or "Канал"}
    save_settings()
    await message.answer(
        f"✅ Промо-канал <b>{esc(chat.title)}</b> подключён.",
        reply_markup=kb([btn("◀️ Кейсы", "adm_cases")]),
    )


@dp.callback_query(F.data == "adm_promo_off", IsAdmin)
async def admin_promo_off(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    settings["promo_channel"] = None
    save_settings()
    await safe_edit(callback.message, admin_cases_text(), admin_cases_kb())
    await callback.answer("Промо-канал отключён")


@dp.callback_query(F.data.startswith("adm_cbc_"), IsAdmin)
async def admin_case_broadcast(callback: types.CallbackQuery):
    case = find_case(int(callback.data.removeprefix("adm_cbc_")))
    if not case:
        await callback.answer("Кейс не найден", show_alert=True)
        return
    asyncio.create_task(broadcast_case(case))
    await safe_edit(callback.message, "📬 Рассылка запущена — пришлю итог, когда закончится.", kb([btn("◀️ Кейсы", "adm_cases")]))
    await callback.answer()


@dp.callback_query(F.data == "adm_case_list", IsAdmin)
async def admin_case_list(callback: types.CallbackQuery):
    if not cases:
        await callback.answer("Кейсов пока нет", show_alert=True)
        return
    rows = [
        [btn(f"🗑 {'📥 ' if c.get('queued') else ''}{case_title(c)} · {c.get('date') or 'в очереди'}"[:60], f"adm_cdel_{c['id']}")]
        for c in list(reversed(cases))[:20]
    ]
    rows.append([btn("◀️ Кейсы", "adm_cases")])
    await safe_edit(
        callback.message,
        "🗂 <b>Кейсы</b>\n\nНажмите на кейс, чтобы удалить его:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_cdel_"), IsAdmin)
async def admin_case_delete(callback: types.CallbackQuery):
    case_id = int(callback.data.removeprefix("adm_cdel_"))
    cases[:] = [c for c in cases if c["id"] != case_id]
    save_cases()
    await callback.answer("Кейс удалён")
    if cases:
        rows = [
            [btn(f"🗑 {'📥 ' if c.get('queued') else ''}{case_title(c)} · {c.get('date') or 'в очереди'}"[:60], f"adm_cdel_{c['id']}")]
            for c in list(reversed(cases))[:20]
        ]
        rows.append([btn("◀️ Кейсы", "adm_cases")])
        await safe_edit(callback.message, "🗂 <b>Кейсы</b>\n\nНажмите на кейс, чтобы удалить его:",
                        InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await safe_edit(callback.message, "Кейсов больше нет.", admin_cases_kb())


# ====================================================
# 📣 РЕКЛАМНЫЕ РАССЫЛКИ (только владелец)
# ====================================================
class AdminBroadcast(StatesGroup):
    content = State()
    button = State()
    audience = State()
    confirm = State()


AUDIENCES = {
    "all": "👥 Всем",
    "free": "🟡 Только Free",
    "pro": "💎 Только PRO",
}
broadcast_running = False


def audience_ids(audience: str) -> list[int]:
    ids = []
    for uid, u in user_data.items():
        if uid == ADMIN_TELEGRAM_ID or u.get("blocked") or not u.get("ads", True):
            continue
        sub = is_sub_active(uid, u)
        if audience == "free" and sub:
            continue
        if audience == "pro" and not sub:
            continue
        ids.append(uid)
    return ids


def ad_markup(button: dict | None) -> InlineKeyboardMarkup:
    rows = []
    if button:
        rows.append([InlineKeyboardButton(text=button["text"], url=button["url"])])
    rows.append([btn("🔕 Не получать рекламу", "ads_off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_bc", IsAdmin)
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if broadcast_running:
        await callback.answer("Рассылка уже идёт — дождитесь отчёта", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminBroadcast.content)
    reachable = len(audience_ids("all"))
    await safe_edit(
        callback.message,
        f"📣 <b>Рекламная рассылка</b>\n{LINE}\n\n"
        f"Доступно для рассылки: <b>{reachable}</b> чел.\n\n"
        "Пришлите сообщение, которое нужно разослать — <b>в том виде, как его увидят люди</b>:\n"
        "текст, фото или видео с подписью, кружок, голосовое, GIF, файл.\n"
        "Форматирование (жирный, ссылки) сохранится.\n\n"
        "💡 <i>Рекламу партнёров помечайте словом «Реклама» в тексте.</i>",
        kb([btn("✖️ Отмена", "adm_panel")]),
    )
    await callback.answer()


@dp.message(AdminBroadcast.content, IsAdmin)
async def admin_broadcast_content(message: types.Message, state: FSMContext):
    if message.media_group_id:
        await message.answer("⚠️ Альбомы не поддерживаются — пришлите одно фото или видео с подписью.")
        return
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id, button=None)
    await state.set_state(AdminBroadcast.button)
    await message.answer(
        "🔗 Добавить кнопку-ссылку под сообщением?\n\n"
        "Пришлите в формате:\n<code>Текст кнопки | https://ссылка</code>\n\n"
        "Например: <code>🎁 Забрать скидку | https://example.com</code>",
        reply_markup=kb([btn("Без кнопки ➡️", "adm_bc_nobtn")], [btn("✖️ Отмена", "adm_panel")]),
    )


async def ask_audience(message: types.Message, state: FSMContext):
    await state.set_state(AdminBroadcast.audience)
    buttons = [btn(f"{name} ({len(audience_ids(code))})", f"adm_bca_{code}") for code, name in AUDIENCES.items()]
    await message.answer(
        "👥 Кому отправить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons] + [[btn("✖️ Отмена", "adm_panel")]]),
    )


@dp.message(AdminBroadcast.button, IsAdmin, F.text)
async def admin_broadcast_button(message: types.Message, state: FSMContext):
    parts = [p.strip() for p in message.text.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not re.match(r"^(https?://|tg://)\S+$", parts[1]):
        await message.answer("❌ Формат: <code>Текст кнопки | https://ссылка</code>")
        return
    await state.update_data(button={"text": parts[0][:60], "url": parts[1]})
    await ask_audience(message, state)


@dp.callback_query(AdminBroadcast.button, F.data == "adm_bc_nobtn", IsAdmin)
async def admin_broadcast_nobutton(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await ask_audience(callback.message, state)


@dp.callback_query(AdminBroadcast.audience, F.data.startswith("adm_bca_"), IsAdmin)
async def admin_broadcast_audience(callback: types.CallbackQuery, state: FSMContext):
    audience = callback.data.removeprefix("adm_bca_")
    data = await state.get_data()
    await state.update_data(audience=audience)
    await state.set_state(AdminBroadcast.confirm)
    await callback.answer()
    count = len(audience_ids(audience))
    await callback.message.answer("👀 <b>Превью — так увидят пользователи:</b>")
    try:
        await bot.copy_message(
            ADMIN_TELEGRAM_ID, data["src_chat"], data["src_msg"], reply_markup=ad_markup(data.get("button"))
        )
    except TelegramBadRequest as e:
        await state.clear()
        await callback.message.answer(f"⚠️ Это сообщение нельзя разослать: {esc(str(e))}")
        return
    await callback.message.answer(
        f"Отправить <b>{count}</b> пользователям ({AUDIENCES[audience]})?",
        reply_markup=kb([btn("🚀 Отправить", "adm_bc_go")], [btn("✖️ Отмена", "adm_panel")]),
    )


async def run_broadcast(data: dict):
    global broadcast_running
    broadcast_running = True
    ids = audience_ids(data["audience"])
    markup = ad_markup(data.get("button"))
    sent = blocked = failed = 0
    progress = await bot.send_message(ADMIN_TELEGRAM_ID, f"📣 Рассылка: 0 / {len(ids)}")
    try:
        for i, uid in enumerate(ids, 1):
            try:
                await bot.copy_message(uid, data["src_chat"], data["src_msg"], reply_markup=markup)
                sent += 1
            except TelegramRetryAfter as e:  # Telegram просит притормозить
                await asyncio.sleep(e.retry_after + 1)
                try:
                    await bot.copy_message(uid, data["src_chat"], data["src_msg"], reply_markup=markup)
                    sent += 1
                except Exception:
                    failed += 1
            except TelegramForbiddenError:
                mark_blocked(uid)
                blocked += 1
            except Exception:
                failed += 1
            if i % 50 == 0:
                try:
                    await progress.edit_text(f"📣 Рассылка: {i} / {len(ids)}")
                except Exception:
                    pass
            await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    finally:
        broadcast_running = False
        save_data()
    await bot.send_message(
        ADMIN_TELEGRAM_ID,
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📬 Доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
        f"⚠️ Ошибки: <b>{failed}</b>",
    )


@dp.callback_query(AdminBroadcast.confirm, F.data == "adm_bc_go", IsAdmin)
async def admin_broadcast_go(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if broadcast_running:
        await callback.answer("Рассылка уже идёт", show_alert=True)
        return
    asyncio.create_task(run_broadcast(data))
    await safe_edit(callback.message, "🚀 Рассылка запущена — прогресс и отчёт пришлю сюда.")
    await callback.answer()


@dp.callback_query(F.data == "ads_off")
async def ads_off(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    u["ads"] = False
    save_data()
    await callback.answer("Вы отписались от рекламных сообщений", show_alert=True)


# ====================================================
# AI-ГЕНЕРАЦИЯ
# ====================================================
FORMAT_HINT = (
    " Пиши структурно, с короткими абзацами. Для выделения используй только **жирный**, "
    "без таблиц. Не пиши вступлений вроде «Вот ваш пост» — сразу выдавай результат. "
    "Ответ не длиннее 3500 символов."
)


def build_system_prompt(u: dict) -> str:
    tool = TOOLS[u["tool"]]
    prompt = tool["prompt"]
    b = u["brand"]
    if b:
        prompt += (
            f"\n\nКонтекст бренда — учитывай обязательно: ниша — {b.get('niche')}; "
            f"целевая аудитория — {b.get('audience')}; тон — {TONES.get(b.get('tone'), '')}."
        )
        if b.get("samples"):
            examples = "\n---\n".join(b["samples"])
            prompt += (
                "\n\nПримеры постов автора. Копируй его стиль: лексику, длину предложений, "
                f"эмодзи, манеру обращения. Темы не копируй.\n---\n{examples}\n---"
            )
    prompt += " " + LANG_INSTRUCTION.get(u["lang"], LANG_INSTRUCTION["uk"]) + FORMAT_HINT
    return prompt


async def check_access(message: types.Message, user_id: int, u: dict) -> bool:
    """Проверяет лимиты и PRO-доступ. Возвращает True, если можно генерировать."""
    tool = TOOLS[u["tool"]]
    sub = is_sub_active(user_id, u)
    if tool["pro"] and not sub:
        text, markup = pro_upsell(tool["name"])
        await message.answer(text, reply_markup=markup)
        return False
    if not sub and u["attempts"] <= 0:
        if not u["attempts_spent_at"]:
            u["attempts_spent_at"] = datetime.now()
            save_data()
        await message.answer(
            "⚠️ <b>Бесплатные генерации закончились</b>\n\n"
            f"Вы уже создали <b>{u['total_gens']}</b> материалов. Продолжите без ограничений 👇",
            reply_markup=limit_keyboard(),
        )
        return False
    return True


async def run_generation(user_id: int, status_msg: types.Message, messages: list, u: dict):
    sub = is_sub_active(user_id, u)
    tool = TOOLS[u["tool"]]
    await bot.send_chat_action(status_msg.chat.id, ChatAction.TYPING)

    try:
        response = await ai_client.chat.completions.create(model="deepseek-chat", messages=messages)
        ai_text = response.choices[0].message.content or ""
    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        await notify_admin(f"DeepSeek: {e}", user_id)
        await safe_edit(status_msg, "⚠️ Временный сбой ИИ. Генерация не списана — попробуйте ещё раз.")
        return

    if not sub:
        u["attempts"] -= 1
    u["total_gens"] += 1
    track_generation(u["tool"])
    u["last_answer"] = ai_text[:6000]
    save_data()

    chunks = split_text(ai_text)
    header = f"{tool['name']}\n{LINE}\n\n"
    footer = f"\n\n{LINE}\n{status_line(user_id, u)}"
    for i, chunk in enumerate(chunks):
        is_first, is_last = i == 0, i == len(chunks) - 1
        body = (header if is_first else "") + md_to_html(chunk) + (footer if is_last else "")
        markup = result_keyboard() if is_last else None
        try:
            if is_first:
                await status_msg.edit_text(body, reply_markup=markup)
            else:
                await status_msg.answer(body, reply_markup=markup)
        except TelegramBadRequest as e:
            logging.warning(f"HTML fallback: {e}")
            plain = chunk + (re.sub("<[^>]+>", "", footer) if is_last else "")
            if is_first:
                await status_msg.edit_text(plain, reply_markup=markup, parse_mode=None)
            else:
                await status_msg.answer(plain, reply_markup=markup, parse_mode=None)


async def handle_prompt(message: types.Message, user_id: int, prompt: str, status_msg: types.Message = None):
    u = get_user_info(user_id)
    if not await check_access(message, user_id, u):
        return
    u["last_prompt"] = prompt[:4000]
    save_data()
    if status_msg is None:
        status_msg = await message.answer(f"⏳ <b>{TOOLS[u['tool']]['name']}</b> — работаю над вашим запросом...")
    messages = [
        {"role": "system", "content": build_system_prompt(u)},
        {"role": "user", "content": prompt},
    ]
    await run_generation(user_id, status_msg, messages, u)


# Кнопки под результатом: доработать ответ
ACTIONS = {
    "act_more": "Дай другой, совершенно новый вариант с иным подходом.",
    "act_short": "Сократи вдвое, сохранив главное.",
    "act_sell": "Перепиши, усилив продающую часть: выгоды, срочность, сильный CTA.",
    "act_simple": "Перепиши проще и живее, разговорным языком, как другу.",
}


@dp.callback_query(F.data.in_(set(ACTIONS)))
async def refine_result(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    u = get_user_info(user_id)
    if not u["last_prompt"] or not u["last_answer"]:
        await callback.answer("Сначала отправьте запрос 🙂", show_alert=True)
        return
    await callback.answer()
    if not await check_access(callback.message, user_id, u):
        return
    status_msg = await callback.message.answer("⏳ Дорабатываю...")
    messages = [
        {"role": "system", "content": build_system_prompt(u)},
        {"role": "user", "content": u["last_prompt"]},
        {"role": "assistant", "content": u["last_answer"]},
        {"role": "user", "content": ACTIONS[callback.data]},
    ]
    await run_generation(user_id, status_msg, messages, u)


@dp.message(StateFilter(None), F.voice)
async def process_voice_message(message: types.Message):
    user_id = message.from_user.id
    u = get_user_info(user_id)
    if whisper_client is None:
        await message.answer("🎙 Голосовые временно недоступны — отправьте запрос текстом.")
        return
    if not await check_access(message, user_id, u):
        return

    status_msg = await message.answer("🎙 <b>Слушаю голосовое...</b>")
    ogg_file = f"/tmp/voice_{user_id}_{message.message_id}.ogg"
    try:
        file_info = await bot.get_file(message.voice.file_id)
        await bot.download_file(file_info.file_path, ogg_file)
        with open(ogg_file, "rb") as audio_file:
            transcript = await whisper_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        text = transcript.text
    except Exception as e:
        logging.error(f"Voice Error: {e}")
        await notify_admin(f"Voice: {e}", user_id)
        await safe_edit(status_msg, "⚠️ Не удалось распознать голосовое. Попробуйте ещё раз или напишите текстом.")
        return
    finally:
        if os.path.exists(ogg_file):
            os.remove(ogg_file)

    await safe_edit(status_msg, f"🗣 <i>«{esc(text[:1000])}»</i>\n\n⏳ Работаю...")
    await handle_prompt(message, user_id, text, status_msg)


@dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def generate_text(message: types.Message):
    await handle_prompt(message, message.from_user.id, message.text)


# ====================================================
# ЗАПУСК
# ====================================================
async def main():
    if DATABASE_URL:
        await db_init()  # без базы бот не стартует — чтобы не потерять данные
        asyncio.create_task(db_loop())
    else:
        logging.warning("DATABASE_URL не задан — данные хранятся в файлах и пропадут при деплое!")
    await bot.delete_webhook(drop_pending_updates=True)
    public_commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="profile", description="👤 Профиль и баланс"),
        BotCommand(command="brand", description="🎯 Настроить мой бренд"),
        BotCommand(command="buy", description="💎 PRO-доступ"),
        BotCommand(command="id", description="🆔 Мой ID"),
        BotCommand(command="partner", description="🤝 Сотрудничество"),
    ]
    await bot.set_my_commands(public_commands)
    # владельцу дополнительно показываем админ-команду
    try:
        await bot.set_my_commands(
            public_commands + [BotCommand(command="admin", description="🛠 Админ-панель")],
            scope=BotCommandScopeChat(chat_id=ADMIN_TELEGRAM_ID),
        )
    except Exception as e:
        logging.warning(f"Admin commands not set: {e}")
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(auto_cases_loop())
    asyncio.create_task(flush_loop())
    logging.info("Starting bot polling...")
    # явно запрашиваем все нужные типы обновлений, включая блокировки (my_chat_member)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        # при остановке (в т.ч. при деплое) дописываем в базу последние изменения
        save_data()
        save_analytics()
        await db_flush()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
