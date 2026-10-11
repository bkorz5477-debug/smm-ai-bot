import asyncio
import contextvars
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
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
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
    if "lessons" in kv:
        lessons_cache.update(kv["lessons"])
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
                "lessons": lambda: lessons_cache,
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
        current_lang.set(user_data.get(user.id, {}).get("lang"))
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
    "uk": "ВАЖЛИВО: відповідай ТІЛЬКИ українською мовою, навіть якщо запит написано іншою мовою.",
    "ru": "ВАЖНО: отвечай ТОЛЬКО на русском языке, даже если запрос написан на другом языке.",
    "en": "IMPORTANT: reply ONLY in English, even if the request is written in another language.",
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
# 🌐 ПЕРЕВОДЫ ИНТЕРФЕЙСА
# ====================================================
# Тексты в коде написаны на русском. Перед отправкой в Telegram бот сам
# подменяет их на украинский или английский — по языку пользователя.
# Формат: "русский текст": ("українська", "English")
I18N = {
    '📝 Посты и тексты': ('📝 Пости та тексти', '📝 Posts & copy'),
    'Готовые тексты под любую площадку': ('Готові тексти під будь-який майданчик', 'Ready-to-post copy for any platform'),
    '🎬 Reels и Stories': ('🎬 Reels і Stories', '🎬 Reels & Stories'),
    'Сценарии, хуки и прогревы': ('Сценарії, хуки та прогріви', 'Scripts, hooks and warm-ups'),
    '📈 Стратегия и продажи': ('📈 Стратегія та продажі', '📈 Strategy & sales'),
    'Контент-план, воронки, консалтинг': ('Контент-план, воронки, консалтинг', 'Content plan, funnels, consulting'),
    '🎨 Визуал и AI-арт': ('🎨 Візуал та AI-арт', '🎨 Visuals & AI art'),
    'Промпты и стиль ленты': ('Промпти та стиль стрічки', 'Prompts and feed style'),
    '💬 Аудитория и конкуренты': ('💬 Аудиторія та конкуренти', '💬 Audience & competitors'),
    'Ответы на отзывы, разбор конкурентов': ('Відповіді на відгуки, розбір конкурентів', 'Review replies, competitor breakdowns'),
    '📸 Пост Instagram': ('📸 Пост Instagram', '📸 Instagram post'),
    'Хук, сторителлинг, CTA и хэштеги': ('Хук, сторітелінг, CTA та хештеги', 'Hook, storytelling, CTA and hashtags'),
    'Напишите тему поста или пару слов о продукте': ('Напишіть тему поста або кілька слів про продукт', 'Send the post topic or a few words about the product'),
    '🎠 Карусель': ('🎠 Карусель', '🎠 Carousel'),
    'Сценарий 7–10 слайдов + подпись': ('Сценарій 7–10 слайдів + підпис', '7–10 slide script + caption'),
    'Напишите тему карусели': ('Напишіть тему каруселі', 'Send the carousel topic'),
    '✈️ Пост Telegram': ('✈️ Пост Telegram', '✈️ Telegram post'),
    'Ёмкий пост для канала': ('Ємний пост для каналу', 'A tight post for your channel'),
    'Напишите тему поста': ('Напишіть тему поста', 'Send the post topic'),
    '🌐 Кросс-постинг': ('🌐 Крос-постинг', '🌐 Cross-posting'),
    'Одна тема → 5 площадок': ('Одна тема → 5 майданчиків', 'One topic → 5 platforms'),
    'Напишите тему — адаптирую под Instagram, Facebook, Telegram, LinkedIn и X': ('Напишіть тему — адаптую під Instagram, Facebook, Telegram, LinkedIn та X', "Send a topic — I'll adapt it for Instagram, Facebook, Telegram, LinkedIn and X"),
    '🎬 Сценарий Reels': ('🎬 Сценарій Reels', '🎬 Reels script'),
    'Таймкоды, хук, раскадровка': ('Таймкоди, хук, розкадровка', 'Timecodes, hook, storyboard'),
    'Напишите тему или идею ролика': ('Напишіть тему або ідею ролика', 'Send the video topic or idea'),
    '🪝 10 хуков': ('🪝 10 хуків', '🪝 10 hooks'),
    'Первые 3 секунды, которые держат': ('Перші 3 секунди, які тримають', 'First 3 seconds that keep viewers'),
    'Напишите тему видео или поста': ('Напишіть тему відео або поста', 'Send the video or post topic'),
    '🧪 A/B заголовки': ('🧪 A/B заголовки', '🧪 A/B headlines'),
    '3 варианта под разные психотипы': ('3 варіанти під різні психотипи', '3 variants for different personas'),
    'Напишите ваш текущий заголовок или тему': ('Напишіть ваш поточний заголовок або тему', 'Send your current headline or topic'),
    '📱 Прогрев в Stories': ('📱 Прогрів у Stories', '📱 Stories warm-up'),
    'Серия 5–7 Stories к продаже': ('Серія 5–7 Stories до продажу', 'A 5–7 Stories series leading to a sale'),
    'Напишите, что продаёте': ('Напишіть, що продаєте', 'Tell me what you sell'),
    '📅 Контент-план 30 дней': ('📅 Контент-план на 30 днів', '📅 30-day content plan'),
    'План на месяц: формат, тема, цель': ('План на місяць: формат, тема, ціль', 'Monthly plan: format, topic, goal'),
    'Опишите нишу и цель на месяц (охваты, продажи, запуск)': ('Опишіть нішу та ціль на місяць (охоплення, продажі, запуск)', 'Describe your niche and monthly goal (reach, sales, launch)'),
    '💰 Воронка продаж': ('💰 Воронка продажів', '💰 Sales funnel'),
    'От подписчика до покупателя': ('Від підписника до покупця', 'From follower to buyer'),
    'Опишите продукт и его цену': ('Опишіть продукт і його ціну', 'Describe the product and its price'),
    '🏛 AI-консультант': ('🏛 AI-консультант', '🏛 AI consultant'),
    'Стратегический разбор как у CMO': ('Стратегічний розбір рівня CMO', 'CMO-level strategic review'),
    'Опишите ситуацию: проект, цифры, что не работает': ('Опишіть ситуацію: проєкт, цифри, що не працює', "Describe the situation: project, numbers, what isn't working"),
    '👤 Шапка профиля': ('👤 Шапка профілю', '👤 Profile bio'),
    '3 варианта шапки + Highlights': ('3 варіанти шапки + Highlights', '3 bio options + Highlights'),
    'Кто вы и что предлагаете?': ('Хто ви і що пропонуєте?', 'Who are you and what do you offer?'),
    '🎨 Промпты Midjourney': ('🎨 Промпти Midjourney', '🎨 Midjourney prompts'),
    'Готовые промпты 9:16 и 1:1': ('Готові промпти 9:16 та 1:1', 'Ready prompts in 9:16 and 1:1'),
    'Опишите, какое изображение нужно': ('Опишіть, яке зображення потрібне', 'Describe the image you need'),
    '🧩 Стиль ленты': ('🧩 Стиль стрічки', '🧩 Feed style'),
    'Палитра, шрифты, сетка 9 постов': ('Палітра, шрифти, сітка з 9 постів', 'Palette, fonts, 9-post grid'),
    'Опишите нишу и настроение бренда': ('Опишіть нішу та настрій бренду', 'Describe your niche and brand mood'),
    '💬 Ответ на отзыв': ('💬 Відповідь на відгук', '💬 Review reply'),
    '3 варианта ответа, даже на негатив': ('3 варіанти відповіді, навіть на негатив', '3 reply options, even for negative ones'),
    'Вставьте комментарий или отзыв клиента': ('Вставте коментар або відгук клієнта', "Paste the customer's comment or review"),
    '🕵️ Разбор конкурента': ('🕵️ Розбір конкурента', '🕵️ Competitor breakdown'),
    'Что работает у них и как сделать лучше': ('Що працює в них і як зробити краще', 'What works for them and how to do better'),
    'Вставьте текст поста конкурента': ('Вставте текст поста конкурента', "Paste the competitor's post"),
    '#️⃣ Хэштеги и SEO': ('#️⃣ Хештеги та SEO', '#️⃣ Hashtags & SEO'),
    '30 тегов по частотности + ключи': ('30 тегів за частотністю + ключі', '30 tags by popularity + keywords'),
    'Напишите тему поста или нишу': ('Напишіть тему поста або нішу', 'Send the post topic or niche'),
    '🚨 Антикризис': ('🚨 Антикриза', '🚨 Crisis PR'),
    'Пост и план реакции на 48 часов': ('Пост і план реакції на 48 годин', 'A post and a 48-hour response plan'),
    'Опишите, что случилось': ('Опишіть, що сталося', 'Describe what happened'),
    '🔍 Аудит текста / ДЗ': ('🔍 Аудит тексту / ДЗ', '🔍 Copy / homework audit'),
    'Оценка, ошибки и улучшенная версия': ('Оцінка, помилки та покращена версія', 'Score, mistakes and an improved version'),
    'Пришлите текст поста, сценарий или домашнее задание': ('Надішліть текст поста, сценарій або домашнє завдання', 'Send a post, script or homework'),
    '😊 Дружелюбный': ('😊 Дружній', '😊 Friendly'),
    '🎓 Экспертный': ('🎓 Експертний', '🎓 Expert'),
    '🔥 Дерзкий': ('🔥 Зухвалий', '🔥 Bold'),
    '💎 Премиальный': ('💎 Преміальний', '💎 Premium'),
    '👑 <b>VIP</b> · безлимит': ('👑 <b>VIP</b> · безліміт', '👑 <b>VIP</b> · unlimited'),
    '💎 <b>PRO</b> до ': ('💎 <b>PRO</b> до ', '💎 <b>PRO</b> until '),
    ' · безлимит': (' · безліміт', ' · unlimited'),
    '🎁 <b>Пробный PRO</b> до ': ('🎁 <b>Пробний PRO</b> до ', '🎁 <b>PRO trial</b> until '),
    '🟡 <b>Free</b> · осталось генераций: <b>': ('🟡 <b>Free</b> · залишилось генерацій: <b>', '🟡 <b>Free</b> · generations left: <b>'),
    ' дня PRO бесплатно': (' дні PRO безкоштовно', '-day free PRO'),
    '🏠 Главное меню': ('🏠 Головне меню', '🏠 Main menu'),
    '🔄 Ещё вариант': ('🔄 Ще варіант', '🔄 Another version'),
    '✂️ Короче': ('✂️ Коротше', '✂️ Shorter'),
    '🔥 Сильнее продажа': ('🔥 Сильніший продаж', '🔥 Stronger sell'),
    '😊 Проще и живее': ('😊 Простіше та живіше', '😊 Simpler & livelier'),
    '📤 В мой канал': ('📤 У мій канал', '📤 To my channel'),
    '⭐ Сохранить': ('⭐ Зберегти', '⭐ Save'),
    '🛠 Другой инструмент': ('🛠 Інший інструмент', '🛠 Another tool'),
    '🏠 Меню': ('🏠 Меню', '🏠 Menu'),
    '🎁 Акция: 10 генераций — 30 ⭐': ('🎁 Акція: 10 генерацій — 30 ⭐', '🎁 Deal: 10 generations — 30 ⭐'),
    '💎 PRO безлимит на месяц — 500 ⭐': ('💎 PRO безліміт на місяць — 500 ⭐', '💎 PRO unlimited for a month — 500 ⭐'),
    '👥 Пригласить друга (+5 бесплатно)': ('👥 Запросити друга (+5 безкоштовно)', '👥 Invite a friend (+5 free)'),
    '💎 Открыть PRO — 500 ⭐ / мес': ('💎 Відкрити PRO — 500 ⭐ / міс', '💎 Get PRO — 500 ⭐ / mo'),
    '👑 VIP на год — 1000 ⭐': ('👑 VIP на рік — 1000 ⭐', '👑 VIP for a year — 1000 ⭐'),
    '🎉 <b>Новый реферал!</b> Вам начислено <b>+5 генераций</b>!': ('🎉 <b>Новий реферал!</b> Вам нараховано <b>+5 генерацій</b>!', '🎉 <b>New referral!</b> You got <b>+5 generations</b>!'),
    '✍️ <b>Что создаём?</b>\n': ('✍️ <b>Що створюємо?</b>\n', '✍️ <b>What are we creating?</b>\n'),
    '◀️ Категории': ('◀️ Категорії', '◀️ Categories'),
    '.\n\nМожно текстом или 🎙 голосовым.': ('.\n\nМожна текстом або 🎙 голосовим.', '.\n\nText or a 🎙 voice note both work.'),
    'Инструмент выбран ✅': ('Інструмент обрано ✅', 'Tool selected ✅'),
    '✖️ Отмена': ('✖️ Скасувати', '✖️ Cancel'),
    '🎯 <b>Мой бренд</b>\n': ('🎯 <b>Мій бренд</b>\n', '🎯 <b>My brand</b>\n'),
    '\n\nРасскажите о себе один раз — и каждый текст будет написан под вашу нишу, аудиторию и стиль. Без этого ИИ пишет «в целом», с этим — как ваш личный SMM-щик.': ('\n\nРозкажіть про себе один раз — і кожен текст буде написано під вашу нішу, аудиторію та стиль. Без цього ШІ пише «загалом», з цим — як ваш особистий SMM-ник.', '\n\nTell me about yourself once — and every piece of copy will fit your niche, audience and style. Without it the AI writes generically; with it, like your own SMM manager.'),
    '⚡️ Настроить за 30 сек': ('⚡️ Налаштувати за 30 сек', '⚡️ Set up in 30 sec'),
    '\n\n🏷 Ниша: <b>': ('\n\n🏷 Ніша: <b>', '\n\n🏷 Niche: <b>'),
    '</b>\n👥 Аудитория: <b>': ('</b>\n👥 Аудиторія: <b>', '</b>\n👥 Audience: <b>'),
    '</b>\n🎙 Тон: <b>': ('</b>\n🎙 Тон: <b>', '</b>\n🎙 Tone: <b>'),
    '</b>\n📚 Примеров вашего стиля: <b>': ('</b>\n📚 Прикладів вашого стилю: <b>', '</b>\n📚 Samples of your style: <b>'),
    '</b>\n\n✅ Все тексты учитывают эти данные.': ('</b>\n\n✅ Усі тексти враховують ці дані.', '</b>\n\n✅ All copy takes this into account.'),
    '\n\n💡 <i>Пришлите 1–3 своих поста — и бот будет писать вашим голосом, а не «как ИИ».</i>': ('\n\n💡 <i>Надішліть 1–3 своїх пости — і бот писатиме вашим голосом, а не «як ШІ».</i>', '\n\n💡 <i>Send 1–3 of your posts — and the bot will write in your voice, not “like an AI”.</i>'),
    '📚 Обучить на моих постах': ('📚 Навчити на моїх постах', '📚 Learn from my posts'),
    '✏️ Изменить': ('✏️ Змінити', '✏️ Edit'),
    '🗑 Сбросить': ('🗑 Скинути', '🗑 Reset'),
    '🎯 <b>Шаг 1 из 3 — Ниша</b>\n\nЧем вы занимаетесь?\n<i>Например: «маникюр в Киеве», «онлайн-курсы английского», «кофейня»</i>': ('🎯 <b>Крок 1 з 3 — Ніша</b>\n\nЧим ви займаєтесь?\n<i>Наприклад: «манікюр у Києві», «онлайн-курси англійської», «кавʼярня»</i>', '🎯 <b>Step 1 of 3 — Niche</b>\n\nWhat do you do?\n<i>For example: “nail salon in Kyiv”, “online English courses”, “coffee shop”</i>'),
    '👥 <b>Шаг 2 из 3 — Аудитория</b>\n\nКто ваши клиенты?\n<i>Например: «девушки 25–40, ценят качество»</i>': ('👥 <b>Крок 2 з 3 — Аудиторія</b>\n\nХто ваші клієнти?\n<i>Наприклад: «дівчата 25–40, цінують якість»</i>', '👥 <b>Step 2 of 3 — Audience</b>\n\nWho are your customers?\n<i>For example: “women 25–40 who value quality”</i>'),
    '🎙 <b>Шаг 3 из 3 — Тон</b>\n\nКак должен звучать ваш бренд?': ('🎙 <b>Крок 3 з 3 — Тон</b>\n\nЯк має звучати ваш бренд?', '🎙 <b>Step 3 of 3 — Tone</b>\n\nHow should your brand sound?'),
    'Бренд сохранён ✅': ('Бренд збережено ✅', 'Brand saved ✅'),
    '🎉 <b>Готово! Бренд сохранён.</b>\n\n': ('🎉 <b>Готово! Бренд збережено.</b>\n\n', '🎉 <b>Done! Brand saved.</b>\n\n'),
    'Сначала настройте бренд 🙂': ('Спочатку налаштуйте бренд 🙂', 'Set up your brand first 🙂'),
    '📚 <b>Обучение на ваших постах</b>\n': ('📚 <b>Навчання на ваших постах</b>\n', '📚 <b>Learning from your posts</b>\n'),
    '\n\nПришлите до ': ('\n\nНадішліть до ', '\n\nSend up to '),
    ' своих лучших постов — по одному сообщению.\nМожно просто переслать их из своего канала.\n\nБот запомнит вашу лексику, ритм и манеру и будет писать так же.': (' своїх найкращих постів — по одному повідомленню.\nМожна просто переслати їх зі свого каналу.\n\nБот запамʼятає вашу лексику, ритм і манеру та писатиме так само.', ' of your best posts — one per message.\nYou can simply forward them from your channel.\n\nThe bot will learn your vocabulary, rhythm and manner and write the same way.'),
    '✅ <b>Готово! Бот изучил ваш стиль.</b>\n\nТеперь все тексты будут звучать как вы.': ('✅ <b>Готово! Бот вивчив ваш стиль.</b>\n\nТепер усі тексти звучатимуть як ви.', '✅ <b>Done! The bot has learned your style.</b>\n\nAll copy will now sound like you.'),
    '👍 Пример ': ('👍 Приклад ', '👍 Sample '),
    ' сохранён. Пришлите ещё или завершите.': (' збережено. Надішліть ще або завершіть.', ' saved. Send another or finish.'),
    '✅ Готово': ('✅ Готово', '✅ Done'),
    '✅ <b>Стиль сохранён!</b> Теперь тексты будут звучать как вы.': ('✅ <b>Стиль збережено!</b> Тепер тексти звучатимуть як ви.', '✅ <b>Style saved!</b> Your copy will now sound like you.'),
    'Бренд сброшен': ('Бренд скинуто', 'Brand reset'),
    '👤 <b>Профиль</b>\n': ('👤 <b>Профіль</b>\n', '👤 <b>Profile</b>\n'),
    '\n📊 Создано материалов: <b>': ('\n📊 Створено матеріалів: <b>', '\n📊 Pieces created: <b>'),
    '</b>\n🧩 Решено кейсов: <b>': ('</b>\n🧩 Розвʼязано кейсів: <b>', '</b>\n🧩 Cases solved: <b>'),
    '</b>\n🎯 Бренд: <b>': ('</b>\n🎯 Бренд: <b>', '</b>\n🎯 Brand: <b>'),
    'не настроен': ('не налаштовано', 'not set up'),
    '💎 Улучшить до PRO': ('💎 Перейти на PRO', '💎 Upgrade to PRO'),
    '🎁 <b>Бесплатные генерации</b>\n': ('🎁 <b>Безкоштовні генерації</b>\n', '🎁 <b>Free generations</b>\n'),
    '\n\nПриглашайте коллег — за каждого друга <b>+5 генераций</b>.\n\nВаша ссылка (нажмите, чтобы скопировать):\n<code>': ('\n\nЗапрошуйте колег — за кожного друга <b>+5 генерацій</b>.\n\nВаше посилання (натисніть, щоб скопіювати):\n<code>', '\n\nInvite colleagues — <b>+5 generations</b> for every friend.\n\nYour link (tap to copy):\n<code>'),
    '</code>\n\n👥 Приглашено: <b>': ('</code>\n\n👥 Запрошено: <b>', '</code>\n\n👥 Invited: <b>'),
    '📤 Отправить другу': ('📤 Надіслати другові', '📤 Send to a friend'),
    '&text=AI-маркетолог, который пишет посты и сценарии за секунды': ('&text=AI-маркетолог, який пише пости й сценарії за секунди', '&text=An AI marketer that writes posts and scripts in seconds'),
    '🌐 <b>Мова / Язык / Language</b>\n\nНа этом языке будет меню и ответы ИИ:': ('🌐 <b>Мова / Язык / Language</b>\n\nЦією мовою будуть меню та відповіді ШІ:', '🌐 <b>Мова / Язык / Language</b>\n\nMenus and AI replies will use this language:'),
    '🎓 <b>SMM Академия</b>\n': ('🎓 <b>SMM Академія</b>\n', '🎓 <b>SMM Academy</b>\n'),
    '🧪 Тесты': ('🧪 Тести', '🧪 Quizzes'),
    '◀️ Академия': ('◀️ Академія', '◀️ Academy'),
    '🎯 Хуки': ('🎯 Хуки', '🎯 Hooks'),
    'Какая главная задача первых 3 секунд Reels?': ('Яке головне завдання перших 3 секунд Reels?', "What is the main job of a Reel's first 3 seconds?"),
    'Поздороваться и представиться': ('Привітатися та представитися', 'Say hello and introduce yourself'),
    'Вызвать эмоцию или показать проблему': ('Викликати емоцію або показати проблему', 'Trigger an emotion or show a problem'),
    'Сразу предложить купить': ('Одразу запропонувати купити', 'Pitch the product right away'),
    '🧠 Офферы': ('🧠 Офери', '🧠 Offers'),
    'Какой оффер лучше для холодного клиента?': ('Який офер кращий для холодного клієнта?', 'Which offer works best for a cold lead?'),
    '«Купите курс за $500»': ('«Купіть курс за $500»', '“Buy the course for $500”'),
    '«Качественный SMM для всех»': ('«Якісний SMM для всіх»', '“Quality SMM for everyone”'),
    '«Бесплатный аудит + план роста охватов»': ('«Безкоштовний аудит + план росту охоплень»', '“Free audit + a reach growth plan”'),
    '📊 Форматы': ('📊 Формати', '📊 Formats'),
    'Что лучше удерживает внимание?': ('Що краще утримує увагу?', 'What holds attention best?'),
    'Длинные посты без визуала': ('Довгі пости без візуалу', 'Long posts with no visuals'),
    'Карусели со структурой и динамичные Reels': ('Каруселі зі структурою та динамічні Reels', 'Structured carousels and dynamic Reels'),
    'Фото со стока без текста': ('Стокове фото без тексту', 'Stock photos with no text'),
    '🧪 <b>Тесты</b>\n\nВыберите тему:': ('🧪 <b>Тести</b>\n\nОберіть тему:', '🧪 <b>Quizzes</b>\n\nPick a topic:'),
    '🎉 <b>Верно!</b>\n\nВы понимаете современные механики. Примените знания на практике 👇': ('🎉 <b>Правильно!</b>\n\nВи розумієте сучасні механіки. Застосуйте знання на практиці 👇', '🎉 <b>Correct!</b>\n\nYou understand how things work today. Put it into practice 👇'),
    '❌ <b>Не совсем.</b> Правильный ответ: <b>': ('❌ <b>Не зовсім.</b> Правильна відповідь: <b>', '❌ <b>Not quite.</b> The right answer is <b>'),
    '</b>.\n\nРешают первые секунды внимания и попадание в боль клиента.': ('</b>.\n\nВирішують перші секунди уваги та влучання в біль клієнта.', "</b>.\n\nThe first seconds of attention and hitting the customer's pain point are what matter."),
    '🧪 Другие тесты': ('🧪 Інші тести', '🧪 More quizzes'),
    '👑 VIP на год — 1000 ⭐ 🔥': ('👑 VIP на рік — 1000 ⭐ 🔥', '👑 VIP for a year — 1000 ⭐ 🔥'),
    '💎 PRO месяц — 500 ⭐': ('💎 PRO місяць — 500 ⭐', '💎 PRO month — 500 ⭐'),
    '📦 10 генераций — 50 ⭐': ('📦 10 генерацій — 50 ⭐', '📦 10 generations — 50 ⭐'),
    '📦 10 генераций': ('📦 10 генерацій', '📦 10 generations'),
    'Пополнение баланса на 10 генераций.': ('Поповнення балансу на 10 генерацій.', 'Top up your balance with 10 generations.'),
    '🎁 Акция: 10 генераций': ('🎁 Акція: 10 генерацій', '🎁 Deal: 10 generations'),
    'Пополнение баланса по акции.': ('Поповнення балансу за акцією.', 'Promo balance top-up.'),
    '💎 PRO на 30 дней': ('💎 PRO на 30 днів', '💎 PRO for 30 days'),
    'Безлимит и все PRO-инструменты.': ('Безліміт і всі PRO-інструменти.', 'Unlimited use and every PRO tool.'),
    '👑 VIP на 365 дней': ('👑 VIP на 365 днів', '👑 VIP for 365 days'),
    'Безлимит и все PRO-инструменты на год.': ('Безліміт і всі PRO-інструменти на рік.', 'Unlimited use and every PRO tool for a year.'),
    '🎉 <b>Оплата прошла!</b> +10 генераций на балансе.': ('🎉 <b>Оплата пройшла!</b> +10 генерацій на балансі.', '🎉 <b>Payment received!</b> +10 generations added.'),
    '👑 <b>PRO активирован до ': ('👑 <b>PRO активовано до ', '👑 <b>PRO active until '),
    '!</b>\n\nВсе инструменты открыты 🔓': ('!</b>\n\nУсі інструменти відкрито 🔓', '!</b>\n\nEvery tool is unlocked 🔓'),
    '✅ Оплата получена.': ('✅ Оплату отримано.', '✅ Payment received.'),
    '🆔 Ваш ID: <code>': ('🆔 Ваш ID: <code>', '🆔 Your ID: <code>'),
    '</code>\n\n<i>Нажмите на число, чтобы скопировать.</i>': ('</code>\n\n<i>Натисніть на число, щоб скопіювати.</i>', '</code>\n\n<i>Tap the number to copy it.</i>'),
    '📢 Реклама в боте': ('📢 Реклама в боті', '📢 Advertise in the bot'),
    'Размещение вашего продукта или канала для нашей аудитории': ('Розміщення вашого продукту чи каналу для нашої аудиторії', 'Promote your product or channel to our audience'),
    '💸 Партнёрская программа': ('💸 Партнерська програма', '💸 Affiliate program'),
    'Приводите клиентов и получайте % с их оплат': ('Приводьте клієнтів і отримуйте % з їхніх оплат', 'Bring customers and earn a % of their payments'),
    '🏢 Для агентств и команд': ('🏢 Для агенцій і команд', '🏢 For agencies & teams'),
    'Доступ для команды, оптовые тарифы, интеграции': ('Доступ для команди, оптові тарифи, інтеграції', 'Team access, volume pricing, integrations'),
    '💡 Другое предложение': ('💡 Інша пропозиція', '💡 Other proposal'),
    'Коллаборации, взаимный пиар, идеи': ('Колаборації, взаємний піар, ідеї', 'Collabs, cross-promotion, ideas'),
    '🤝 <b>Сотрудничество</b>\n': ('🤝 <b>Співпраця</b>\n', '🤝 <b>Partnership</b>\n'),
    '\n\nОткрыты к партнёрству. Выберите формат:\n\n': ('\n\nВідкриті до партнерства. Оберіть формат:\n\n', "\n\nWe're open to partnerships. Choose a format:\n\n"),
    'Заявку рассмотрим лично и ответим прямо здесь, в боте.': ('Заявку розглянемо особисто й відповімо прямо тут, у боті.', 'We review every request personally and reply right here in the bot.'),
    'Заявка уже отправлена — скоро ответим 🙌': ('Заявку вже надіслано — скоро відповімо 🙌', "Request already sent — we'll reply soon 🙌"),
    '\n\nОпишите предложение одним сообщением:\n• кто вы и что за проект\n• что предлагаете\n• ссылка на канал / сайт\n• как с вами связаться (если не через этот чат)': ('\n\nОпишіть пропозицію одним повідомленням:\n• хто ви і що за проєкт\n• що пропонуєте\n• посилання на канал / сайт\n• як з вами звʼязатися (якщо не через цей чат)', '\n\nDescribe your proposal in one message:\n• who you are and what the project is\n• what you offer\n• a link to your channel / website\n• how to reach you (if not via this chat)'),
    '✅ <b>Заявка отправлена!</b>\n\nМы ответим в этом чате в ближайшее время. Спасибо за интерес 🙌': ('✅ <b>Заявку надіслано!</b>\n\nМи відповімо в цьому чаті найближчим часом. Дякуємо за інтерес 🙌', "✅ <b>Request sent!</b>\n\nWe'll reply in this chat soon. Thanks for your interest 🙌"),
    '⚠️ Не удалось отправить заявку. Попробуйте позже.': ('⚠️ Не вдалося надіслати заявку. Спробуйте пізніше.', "⚠️ Couldn't send the request. Please try again later."),
    '📩 <b>Ответ от команды SMM Architecture AI</b>\n': ('📩 <b>Відповідь від команди SMM Architecture AI</b>\n', '📩 <b>Reply from the SMM Architecture AI team</b>\n'),
    '🤝 Написать ещё': ('🤝 Написати ще', '🤝 Write again'),
    'Пробный период уже был использован': ('Пробний період уже використано', 'The trial has already been used'),
    'PRO активирован 🎉': ('PRO активовано 🎉', 'PRO activated 🎉'),
    '🎉 <b>Пробный PRO на ': ('🎉 <b>Пробний PRO на ', '🎉 <b>Your '),
    ' дня активирован!</b>\n': (' дні активовано!</b>\n', '-day PRO trial is active!</b>\n'),
    '\n\nОткрыто всё:\n• ♾ Безлимитные генерации\n• 📅 Контент-план на 30 дней\n• 💰 Воронки продаж и 🏛 AI-консультант\n• ⏰ Отложенный постинг в ваш канал\n\n💡 Начните с контент-плана — он задаст темы на весь месяц.': ('\n\nВідкрито все:\n• ♾ Безлімітні генерації\n• 📅 Контент-план на 30 днів\n• 💰 Воронки продажів і 🏛 AI-консультант\n• ⏰ Відкладений постинг у ваш канал\n\n💡 Почніть із контент-плану — він задасть теми на весь місяць.', '\n\nEverything is unlocked:\n• ♾ Unlimited generations\n• 📅 30-day content plan\n• 💰 Sales funnels and 🏛 AI consultant\n• ⏰ Scheduled posting to your channel\n\n💡 Start with the content plan — it sets your topics for the whole month.'),
    '📅 Сделать контент-план': ('📅 Зробити контент-план', '📅 Make a content plan'),
    '✍️ Все инструменты': ('✍️ Усі інструменти', '✍️ All tools'),
    'Нечего сохранять': ('Нічого зберігати', 'Nothing to save'),
    'Уже сохранено ⭐': ('Уже збережено ⭐', 'Already saved ⭐'),
    'Сохранено в «Мои материалы» ⭐': ('Збережено в «Мої матеріали» ⭐', 'Saved to “My library” ⭐'),
    '📂 <b>Мои материалы</b>\n': ('📂 <b>Мої матеріали</b>\n', '📂 <b>My library</b>\n'),
    '\n\nПока пусто. Нажмите «⭐ Сохранить» под любым результатом.': ('\n\nПоки порожньо. Натисніть «⭐ Зберегти» під будь-яким результатом.', '\n\nEmpty for now. Tap “⭐ Save” under any result.'),
    '📂 <b>Мои материалы (': ('📂 <b>Мої матеріали (', '📂 <b>My library ('),
    '\n\nНажмите, чтобы открыть:': ('\n\nНатисніть, щоб відкрити:', '\n\nTap to open:'),
    'Материал не найден': ('Матеріал не знайдено', 'Item not found'),
    '🗑 Удалить': ('🗑 Видалити', '🗑 Delete'),
    '◀️ К материалам': ('◀️ До матеріалів', '◀️ Back to library'),
    'Удалено': ('Видалено', 'Deleted'),
    'Без названия': ('Без назви', 'Untitled'),
    '1️⃣ Добавьте этого бота в администраторы канала с правом «Публикация сообщений».\n2️⃣ Перешлите сюда любой пост из канала — или отправьте его @username.': ('1️⃣ Додайте цього бота в адміністратори каналу з правом «Публікація повідомлень».\n2️⃣ Перешліть сюди будь-який пост із каналу — або надішліть його @username.', '1️⃣ Add this bot as a channel admin with the “Post messages” permission.\n2️⃣ Forward any post from the channel here — or send its @username.'),
    '📤 <b>Мой канал</b>\n': ('📤 <b>Мій канал</b>\n', '📤 <b>My channel</b>\n'),
    '\n\n✅ Подключён: <b>': ('\n\n✅ Підключено: <b>', '\n\n✅ Connected: <b>'),
    '</b>\n⏰ В очереди публикаций: <b>': ('</b>\n⏰ У черзі публікацій: <b>', '</b>\n⏰ Posts in queue: <b>'),
    '</b>\n\nПод любым результатом нажмите «📤 В мой канал» — опубликую сразу или по расписанию.': ('</b>\n\nПід будь-яким результатом натисніть «📤 У мій канал» — опублікую одразу або за розкладом.', "</b>\n\nTap “📤 To my channel” under any result — I'll post it now or on schedule."),
    '⏰ Очередь публикаций': ('⏰ Черга публікацій', '⏰ Post queue'),
    '🔄 Сменить канал': ('🔄 Змінити канал', '🔄 Change channel'),
    '🔌 Отключить': ('🔌 Відключити', '🔌 Disconnect'),
    '📤 <b>Автопостинг в ваш канал</b>\n': ('📤 <b>Автопостинг у ваш канал</b>\n', '📤 <b>Auto-posting to your channel</b>\n'),
    '\n\nПубликуйте готовые посты в свой Telegram-канал в один клик — сразу или в нужное время.\n\n': ('\n\nПублікуйте готові пости у свій Telegram-канал в один клік — одразу або в потрібний час.\n\n', '\n\nPublish ready posts to your Telegram channel in one tap — now or at the right time.\n\n'),
    '🔗 Подключить канал': ('🔗 Підключити канал', '🔗 Connect channel'),
    '🔗 <b>Подключение канала</b>\n': ('🔗 <b>Підключення каналу</b>\n', '🔗 <b>Connecting a channel</b>\n'),
    '❌ Не нашёл канал. Перешлите пост из канала или отправьте его @username.': ('❌ Не знайшов канал. Перешліть пост із каналу або надішліть його @username.', '❌ Channel not found. Forward a post from it or send its @username.'),
    '❌ Бот не видит канал. Сначала добавьте его в администраторы канала.': ('❌ Бот не бачить канал. Спочатку додайте його в адміністратори каналу.', "❌ The bot can't see the channel. Add it as a channel admin first."),
    '❌ Дайте боту права администратора с разрешением «Публикация сообщений» и попробуйте снова.': ('❌ Дайте боту права адміністратора з дозволом «Публікація повідомлень» і спробуйте знову.', '❌ Give the bot admin rights with the “Post messages” permission and try again.'),
    '❌ Вы не администратор этого канала.': ('❌ Ви не адміністратор цього каналу.', "❌ You're not an admin of this channel."),
    '✅ <b>Канал «': ('✅ <b>Канал «', '✅ <b>Channel “'),
    '» подключён!</b>\n\nТеперь под каждым результатом есть кнопка «📤 В мой канал».': ('» підключено!</b>\n\nТепер під кожним результатом є кнопка «📤 У мій канал».', '” connected!</b>\n\nEvery result now has a “📤 To my channel” button.'),
    '✍️ Создать пост': ('✍️ Створити пост', '✍️ Create a post'),
    'Канал отключён': ('Канал відключено', 'Channel disconnected'),
    '⏱ Через 1 час': ('⏱ Через 1 годину', '⏱ In 1 hour'),
    '🌆 Сегодня в 19:00': ('🌆 Сьогодні о 19:00', '🌆 Today at 19:00'),
    '🌆 Завтра в 19:00': ('🌆 Завтра о 19:00', '🌆 Tomorrow at 19:00'),
    '🌅 Завтра в 10:00': ('🌅 Завтра о 10:00', '🌅 Tomorrow at 10:00'),
    'Сначала создайте материал': ('Спочатку створіть матеріал', 'Create something first'),
    '📤 <b>Сначала подключите канал</b>\n\n': ('📤 <b>Спочатку підключіть канал</b>\n\n', '📤 <b>Connect a channel first</b>\n\n'),
    '🚀 Опубликовать сейчас': ('🚀 Опублікувати зараз', '🚀 Post now'),
    '📤 Публикация в <b>': ('📤 Публікація в <b>', '📤 Posting to <b>'),
    '</b>\n\nКогда публикуем?': ('</b>\n\nКоли публікуємо?', '</b>\n\nWhen should it go out?'),
    'Нечего публиковать': ('Нічого публікувати', 'Nothing to post'),
    '⚠️ Не удалось опубликовать. Проверьте, что бот всё ещё админ канала.': ('⚠️ Не вдалося опублікувати. Перевірте, що бот досі адмін каналу.', "⚠️ Couldn't post. Check the bot is still a channel admin."),
    '✅ Опубликовано в <b>': ('✅ Опубліковано в <b>', '✅ Posted to <b>'),
    'Готово 🚀': ('Готово 🚀', 'Done 🚀'),
    '⏰ Отложенный постинг': ('⏰ Відкладений постинг', '⏰ Scheduled posting'),
    'Не получилось запланировать': ('Не вдалося запланувати', "Couldn't schedule it"),
    '⏰ Запланировано на <b>': ('⏰ Заплановано на <b>', '⏰ Scheduled for <b>'),
    '</b> (Киев) в <b>': ('</b> (Київ) у <b>', '</b> (Kyiv time) in <b>'),
    '📋 Очередь публикаций': ('📋 Черга публікацій', '📋 Post queue'),
    'Запланировано ✅': ('Заплановано ✅', 'Scheduled ✅'),
    '⏰ Очередь пуста.': ('⏰ Черга порожня.', '⏰ The queue is empty.'),
    '⏰ <b>Очередь публикаций</b>\n': ('⏰ <b>Черга публікацій</b>\n', '⏰ <b>Post queue</b>\n'),
    '✖️ Отменить ': ('✖️ Скасувати ', '✖️ Cancel '),
    '◀️ Мой канал': ('◀️ Мій канал', '◀️ My channel'),
    'Публикация отменена': ('Публікацію скасовано', 'Post cancelled'),
    '✅ Запланированный пост опубликован в вашем канале!': ('✅ Запланований пост опубліковано у вашому каналі!', '✅ Your scheduled post is live on your channel!'),
    '⚠️ Не удалось опубликовать запланированный пост. Проверьте, что бот — админ канала.': ('⚠️ Не вдалося опублікувати запланований пост. Перевірте, що бот — адмін каналу.', "⚠️ Couldn't publish the scheduled post. Check the bot is a channel admin."),
    '🏆 Лучший пост дня': ('🏆 Найкращий пост дня', '🏆 Best post of the day'),
    '🏆 Лучший пост недели': ('🏆 Найкращий пост тижня', '🏆 Best post of the week'),
    '🏆 Лучший пост месяца': ('🏆 Найкращий пост місяця', '🏆 Best post of the month'),
    '❌ Провальный пост дня': ('❌ Провальний пост дня', '❌ Flop of the day'),
    '❌ Провальный пост недели': ('❌ Провальний пост тижня', '❌ Flop of the week'),
    '❌ Провальный пост месяца': ('❌ Провальний пост місяця', '❌ Flop of the month'),
    '🧩 Кейс для практики': ('🧩 Кейс для практики', '🧩 Practice case'),
    'Как думаете, почему он сработал?': ('Як думаєте, чому він спрацював?', 'Why do you think it worked?'),
    'Найдёте все ошибки?': ('Знайдете всі помилки?', 'Can you spot every mistake?'),
    'Что здесь хорошо, а что плохо? Какую оценку поставите?': ('Що тут добре, а що погано? Яку оцінку поставите?', "What's good and what's bad here? What score would you give?"),
    '🔥 <b>Разборы постов</b>\n': ('🔥 <b>Розбори постів</b>\n', '🔥 <b>Post breakdowns</b>\n'),
    '\n\nРеальные публикации — лучшие и провальные — с разбором от эксперта: что сработало, где ошибка и как сделать правильно.\n\n🧩 <b>Практика:</b> оцените кейс сами — ИИ-ментор покажет, где вы правы, а что упустили.': ('\n\nРеальні публікації — найкращі та провальні — з розбором від експерта: що спрацювало, де помилка і як зробити правильно.\n\n🧩 <b>Практика:</b> оцініть кейс самі — ШІ-ментор покаже, де ви праві, а що пропустили.', '\n\nReal posts — the best and the flops — broken down by an expert: what worked, where it went wrong and how to do it right.\n\n🧩 <b>Practice:</b> rate a case yourself — the AI mentor shows what you got right and what you missed.'),
    '🏆 Пост дня': ('🏆 Пост дня', '🏆 Post of the day'),
    '📅 Пост недели': ('📅 Пост тижня', '📅 Post of the week'),
    '🗓 Пост месяца': ('🗓 Пост місяця', '🗓 Post of the month'),
    '❌ Разбор ошибок': ('❌ Розбір помилок', '❌ Mistake breakdown'),
    '🧩 Практика: оцени кейс сам': ('🧩 Практика: оціни кейс сам', '🧩 Practice: rate a case yourself'),
    '📚 Архив разборов': ('📚 Архів розборів', '📚 Breakdown archive'),
    '\n<i>📝 Учебный пример — совпадения с реальными брендами случайны</i>': ('\n<i>📝 Навчальний приклад — збіги з реальними брендами випадкові</i>', '\n<i>📝 Training example — any resemblance to real brands is coincidental</i>'),
    '</b>\nСначала подумайте сами — так навык растёт быстрее.': ('</b>\nСпочатку подумайте самі — так навичка росте швидше.', "</b>\nThink it through yourself first — that's how the skill grows fastest."),
    '✍️ Дать свою оценку': ('✍️ Дати свою оцінку', '✍️ Give my rating'),
    '🔍 Показать разбор эксперта': ('🔍 Показати розбір експерта', '🔍 Show expert breakdown'),
    'Скоро здесь появится новый разбор 🔥': ('Скоро тут зʼявиться новий розбір 🔥', 'A new breakdown is coming soon 🔥'),
    'Вы решили все кейсы 🏆 Новые появятся скоро!': ('Ви розвʼязали всі кейси 🏆 Нові зʼявляться скоро!', "You've solved every case 🏆 New ones are coming soon!"),
    'Разбор не найден': ('Розбір не знайдено', 'Breakdown not found'),
    'Архив пока пуст': ('Архів поки порожній', 'The archive is empty for now'),
    '◀️ Разборы': ('◀️ Розбори', '◀️ Breakdowns'),
    '📚 <b>Архив разборов</b>\n': ('📚 <b>Архів розборів</b>\n', '📚 <b>Breakdown archive</b>\n'),
    '\n\nПоследние кейсы:': ('\n\nОстанні кейси:', '\n\nLatest cases:'),
    '🔍 <b>Разбор эксперта</b> · ': ('🔍 <b>Розбір експерта</b> · ', '🔍 <b>Expert breakdown</b> · '),
    '🧩 Попрактиковаться на другом кейсе': ('🧩 Попрактикуватися на іншому кейсі', '🧩 Practice on another case'),
    '🧩 <b>Бесплатная практика на сегодня использована</b>\n\nВ Free — ': ('🧩 <b>Безкоштовну практику на сьогодні використано</b>\n\nУ Free — ', "🧩 <b>Today's free practice is used up</b>\n\nFree plan: "),
    ' проверка ментора в день. В PRO — без ограничений.': (' перевірка ментора на день. У PRO — без обмежень.', ' mentor review per day. PRO: unlimited.'),
    '🔍 Посмотреть разбор эксперта': ('🔍 Переглянути розбір експерта', '🔍 View expert breakdown'),
    '💎 Открыть PRO': ('💎 Відкрити PRO', '💎 Get PRO'),
    '✍️ <b>Ваша оценка</b>\n\nНапишите одним сообщением:\n• что в посте сделано хорошо\n• какие ошибки видите\n• какую оценку 1–10 поставите\n\nИИ-ментор сравнит ваш ответ с разбором эксперта.': ('✍️ <b>Ваша оцінка</b>\n\nНапишіть одним повідомленням:\n• що в пості зроблено добре\n• які помилки бачите\n• яку оцінку 1–10 поставите\n\nШІ-ментор порівняє вашу відповідь із розбором експерта.', "✍️ <b>Your rating</b>\n\nWrite one message covering:\n• what the post does well\n• which mistakes you see\n• what score you'd give, 1–10\n\nThe AI mentor will compare your answer with the expert breakdown."),
    'Кейс не найден.': ('Кейс не знайдено.', 'Case not found.'),
    '🧑\u200d🏫 Ментор проверяет ваш ответ...': ('🧑\u200d🏫 Ментор перевіряє вашу відповідь...', '🧑\u200d🏫 The mentor is reviewing your answer...'),
    '⚠️ Ментор временно недоступен. Попробуйте позже — попытка не списана.': ('⚠️ Ментор тимчасово недоступний. Спробуйте пізніше — спробу не списано.', "⚠️ The mentor is unavailable right now. Try again later — this attempt wasn't counted."),
    '🧑\u200d🏫 <b>Обратная связь ментора</b>\n': ('🧑\u200d🏫 <b>Зворотний звʼязок ментора</b>\n', '🧑\u200d🏫 <b>Mentor feedback</b>\n'),
    '🔍 Полный разбор эксперта': ('🔍 Повний розбір експерта', '🔍 Full expert breakdown'),
    '🧩 Следующий кейс': ('🧩 Наступний кейс', '🧩 Next case'),
    'Уведомления о разборах отключены': ('Сповіщення про розбори вимкнено', 'Breakdown notifications turned off'),
    '🔥 <b>Новый разбор: ': ('🔥 <b>Новий розбір: ', '🔥 <b>New breakdown: '),
    '👀 Смотреть разбор': ('👀 Дивитися розбір', '👀 View breakdown'),
    '🔕 Не присылать': ('🔕 Не надсилати', '🔕 Stop sending'),
    '☀️ <b>Вдохновение дня:</b> топовый пост и почему он работает': ('☀️ <b>Натхнення дня:</b> топовий пост і чому він працює', '☀️ <b>Inspiration of the day:</b> a top post and why it works'),
    '🌙 <b>Разбор ошибок дня:</b> почему этот пост провалился': ('🌙 <b>Розбір помилок дня:</b> чому цей пост провалився', '🌙 <b>Mistakes of the day:</b> why this post flopped'),
    '🔕 Не получать рекламу': ('🔕 Не отримувати рекламу', '🔕 No ads, please'),
    'Вы отписались от рекламных сообщений': ('Ви відписалися від рекламних повідомлень', "You've unsubscribed from promotional messages"),
    '⚠️ <b>Бесплатные генерации закончились</b>\n\nВы уже создали <b>': ('⚠️ <b>Безкоштовні генерації закінчилися</b>\n\nВи вже створили <b>', "⚠️ <b>You're out of free generations</b>\n\nYou've already created <b>"),
    '</b> материалов. Продолжите без ограничений 👇': ('</b> матеріалів. Продовжуйте без обмежень 👇', '</b> pieces. Keep going without limits 👇'),
    '⚠️ Временный сбой ИИ. Генерация не списана — попробуйте ещё раз.': ('⚠️ Тимчасовий збій ШІ. Генерацію не списано — спробуйте ще раз.', '⚠️ Temporary AI glitch. No generation was used — please try again.'),
    '</b> — работаю над вашим запросом...': ('</b> — працюю над вашим запитом...', '</b> — working on your request...'),
    'Сначала отправьте запрос 🙂': ('Спочатку надішліть запит 🙂', 'Send a request first 🙂'),
    '⏳ Дорабатываю...': ('⏳ Доопрацьовую...', '⏳ Refining...'),
    '🎙 Голосовые временно недоступны — отправьте запрос текстом.': ('🎙 Голосові тимчасово недоступні — надішліть запит текстом.', '🎙 Voice notes are unavailable right now — please send text.'),
    '🎙 <b>Слушаю голосовое...</b>': ('🎙 <b>Слухаю голосове...</b>', '🎙 <b>Listening to your voice note...</b>'),
    '⚠️ Не удалось распознать голосовое. Попробуйте ещё раз или напишите текстом.': ('⚠️ Не вдалося розпізнати голосове. Спробуйте ще раз або напишіть текстом.', "⚠️ Couldn't recognize the voice note. Try again or type it."),
    '»</i>\n\n⏳ Работаю...': ('»</i>\n\n⏳ Працюю...', '”</i>\n\n⏳ Working...'),
    '👤 Профиль и баланс': ('👤 Профіль і баланс', '👤 Profile & balance'),
    '🎯 Настроить мой бренд': ('🎯 Налаштувати мій бренд', '🎯 Set up my brand'),
    '💎 PRO-доступ': ('💎 PRO-доступ', '💎 PRO access'),
    '🆔 Мой ID': ('🆔 Мій ID', '🆔 My ID'),
    '🤝 Сотрудничество': ('🤝 Співпраця', '🤝 Partnership'),
    '\n<b>SMM Architecture AI</b> — ваш личный SMM-отдел: стратег, копирайтер, аналитик и наставник.\n\n': ('\n<b>SMM Architecture AI</b> — ваш особистий SMM-відділ: стратег, копірайтер, аналітик і наставник.\n\n', '\n<b>SMM Architecture AI</b> — your personal SMM department: strategist, copywriter, analyst and mentor.\n\n'),
    '\n🧭 <b>AI-стратег</b> — диагностика бизнеса и план роста\n✍️ <b>Контент</b> — посты, Reels, воронки, контент-план\n🕵️ <b>Конкуренты</b> — разбор и стратегия обгона\n🎓 <b>Академия</b> — курс SMM из 24 уроков · пройдено ': ('\n🧭 <b>AI-стратег</b> — діагностика бізнесу та план зростання\n✍️ <b>Контент</b> — пости, Reels, воронки, контент-план\n🕵️ <b>Конкуренти</b> — розбір і стратегія обходу\n🎓 <b>Академія</b> — курс SMM з 24 уроків · пройдено ', '\n🧭 <b>AI strategist</b> — business diagnosis and growth plan\n✍️ <b>Content</b> — posts, Reels, funnels, content plan\n🕵️ <b>Competitors</b> — breakdowns and a plan to get ahead\n🎓 <b>Academy</b> — a 24-lesson SMM course · completed '),
    '\n🔥 <b>Разборы</b> — лучшие и провальные посты каждый день\n': ('\n🔥 <b>Розбори</b> — найкращі та провальні пости щодня\n', '\n🔥 <b>Breakdowns</b> — the best and worst posts, daily\n'),
    '\n💬 Или просто напишите задачу — сейчас активен: <b>': ('\n💬 Або просто напишіть завдання — зараз активний: <b>', '\n💬 Or just type a task — active tool: <b>'),
    '🧭 AI-стратег: план роста': ('🧭 AI-стратег: план зростання', '🧭 AI strategist: growth plan'),
    '🧭 AI-стратег': ('🧭 AI-стратег', '🧭 AI strategist'),
    '✍️ Создать контент': ('✍️ Створити контент', '✍️ Create content'),
    '🕵️ Конкуренты': ('🕵️ Конкуренти', '🕵️ Competitors'),
    '🎓 Академия SMM': ('🎓 Академія SMM', '🎓 SMM Academy'),
    '🔥 Разборы постов': ('🔥 Розбори постів', '🔥 Post breakdowns'),
    '🎯 Мой бренд': ('🎯 Мій бренд', '🎯 My brand'),
    '📂 Мои материалы': ('📂 Мої матеріали', '📂 My library'),
    '📤 Мой канал': ('📤 Мій канал', '📤 My channel'),
    '👤 Профиль': ('👤 Профіль', '👤 Profile'),
    'ℹ️ О боте': ('ℹ️ Про бота', 'ℹ️ About'),
    '🎁 +Генерации': ('🎁 +Генерації', '🎁 +Credits'),
    '🌐 Язык': ('🌐 Мова', '🌐 Language'),
    '🔍 <b>Аудит текста</b>\n<i>Оценка вашего поста и улучшенная версия</i>': ('🔍 <b>Аудит тексту</b>\n<i>Оцінка вашого поста та покращена версія</i>', '🔍 <b>Copy audit</b>\n<i>A score for your post and an improved version</i>'),
    '🧭 AI-стратег без ограничений — консультации в любой момент\n🎓 Полный курс SMM: 6 модулей, 24 урока, проверка заданий, сертификат\n🕵️ До 5 конкурентов, сравнение, поиск свободной ниши и план обгона\n✍️ Все 19 инструментов без лимита: контент-план, воронки, Reels\n📤 Автопостинг в ваш канал по расписанию\n🧩 Безлимитная практика с ИИ-ментором\n\n💡 <i>Для сравнения: SMM-специалист стоит от $300 в месяц, курс SMM — от $200. PRO заменяет и то и другое.</i>': ('🧭 AI-стратег без обмежень — консультації будь-коли\n🎓 Повний курс SMM: 6 модулів, 24 уроки, перевірка завдань, сертифікат\n🕵️ До 5 конкурентів, порівняння, пошук вільної ніші та план обходу\n✍️ Усі 19 інструментів без ліміту: контент-план, воронки, Reels\n📤 Автопостинг у ваш канал за розкладом\n🧩 Безлімітна практика з ШІ-ментором\n\n💡 <i>Для порівняння: SMM-спеціаліст коштує від $300 на місяць, курс SMM — від $200. PRO замінює і те, і інше.</i>', '🧭 Unlimited AI strategist — consultations any time\n🎓 Full SMM course: 6 modules, 24 lessons, graded assignments, certificate\n🕵️ Up to 5 competitors, comparisons, niche gap analysis and a plan to get ahead\n✍️ All 19 tools, no limits: content plan, funnels, Reels\n📤 Scheduled auto-posting to your channel\n🧩 Unlimited practice with the AI mentor\n\n💡 <i>For comparison: an SMM specialist costs $300+ a month and an SMM course $200+. PRO replaces both.</i>'),
    '</b> — доступно в PRO\n': ('</b> — доступно в PRO\n', '</b> is available in PRO\n'),
    '\n\n<b>Что открывает PRO:</b>\n': ('\n\n<b>Що відкриває PRO:</b>\n', '\n\n<b>What PRO unlocks:</b>\n'),
    '💎 <b>PRO — ваш SMM-отдел в кармане</b>\n': ('💎 <b>PRO — ваш SMM-відділ у кишені</b>\n', '💎 <b>PRO — an SMM department in your pocket</b>\n'),
    '\n\n<b>Тарифы:</b>\n💎 PRO месяц — <s>700</s> <b>500 ⭐</b>\n👑 VIP год — <s>3500</s> <b>1000 ⭐</b> · выгода 70%\n📦 10 генераций — <b>50 ⭐</b>\n\n<i>Оплата Telegram Stars — безопасно, прямо в Telegram.</i>': ('\n\n<b>Тарифи:</b>\n💎 PRO місяць — <s>700</s> <b>500 ⭐</b>\n👑 VIP рік — <s>3500</s> <b>1000 ⭐</b> · вигода 70%\n📦 10 генерацій — <b>50 ⭐</b>\n\n<i>Оплата Telegram Stars — безпечно, прямо в Telegram.</i>', '\n\n<b>Plans:</b>\n💎 PRO month — <s>700</s> <b>500 ⭐</b>\n👑 VIP year — <s>3500</s> <b>1000 ⭐</b> · save 70%\n📦 10 generations — <b>50 ⭐</b>\n\n<i>Pay with Telegram Stars — safe, right inside Telegram.</i>'),
    '</b>\n🎓 Курс: <b>': ('</b>\n🎓 Курс: <b>', '</b>\n🎓 Course: <b>'),
    '</b> уроков\n': ('</b> уроків\n', '</b> lessons\n'),
    '🧭 План роста: <b>готов</b>\n': ('🧭 План зростання: <b>готовий</b>\n', '🧭 Growth plan: <b>ready</b>\n'),
    '🧭 План роста: <b>не составлен</b>\n': ('🧭 План зростання: <b>ще не складено</b>\n', '🧭 Growth plan: <b>not created yet</b>\n'),
    '👥 Приглашено друзей: <b>': ('👥 Запрошено друзів: <b>', '👥 Friends invited: <b>'),
    '⏳ Анализирую...': ('⏳ Аналізую...', '⏳ Analyzing...'),
    'ℹ️ <b>SMM Architecture AI — что это?</b>\n': ('ℹ️ <b>SMM Architecture AI — що це?</b>\n', 'ℹ️ <b>What is SMM Architecture AI?</b>\n'),
    '\n\nВаш личный SMM-отдел в Telegram: <b>стратег, копирайтер, аналитик и наставник</b> в одном боте. Работает 24/7 и знает ваш бизнес.\n\n<b>Что умеет:</b>\n🧭 <b>AI-стратег</b> — задаёт вопросы о вашем бизнесе, ставит диагноз и составляет план роста на 30 дней. Потом с ним можно советоваться, как с живым консультантом.\n✍️ <b>Контент</b> — 19 инструментов: посты, карусели, Reels, Stories, воронки, контент-план, промпты для визуала.\n🕵️ <b>Конкуренты</b> — разбор чужих аккаунтов, сравнение с вами, поиск свободной ниши и план обгона.\n🎓 <b>Академия</b> — курс «SMM: с нуля до PRO»: 6 модулей, 24 урока, задания с проверкой ИИ-ментора и сертификат.\n🔥 <b>Разборы</b> — каждый день лучшие и провальные посты с объяснением, что сработало и где ошибка.\n📤 <b>Автопостинг</b> — публикация в ваш Telegram-канал сразу или по расписанию.\n\n<b>Для кого:</b>\n• владельцы бизнеса и эксперты, которые ведут соцсети сами;\n• SMM-специалисты — делать работу в 5 раз быстрее и брать больше клиентов;\n• новички — освоить профессию SMM с нуля.\n\n<b>С чего начать — 3 шага:</b>\n1️⃣ Пройдите диагностику у AI-стратега (2 минуты).\n2️⃣ Получите план роста и первые задачи на сегодня.\n3️⃣ Создавайте контент по плану и учитесь в Академии.\n\n<b>Бесплатно:</b> диагностика, первый модуль курса, разборы постов, 3 генерации.\n<b>PRO:</b> всё без ограничений.': ('\n\nВаш особистий SMM-відділ у Telegram: <b>стратег, копірайтер, аналітик і наставник</b> в одному боті. Працює 24/7 і знає ваш бізнес.\n\n<b>Що вміє:</b>\n🧭 <b>AI-стратег</b> — ставить питання про ваш бізнес, ставить діагноз і складає план зростання на 30 днів. Потім із ним можна радитися, як із живим консультантом.\n✍️ <b>Контент</b> — 19 інструментів: пости, каруселі, Reels, Stories, воронки, контент-план, промпти для візуалу.\n🕵️ <b>Конкуренти</b> — розбір чужих акаунтів, порівняння з вами, пошук вільної ніші та план обходу.\n🎓 <b>Академія</b> — курс «SMM: з нуля до PRO»: 6 модулів, 24 уроки, завдання з перевіркою ШІ-ментора та сертифікат.\n🔥 <b>Розбори</b> — щодня найкращі та провальні пости з поясненням, що спрацювало і де помилка.\n📤 <b>Автопостинг</b> — публікація у ваш Telegram-канал одразу або за розкладом.\n\n<b>Для кого:</b>\n• власники бізнесу та експерти, які ведуть соцмережі самі;\n• SMM-спеціалісти — робити роботу в 5 разів швидше і брати більше клієнтів;\n• новачки — опанувати професію SMM з нуля.\n\n<b>З чого почати — 3 кроки:</b>\n1️⃣ Пройдіть діагностику в AI-стратега (2 хвилини).\n2️⃣ Отримайте план зростання та перші завдання на сьогодні.\n3️⃣ Створюйте контент за планом і навчайтеся в Академії.\n\n<b>Безкоштовно:</b> діагностика, перший модуль курсу, розбори постів, 3 генерації.\n<b>PRO:</b> усе без обмежень.', "\n\nYour personal SMM department in Telegram: <b>strategist, copywriter, analyst and mentor</b> in one bot. Works 24/7 and knows your business.\n\n<b>What it does:</b>\n🧭 <b>AI strategist</b> — asks about your business, diagnoses it and builds a 30-day growth plan. Then you can consult it like a real advisor.\n✍️ <b>Content</b> — 19 tools: posts, carousels, Reels, Stories, funnels, content plans, visual prompts.\n🕵️ <b>Competitors</b> — breakdowns of other accounts, comparison with you, niche gap analysis and a plan to get ahead.\n🎓 <b>Academy</b> — the “SMM: Zero to PRO” course: 6 modules, 24 lessons, assignments graded by an AI mentor, and a certificate.\n🔥 <b>Breakdowns</b> — the best and worst posts every day, with what worked and what went wrong.\n📤 <b>Auto-posting</b> — publish to your Telegram channel now or on schedule.\n\n<b>Who it's for:</b>\n• business owners and experts who run their own social media;\n• SMM specialists — work 5× faster and take on more clients;\n• beginners — learn the SMM profession from scratch.\n\n<b>How to start — 3 steps:</b>\n1️⃣ Take the AI strategist's diagnosis (2 minutes).\n2️⃣ Get your growth plan and today's first tasks.\n3️⃣ Create content by the plan and study in the Academy.\n\n<b>Free:</b> diagnosis, the first course module, post breakdowns, 3 generations.\n<b>PRO:</b> everything, unlimited."),
    '🧭 Пройти диагностику — 2 мин': ('🧭 Пройти діагностику — 2 хв', '🧭 Take the diagnosis — 2 min'),
    '🎓 Начать обучение': ('🎓 Почати навчання', '🎓 Start learning'),
    '👋 <b>Добро пожаловать в SMM Architecture AI!</b>\n\nЯ — ваш AI-маркетолог: помогу понять, <b>как продвигать именно ваш бизнес</b>, напишу контент, разберу конкурентов и научу SMM с нуля.\n\n🎁 Для старта у вас <b>3 бесплатные генерации</b>, бесплатная диагностика и первый модуль курса.\n\n<b>С чего лучше начать?</b>\n🧭 Если у вас есть бизнес или блог — пройдите диагностику: я задам 7 вопросов и составлю план роста.\n🎓 Если хотите освоить SMM — начните с курса.': ('👋 <b>Ласкаво просимо до SMM Architecture AI!</b>\n\nЯ — ваш AI-маркетолог: допоможу зрозуміти, <b>як просувати саме ваш бізнес</b>, напишу контент, розберу конкурентів і навчу SMM з нуля.\n\n🎁 Для старту у вас <b>3 безкоштовні генерації</b>, безкоштовна діагностика та перший модуль курсу.\n\n<b>З чого краще почати?</b>\n🧭 Якщо у вас є бізнес або блог — пройдіть діагностику: я поставлю 7 запитань і складу план зростання.\n🎓 Якщо хочете опанувати SMM — почніть із курсу.', "👋 <b>Welcome to SMM Architecture AI!</b>\n\nI'm your AI marketer: I'll help you figure out <b>how to promote your business specifically</b>, write content, break down competitors and teach you SMM from scratch.\n\n🎁 To start, you get <b>3 free generations</b>, a free diagnosis and the first course module.\n\n<b>Where to begin?</b>\n🧭 Have a business or blog? Take the diagnosis: I'll ask 7 questions and build a growth plan.\n🎓 Want to learn SMM? Start with the course."),
    '🎓 Начать обучение SMM': ('🎓 Почати навчання SMM', '🎓 Start learning SMM'),
    'ℹ️ Что умеет бот': ('ℹ️ Що вміє бот', 'ℹ️ What the bot does'),
    'Чем вы занимаетесь? Что продаёте и где работаете (город или онлайн)?': ('Чим ви займаєтесь? Що продаєте і де працюєте (місто чи онлайн)?', 'What do you do? What do you sell and where (city or online)?'),
    'Например: «кондитерская на заказ в Днепре, торты и десерты»': ('Наприклад: «кондитерська на замовлення в Дніпрі, торти й десерти»', 'For example: “custom bakery in Dnipro, cakes and desserts”'),
    'Сколько стоит ваш продукт и как сейчас приходят клиенты?': ('Скільки коштує ваш продукт і як зараз приходять клієнти?', 'How much does your product cost and how do customers find you now?'),
    'Например: «средний чек 1500 грн, клиенты в основном по сарафанному радио»': ('Наприклад: «середній чек 1500 грн, клієнти здебільшого за рекомендаціями»', 'For example: “average order 1500 UAH, mostly word of mouth”'),
    'Кто ваш идеальный клиент?': ('Хто ваш ідеальний клієнт?', 'Who is your ideal customer?'),
    'Например: «мамы 25–40 лет, заказывают торты на дни рождения детей»': ('Наприклад: «мами 25–40 років, замовляють торти на дні народження дітей»', "For example: “moms aged 25–40 ordering cakes for kids' birthdays”"),
    'Какие соцсети вы ведёте, сколько подписчиков и как часто публикуете?': ('Які соцмережі ви ведете, скільки підписників і як часто публікуєте?', 'Which social networks do you run, how many followers, and how often do you post?'),
    'Например: «Instagram 800 подписчиков, пост раз в неделю» или «начинаю с нуля»': ('Наприклад: «Instagram 800 підписників, пост раз на тиждень» або «починаю з нуля»', 'For example: “Instagram, 800 followers, one post a week” or “starting from zero”'),
    'Какая главная цель на ближайшие 3 месяца?': ('Яка головна ціль на найближчі 3 місяці?', "What's your main goal for the next 3 months?"),
    'Например: «20 заказов в месяц из Instagram» или «запустить онлайн-курс»': ('Наприклад: «20 замовлень на місяць з Instagram» або «запустити онлайн-курс»', 'For example: “20 orders a month from Instagram” or “launch an online course”'),
    'Что сейчас не получается? Главная проблема в продвижении?': ('Що зараз не виходить? Головна проблема в просуванні?', "What isn't working right now? Your main marketing problem?"),
    'Например: «мало охватов, посты никто не видит» или «не знаю, что публиковать»': ('Наприклад: «мало охоплень, пости ніхто не бачить» або «не знаю, що публікувати»', "For example: “low reach, nobody sees my posts” or “I don't know what to post”"),
    'Сколько времени и денег вы готовы вкладывать в продвижение?': ('Скільки часу та грошей ви готові вкладати в просування?', 'How much time and money can you invest in promotion?'),
    'Например: «1 час в день, рекламный бюджет до 3000 грн в месяц»': ('Наприклад: «1 година на день, рекламний бюджет до 3000 грн на місяць»', 'For example: “1 hour a day, ad budget up to 3000 UAH a month”'),
    '\n\nЯ проведу <b>диагностику вашего бизнеса</b>, как это делает дорогой консультант:\n\n1️⃣ Задам 7 вопросов — о продукте, клиентах, соцсетях и целях.\n2️⃣ Найду, что мешает расти.\n3️⃣ Составлю <b>персональную стратегию</b>: позиционирование, портрет клиента, план на 30 дней и 3 действия на сегодня.\n4️⃣ Дальше — советуйтесь со мной в любой момент, я помню ваш бизнес.\n\n⏱ Займёт 2–3 минуты. Первая диагностика — бесплатно.': ('\n\nЯ проведу <b>діагностику вашого бізнесу</b>, як це робить дорогий консультант:\n\n1️⃣ Поставлю 7 запитань — про продукт, клієнтів, соцмережі та цілі.\n2️⃣ Знайду, що заважає рости.\n3️⃣ Складу <b>персональну стратегію</b>: позиціонування, портрет клієнта, план на 30 днів і 3 дії на сьогодні.\n4️⃣ Далі — радьтеся зі мною будь-коли, я памʼятаю ваш бізнес.\n\n⏱ Займе 2–3 хвилини. Перша діагностика — безкоштовно.', "\n\nI'll run a <b>diagnosis of your business</b>, the way an expensive consultant would:\n\n1️⃣ Ask 7 questions — about your product, customers, social media and goals.\n2️⃣ Find what's holding you back.\n3️⃣ Build a <b>personal strategy</b>: positioning, customer profile, a 30-day plan and 3 actions for today.\n4️⃣ After that, consult me any time — I remember your business.\n\n⏱ Takes 2–3 minutes. The first diagnosis is free."),
    '🚀 Начать диагностику': ('🚀 Почати діагностику', '🚀 Start the diagnosis'),
    '🧭 <b>AI-стратег</b>\n': ('🧭 <b>AI-стратег</b>\n', '🧭 <b>AI strategist</b>\n'),
    '\n\n✅ Диагностика пройдена, ваш план роста готов.\n\n💬 <b>Консультация</b> — спросите что угодно о продвижении: как поднять охваты, что публиковать, сколько вкладывать в рекламу, как поднять чек. Я отвечу с учётом вашего бизнеса.\n\n🔄 Бизнес изменился? Пройдите диагностику заново.': ('\n\n✅ Діагностику пройдено, ваш план зростання готовий.\n\n💬 <b>Консультація</b> — запитайте що завгодно про просування: як підняти охоплення, що публікувати, скільки вкладати в рекламу, як підняти чек. Я відповім з урахуванням вашого бізнесу.\n\n🔄 Бізнес змінився? Пройдіть діагностику заново.', "\n\n✅ Diagnosis complete — your growth plan is ready.\n\n💬 <b>Consultation</b> — ask anything about promotion: how to grow reach, what to post, how much to spend on ads, how to raise your average order. I'll answer with your business in mind.\n\n🔄 Business changed? Take the diagnosis again."),
    '📋 Мой план роста': ('📋 Мій план зростання', '📋 My growth plan'),
    '💬 Консультация': ('💬 Консультація', '💬 Consultation'),
    '🔄 Пройти заново': ('🔄 Пройти заново', '🔄 Start over'),
    '🧭 <b>Вопрос ': ('🧭 <b>Питання ', '🧭 <b>Question '),
    '</i>\n\n💬 Можно ответить текстом в свободной форме.': ('</i>\n\n💬 Можна відповісти текстом у вільній формі.', '</i>\n\n💬 Answer in your own words.'),
    '🧠 <b>Анализирую ваш бизнес...</b>\nСоставляю стратегию — это займёт до минуты.': ('🧠 <b>Аналізую ваш бізнес...</b>\nСкладаю стратегію — це займе до хвилини.', '🧠 <b>Analyzing your business...</b>\nBuilding your strategy — this takes up to a minute.'),
    '🧭 Ваша стратегия роста': ('🧭 Ваша стратегія зростання', '🧭 Your growth strategy'),
    '💬 Обсудить со стратегом': ('💬 Обговорити зі стратегом', '💬 Discuss with the strategist'),
    'Сначала пройдите диагностику 🙂': ('Спочатку пройдіть діагностику 🙂', 'Take the diagnosis first 🙂'),
    '🧭 <b>Ваш план роста</b>\n': ('🧭 <b>Ваш план зростання</b>\n', '🧭 <b>Your growth plan</b>\n'),
    '◀️ AI-стратег': ('◀️ AI-стратег', '◀️ AI strategist'),
    '💬 Чтобы советы были точными, сначала пройдите диагностику — это 2 минуты.': ('💬 Щоб поради були точними, спочатку пройдіть діагностику — це 2 хвилини.', "💬 For accurate advice, take the diagnosis first — it's 2 minutes."),
    '💬 <b>Стратег на связи</b>\n': ('💬 <b>Стратег на звʼязку</b>\n', '💬 <b>Your strategist is here</b>\n'),
    '\n\nЗадайте любой вопрос о продвижении — я знаю ваш бизнес и план.\n\n<i>Например: «Почему падают охваты?», «Сколько вкладывать в рекламу?», «Как поднять средний чек?»</i>': ('\n\nСтавте будь-яке питання про просування — я знаю ваш бізнес і план.\n\n<i>Наприклад: «Чому падають охоплення?», «Скільки вкладати в рекламу?», «Як підняти середній чек?»</i>', '\n\nAsk anything about promotion — I know your business and plan.\n\n<i>For example: “Why is my reach dropping?”, “How much should I spend on ads?”, “How do I raise my average order?”</i>'),
    '🏁 Завершить консультацию': ('🏁 Завершити консультацію', '🏁 End consultation'),
    '✅ Консультация завершена. Возвращайтесь в любой момент!\n\n': ('✅ Консультацію завершено. Повертайтеся будь-коли!\n\n', '✅ Consultation ended. Come back any time!\n\n'),
    '🧭 Думаю над ответом...': ('🧭 Думаю над відповіддю...', '🧭 Thinking it over...'),
    '🕵️ <b>Анализ конкурентов</b>\n': ('🕵️ <b>Аналіз конкурентів</b>\n', '🕵️ <b>Competitor analysis</b>\n'),
    '\n\nДобавьте конкурентов — и я разберу их стратегию: позиционирование, оффер, контент, воронку, сильные и слабые стороны.\n\n🔍 <b>Разбор</b> — что работает у конкурента и что взять себе\n⚔️ <b>Сравнение со мной</b> — где вы сильнее, где отстаёте\n🕳 <b>Свободная ниша</b> — что упускают все конкуренты\n🚀 <b>План обгона</b> — стратегия на 30 дней\n\n📋 Конкурентов: <b>': ('\n\nДодайте конкурентів — і я розберу їхню стратегію: позиціонування, офер, контент, воронку, сильні та слабкі сторони.\n\n🔍 <b>Розбір</b> — що працює в конкурента і що взяти собі\n⚔️ <b>Порівняння зі мною</b> — де ви сильніші, де відстаєте\n🕳 <b>Вільна ніша</b> — що пропускають усі конкуренти\n🚀 <b>План обходу</b> — стратегія на 30 днів\n\n📋 Конкурентів: <b>', "\n\nAdd competitors and I'll break down their strategy: positioning, offer, content, funnel, strengths and weaknesses.\n\n🔍 <b>Breakdown</b> — what works for them and what to borrow\n⚔️ <b>Compare with me</b> — where you're ahead and where you lag\n🕳 <b>Niche gap</b> — what every competitor misses\n🚀 <b>Plan to get ahead</b> — a 30-day strategy\n\n📋 Competitors: <b>"),
    '</b>\n<i>Бот не заходит в чужие аккаунты: анализ строится на данных, которые вы пришлёте.</i>': ('</b>\n<i>Бот не заходить у чужі акаунти: аналіз будується на даних, які ви надішлете.</i>', "</b>\n<i>The bot doesn't access other accounts: the analysis is based on the data you send.</i>"),
    '➕ Добавить конкурента': ('➕ Додати конкурента', '➕ Add a competitor'),
    '🔒 До 5 конкурентов — в PRO': ('🔒 До 5 конкурентів — у PRO', '🔒 Up to 5 competitors in PRO'),
    '🕳 Свободная ниша': ('🕳 Вільна ніша', '🕳 Niche gap'),
    '🚀 План обгона': ('🚀 План обходу', '🚀 Plan to get ahead'),
    'Достигнут лимит конкурентов': ('Досягнуто ліміту конкурентів', 'Competitor limit reached'),
    '🕵️ <b>Шаг 1 из 2</b>\n\nКак называется конкурент? Можно @никнейм или ссылку на профиль.': ('🕵️ <b>Крок 1 з 2</b>\n\nЯк називається конкурент? Можна @нікнейм або посилання на профіль.', "🕵️ <b>Step 1 of 2</b>\n\nWhat's the competitor's name? An @username or profile link works too."),
    '🕵️ <b>Шаг 2 из 2</b>\n\nПришлите всё, что знаете о конкуренте, <b>одним сообщением</b>:\n• шапку профиля (bio)\n• что продаёт и по каким ценам\n• 2–3 текста его постов\n• сколько подписчиков, что вам у него нравится\n\n<i>Чем больше данных — тем точнее разбор. Можно просто переслать его пост.</i>': ('🕵️ <b>Крок 2 з 2</b>\n\nНадішліть усе, що знаєте про конкурента, <b>одним повідомленням</b>:\n• шапку профілю (bio)\n• що продає і за якими цінами\n• 2–3 тексти його постів\n• скільки підписників, що вам у нього подобається\n\n<i>Що більше даних — то точніший розбір. Можна просто переслати його пост.</i>', '🕵️ <b>Step 2 of 2</b>\n\nSend everything you know about the competitor <b>in one message</b>:\n• their profile bio\n• what they sell and at what prices\n• 2–3 of their posts\n• follower count and what you like about them\n\n<i>The more data, the sharper the breakdown. You can simply forward their post.</i>'),
    '✅ Конкурент <b>': ('✅ Конкурента <b>', '✅ Competitor <b>'),
    '</b> добавлен.': ('</b> додано.', '</b> added.'),
    '🔍 Сделать разбор': ('🔍 Зробити розбір', '🔍 Run a breakdown'),
    'Конкурент не найден': ('Конкурента не знайдено', 'Competitor not found'),
    '🔍 Полный разбор': ('🔍 Повний розбір', '🔍 Full breakdown'),
    '⚔️ Сравнить со мной': ('⚔️ Порівняти зі мною', '⚔️ Compare with me'),
    '◀️ Конкуренты': ('◀️ Конкуренти', '◀️ Competitors'),
    '🔍 Разбор конкурента: ': ('🔍 Розбір конкурента: ', '🔍 Competitor breakdown: '),
    '⚔️ Для сравнения мне нужно знать ваш бизнес. Пройдите диагностику у AI-стратега — 2 минуты.': ('⚔️ Для порівняння мені потрібно знати ваш бізнес. Пройдіть діагностику в AI-стратега — 2 хвилини.', "⚔️ To compare, I need to know your business. Take the AI strategist's diagnosis — 2 minutes."),
    '⚔️ Вы vs ': ('⚔️ Ви vs ', '⚔️ You vs '),
    '⚔️ Сравнение с конкурентом': ('⚔️ Порівняння з конкурентом', '⚔️ Competitor comparison'),
    '🕳 Свободная ниша и УТП': ('🕳 Вільна ніша та УТП', '🕳 Niche gap & USP'),
    '🕳 Поиск свободной ниши': ('🕳 Пошук вільної ніші', '🕳 Niche gap analysis'),
    '🚀 План обгона конкурентов': ('🚀 План обходу конкурентів', '🚀 Plan to beat competitors'),
    '🧱 Фундамент SMM': ('🧱 Фундамент SMM', '🧱 SMM fundamentals'),
    'Как работает SMM, целевая аудитория, позиционирование и упаковка профиля.': ('Як працює SMM, цільова аудиторія, позиціонування та упаковка профілю.', 'How SMM works, target audience, positioning and profile packaging.'),
    'Как работает SMM и зачем он бизнесу': ('Як працює SMM і навіщо він бізнесу', 'How SMM works and why businesses need it'),
    'Целевая аудитория: портрет, боли и желания': ('Цільова аудиторія: портрет, болі та бажання', 'Target audience: profile, pains and desires'),
    'Позиционирование и УТП': ('Позиціонування та УТП', 'Positioning and USP'),
    'Упаковка профиля, который продаёт': ('Упаковка профілю, який продає', 'A profile that sells'),
    '✍️ Контент, который читают': ('✍️ Контент, який читають', '✍️ Content people read'),
    'Контент-стратегия, хуки и копирайтинг, Reels, Stories и прогревы.': ('Контент-стратегія, хуки та копірайтинг, Reels, Stories і прогріви.', 'Content strategy, hooks and copywriting, Reels, Stories and warm-ups.'),
    'Контент-стратегия и рубрикатор': ('Контент-стратегія та рубрикатор', 'Content strategy and content pillars'),
    'Хуки и копирайтинг': ('Хуки та копірайтинг', 'Hooks and copywriting'),
    'Reels и короткие видео': ('Reels і короткі відео', 'Reels and short videos'),
    'Stories и прогревы': ('Stories і прогріви', 'Stories and warm-ups'),
    '📈 Рост и алгоритмы': ('📈 Зростання та алгоритми', '📈 Growth and algorithms'),
    'Как работают алгоритмы, органический рост, комьюнити и аналитика.': ('Як працюють алгоритми, органічне зростання, комʼюніті та аналітика.', 'How algorithms work, organic growth, community and analytics.'),
    'Как работают алгоритмы Instagram, TikTok и Telegram': ('Як працюють алгоритми Instagram, TikTok і Telegram', 'How Instagram, TikTok and Telegram algorithms work'),
    'Органический рост без бюджета': ('Органічне зростання без бюджету', 'Organic growth with no budget'),
    'Вовлечение и комьюнити': ('Залучення та комʼюніті', 'Engagement and community'),
    'Аналитика и метрики': ('Аналітика та метрики', 'Analytics and metrics'),
    '💰 Продажи через соцсети': ('💰 Продажі через соцмережі', '💰 Selling through social media'),
    'Воронки, офферы, продажи в директе и запуски.': ('Воронки, офери, продажі в директі та запуски.', 'Funnels, offers, DM sales and launches.'),
    'Воронка продаж в соцсетях': ('Воронка продажів у соцмережах', 'The social media sales funnel'),
    'Офферы, от которых сложно отказаться': ('Офери, від яких важко відмовитися', 'Offers that are hard to refuse'),
    'Продажи в директе и возражения': ('Продажі в директі та заперечення', 'DM sales and objections'),
    'Запуски и акции': ('Запуски та акції', 'Launches and promotions'),
    '🎯 Реклама и масштабирование': ('🎯 Реклама та масштабування', '🎯 Ads and scaling'),
    'Таргет, креативы, реклама у блогеров и юнит-экономика.': ('Таргет, креативи, реклама в блогерів і юніт-економіка.', 'Targeting, creatives, influencer ads and unit economics.'),
    'Таргетированная реклама: основы': ('Таргетована реклама: основи', 'Targeted ads: the basics'),
    'Креативы, которые конвертируют': ('Креативи, які конвертують', 'Creatives that convert'),
    'Реклама у блогеров': ('Реклама в блогерів', 'Influencer advertising'),
    'Юнит-экономика рекламы': ('Юніт-економіка реклами', 'Ad unit economics'),
    '🚀 SMM как профессия': ('🚀 SMM як професія', '🚀 SMM as a profession'),
    'Первые клиенты, упаковка услуги, работа с клиентом и своё агентство.': ('Перші клієнти, упаковка послуги, робота з клієнтом і власна агенція.', 'First clients, packaging your service, client work and your own agency.'),
    'Как найти первых клиентов': ('Як знайти перших клієнтів', 'How to find your first clients'),
    'Упаковка услуги и цены': ('Упаковка послуги та ціни', 'Packaging your service and pricing'),
    'Работа с клиентом: бриф, отчёты, удержание': ('Робота з клієнтом: бриф, звіти, утримання', 'Client work: briefs, reports, retention'),
    'От фрилансера к агентству': ('Від фрилансера до агенції', 'From freelancer to agency'),
    '\n\n📚 <b>Курс «SMM: с нуля до PRO»</b> — 6 модулей и 24 урока: от целевой аудитории до рекламы и своего агентства. После каждого урока — задание, которое проверяет ИИ-ментор, а в конце — сертификат.\n\n🧪 <b>Тесты</b> — проверьте знания за минуту.\n🔥 <b>Разборы постов</b> — учитесь на чужих успехах и ошибках.\n🔍 <b>Проверка вашего текста</b> — оценка и улучшенная версия.\n\n🎁 Первый модуль — бесплатно.': ('\n\n📚 <b>Курс «SMM: з нуля до PRO»</b> — 6 модулів і 24 уроки: від цільової аудиторії до реклами та власної агенції. Після кожного уроку — завдання, яке перевіряє ШІ-ментор, а наприкінці — сертифікат.\n\n🧪 <b>Тести</b> — перевірте знання за хвилину.\n🔥 <b>Розбори постів</b> — навчайтеся на чужих успіхах і помилках.\n🔍 <b>Перевірка вашого тексту</b> — оцінка та покращена версія.\n\n🎁 Перший модуль — безкоштовно.', "\n\n📚 <b>The “SMM: Zero to PRO” course</b> — 6 modules and 24 lessons: from target audience to ads and your own agency. Every lesson ends with an assignment graded by the AI mentor, and you get a certificate at the end.\n\n🧪 <b>Quizzes</b> — test your knowledge in a minute.\n🔥 <b>Post breakdowns</b> — learn from others' wins and mistakes.\n🔍 <b>Copy check</b> — a score and an improved version.\n\n🎁 The first module is free."),
    '📚 Курс «SMM: с нуля до PRO»': ('📚 Курс «SMM: з нуля до PRO»', '📚 “SMM: Zero to PRO” course'),
    '🔍 Проверить мой текст': ('🔍 Перевірити мій текст', '🔍 Check my copy'),
    '📚 <b>Курс «SMM: с нуля до PRO»</b>\n': ('📚 <b>Курс «SMM: з нуля до PRO»</b>\n', '📚 <b>“SMM: Zero to PRO” course</b>\n'),
    '\n\n6 модулей · ': ('\n\n6 модулів · ', '\n\n6 modules · '),
    ' урока · задания с проверкой ИИ-ментора · сертификат\n\n📊 Прогресс: <code>': (' уроки · завдання з перевіркою ШІ-ментора · сертифікат\n\n📊 Прогрес: <code>', ' lessons · assignments graded by the AI mentor · certificate\n\n📊 Progress: <code>'),
    ' · бесплатно': (' · безкоштовно', ' · free'),
    '🎓 Получить сертификат': ('🎓 Отримати сертифікат', '🎓 Get certificate'),
    '◀️ Курс': ('◀️ Курс', '◀️ Course'),
    '📝 Сдать задание на проверку': ('📝 Здати завдання на перевірку', '📝 Submit assignment'),
    '🎯 Применить к моему бизнесу': ('🎯 Застосувати до мого бізнесу', '🎯 Apply to my business'),
    '✅ Урок пройден → дальше': ('✅ Урок пройдено → далі', '✅ Lesson done → next'),
    '◀️ К модулю': ('◀️ До модуля', '◀️ Back to module'),
    '📖 Открываю урок...': ('📖 Відкриваю урок...', '📖 Opening the lesson...'),
    '⚠️ Не удалось загрузить урок. Попробуйте ещё раз через минуту.': ('⚠️ Не вдалося завантажити урок. Спробуйте ще раз за хвилину.', "⚠️ Couldn't load the lesson. Please try again in a minute."),
    '🎓 <b>Модуль ': ('🎓 <b>Модуль ', '🎓 <b>Module '),
    ' · Урок ': (' · Урок ', ' · Lesson '),
    'Урок засчитан ✅': ('Урок зараховано ✅', 'Lesson completed ✅'),
    '🎉 <b>Вы прошли весь курс!</b>': ('🎉 <b>Ви пройшли весь курс!</b>', "🎉 <b>You've completed the whole course!</b>"),
    '📝 <b>Отправьте выполненное задание</b> одним сообщением.\n\nИИ-ментор проверит его, поставит оценку и покажет, как сделать на 10/10. При оценке от 7 урок засчитается автоматически.': ('📝 <b>Надішліть виконане завдання</b> одним повідомленням.\n\nШІ-ментор перевірить його, поставить оцінку й покаже, як зробити на 10/10. За оцінки від 7 урок зарахується автоматично.', '📝 <b>Send your completed assignment</b> in one message.\n\nThe AI mentor will grade it and show you how to make it a 10/10. With a score of 7 or higher, the lesson is completed automatically.'),
    '🧑\u200d🏫 Ментор проверяет задание...': ('🧑\u200d🏫 Ментор перевіряє завдання...', '🧑\u200d🏫 The mentor is reviewing your assignment...'),
    '🧑\u200d🏫 <b>Проверка задания</b> · ': ('🧑\u200d🏫 <b>Перевірка завдання</b> · ', '🧑\u200d🏫 <b>Assignment review</b> · '),
    '🔄 Сдать ещё раз': ('🔄 Здати ще раз', '🔄 Submit again'),
    '✅ Урок засчитан → следующий': ('✅ Урок зараховано → наступний', '✅ Lesson done → next'),
    '🎯 Чтобы применить урок к вашему бизнесу, мне нужно его знать. Пройдите диагностику — 2 минуты.': ('🎯 Щоб застосувати урок до вашого бізнесу, мені потрібно його знати. Пройдіть діагностику — 2 хвилини.', '🎯 To apply the lesson to your business, I need to know it. Take the diagnosis — 2 minutes.'),
    ' — для вашего бизнеса': (' — для вашого бізнесу', ' — for your business'),
    '🎯 Персональное применение уроков': ('🎯 Персональне застосування уроків', '🎯 Personalized lesson application'),
    'Сертификат откроется после прохождения всех уроков 🎓': ('Сертифікат відкриється після проходження всіх уроків 🎓', 'The certificate unlocks once you finish every lesson 🎓'),
    '🎓 <b>СЕРТИФИКАТ</b>\n━━━━━━━━━━━━━━━━━━━━\n\nНастоящим подтверждается, что\n<b>': ('🎓 <b>СЕРТИФІКАТ</b>\n━━━━━━━━━━━━━━━━━━━━\n\nЦим підтверджується, що\n<b>', '🎓 <b>CERTIFICATE</b>\n━━━━━━━━━━━━━━━━━━━━\n\nThis certifies that\n<b>'),
    '</b>\n\nуспешно прошёл(-ла) курс\n<b>«SMM: с нуля до PRO»</b>\n6 модулей · ': ('</b>\n\nуспішно пройшов(-ла) курс\n<b>«SMM: з нуля до PRO»</b>\n6 модулів · ', '</b>\n\nhas successfully completed the course\n<b>“SMM: Zero to PRO”</b>\n6 modules · '),
    ' урока · практические задания\n\n📅 ': (' уроки · практичні завдання\n\n📅 ', ' lessons · practical assignments\n\n📅 '),
    '🎓 Курс SMM': ('🎓 Курс SMM', '🎓 SMM course'),
}

LANG_INDEX = {"uk": 0, "en": 1}
# длинные фразы заменяем первыми, чтобы короткие не «съели» часть длинных
_I18N_KEYS = sorted(I18N, key=len, reverse=True)
_I18N_RE = re.compile("|".join(re.escape(k) for k in _I18N_KEYS))


def tr(lang: str, text):
    """Переводит русский текст интерфейса на язык пользователя."""
    idx = LANG_INDEX.get(lang)
    if idx is None or not text or not isinstance(text, str):
        return text
    return _I18N_RE.sub(lambda m: I18N[m.group(0)][idx], text)


# --- автоматический перевод всего, что бот отправляет в Telegram ---
current_lang: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_lang", default=None)


def _tr_markup(markup, lang: str):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            upd = {"text": tr(lang, b.text)}
            if b.url:
                upd["url"] = tr(lang, b.url)
            new_row.append(b.model_copy(update=upd))
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TranslateMiddleware(BaseRequestMiddleware):
    """Подменяет русские тексты интерфейса на язык получателя перед отправкой."""

    async def __call__(self, make_request, bot, method):
        try:
            chat_id = getattr(method, "chat_id", None)
            if isinstance(chat_id, int):
                # личный чат пользователя — берём его язык; каналы и группы не трогаем
                lang = user_data[chat_id].get("lang") if chat_id in user_data else None
            else:
                lang = current_lang.get()  # ответы на нажатия кнопок
            if lang in LANG_INDEX:
                upd = {}
                for field in ("text", "caption", "title", "description"):
                    value = getattr(method, field, None)
                    if isinstance(value, str):
                        translated = tr(lang, value)
                        if translated != value:
                            upd[field] = translated
                markup = getattr(method, "reply_markup", None)
                if isinstance(markup, InlineKeyboardMarkup):
                    upd["reply_markup"] = _tr_markup(markup, lang)
                prices = getattr(method, "prices", None)
                if prices:
                    upd["prices"] = [p.model_copy(update={"label": tr(lang, p.label)}) for p in prices]
                if upd:
                    method = method.model_copy(update=upd)
        except Exception as e:  # перевод никогда не должен ломать отправку
            logging.warning(f"Translate error: {e}")
        return await make_request(bot, method)


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
    u.setdefault("biz", {})            # ответы на диагностику AI-стратега
    u.setdefault("growth_plan", "")    # персональная стратегия роста
    u.setdefault("strat_chat", [])     # история консультации со стратегом
    u.setdefault("competitors", [])    # конкуренты для анализа
    u.setdefault("course_done", [])    # пройденные уроки курса
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
    done = len(u.get("course_done", []))

    text = (
        f"{t['hello'].format(name=esc(user.first_name))}\n"
        "<b>SMM Architecture AI</b> — ваш личный SMM-отдел: стратег, копирайтер, аналитик и наставник.\n\n"
        f"{status_line(user.id, u)}\n"
        f"{LINE}\n"
        "🧭 <b>AI-стратег</b> — диагностика бизнеса и план роста\n"
        "✍️ <b>Контент</b> — посты, Reels, воронки, контент-план\n"
        "🕵️ <b>Конкуренты</b> — разбор и стратегия обгона\n"
        f"🎓 <b>Академия</b> — курс SMM из 24 уроков · пройдено {done}/{COURSE_TOTAL}\n"
        "🔥 <b>Разборы</b> — лучшие и провальные посты каждый день\n"
        f"{LINE}\n"
        f"💬 Или просто напишите задачу — сейчас активен: <b>{tool['name']}</b>"
    )
    rows = [[btn("🧭 AI-стратег: план роста" if not u.get("growth_plan") else "🧭 AI-стратег", "strat")]]
    if not u["trial_used"] and not is_sub_active(user.id, u):
        rows.append([btn(f"🎁 {TRIAL_DAYS} дня PRO бесплатно", "trial")])
    rows += [
        [btn("✍️ Создать контент", "create"), btn("🕵️ Конкуренты", "comp")],
        [btn("🎓 Академия SMM", "academy_menu"), btn("🔥 Разборы постов", "cases")],
        [btn("🎯 Мой бренд", "brand"), btn("📂 Мои материалы", "saved")],
        [btn("📤 Мой канал", "channel"), btn("👤 Профиль", "profile")],
        [btn("💎 PRO-доступ", "buy_menu"), btn("ℹ️ О боте", "about")],
        [btn("🎁 +Генерации", "ref_menu"), btn("🤝 Сотрудничество", "partner")],
        [btn("🌐 Язык", "lang_menu")],
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


PRO_VALUE = (
    "🧭 AI-стратег без ограничений — консультации в любой момент\n"
    "🎓 Полный курс SMM: 6 модулей, 24 урока, проверка заданий, сертификат\n"
    "🕵️ До 5 конкурентов, сравнение, поиск свободной ниши и план обгона\n"
    "✍️ Все 19 инструментов без лимита: контент-план, воронки, Reels\n"
    "📤 Автопостинг в ваш канал по расписанию\n"
    "🧩 Безлимитная практика с ИИ-ментором\n\n"
    "💡 <i>Для сравнения: SMM-специалист стоит от $300 в месяц, курс SMM — от $200. "
    "PRO заменяет и то и другое.</i>"
)


def pro_upsell(tool_name: str) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🔒 <b>{tool_name}</b> — доступно в PRO\n{LINE}\n\n<b>Что открывает PRO:</b>\n" + PRO_VALUE
    return text, kb(
        [btn("💎 Открыть PRO — 500 ⭐ / мес", "buy_sub_month")],
        [btn("👑 VIP на год — 1000 ⭐", "buy_sub_year")],
        back_main_row(),
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

    if is_new:
        await message.answer(WELCOME_NEW, reply_markup=welcome_new_kb())
        return
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
async def create_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()  # выходим из консультации/анкеты, чтобы текст шёл в инструмент
    text = f"✍️ <b>Что создаём?</b>\n{LINE}\n\n"
    for name, desc in CATEGORIES.values():
        text += f"{name}\n<i>{desc}</i>\n\n"
    text += "🔍 <b>Аудит текста</b>\n<i>Оценка вашего поста и улучшенная версия</i>"
    buttons = [btn(name, f"cat_{code}") for code, (name, _) in CATEGORIES.items()]
    rows = grid(buttons, 2) + [[btn("🔍 Аудит текста / ДЗ", "tool_audit")], back_main_row()]
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
async def select_tool(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
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
        f"🎓 Курс: <b>{len(u['course_done'])}/{COURSE_TOTAL}</b> уроков\n"
        + ("🧭 План роста: <b>готов</b>\n" if u["growth_plan"] else "🧭 План роста: <b>не составлен</b>\n")
        + f"👥 Приглашено друзей: <b>{u['ref_count']}</b>\n"
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
# 🧠 ОБЩИЕ ФУНКЦИИ ДЛЯ ИИ-МОДУЛЕЙ
# ====================================================
def lang_rule(u: dict) -> str:
    return FORMAT_HINT + " " + LANG_INSTRUCTION.get(u["lang"], LANG_INSTRUCTION["uk"])


def biz_context(u: dict) -> str:
    """Всё, что бот знает о бизнесе пользователя, — для персональных ответов ИИ."""
    parts = []
    b = u.get("brand") or {}
    if b:
        parts.append(f"Ниша: {b.get('niche')}. Аудитория: {b.get('audience')}. Тон: {TONES.get(b.get('tone'), '')}.")
    for key, answer in (u.get("biz") or {}).items():
        question = next((q[0] for q in STRAT_QUESTIONS if q[2] == key), key)
        parts.append(f"{question} — {answer}")
    return "\n".join(parts) or "Данных о бизнесе пока нет."


async def check_limits(message: types.Message, user_id: int, u: dict, pro_feature: str | None = None) -> bool:
    """PRO-функции — только для PRO; остальное — по балансу генераций."""
    sub = is_sub_active(user_id, u)
    if pro_feature and not sub:
        text, markup = pro_upsell(pro_feature)
        await message.answer(text, reply_markup=markup)
        return False
    if not sub and u["attempts"] <= 0:
        await message.answer(
            "⚠️ <b>Бесплатные генерации закончились</b>\n\n"
            f"Вы уже создали <b>{u['total_gens']}</b> материалов. Продолжите без ограничений 👇",
            reply_markup=limit_keyboard(),
        )
        return False
    return True


async def run_ai_task(
    message: types.Message, user_id: int, system: str, prompt: str, header: str, track_key: str,
    pro_feature: str | None = None, charge: bool = True, markup=None, wait_text: str = "⏳ Анализирую...",
):
    u = get_user_info(user_id)
    if charge and not await check_limits(message, user_id, u, pro_feature):
        return
    u["last_prompt"] = prompt[:4000]
    status_msg = await message.answer(wait_text)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    await run_generation(user_id, status_msg, messages, u, header=header, track_key=track_key,
                         charge=charge, markup_override=markup)


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(done / total * width) if total else 0
    return "▓" * filled + "░" * (width - filled)


# ====================================================
# ℹ️ О БОТЕ И ПЕРВЫЙ ЗАПУСК
# ====================================================
ABOUT_TEXT = (
    f"ℹ️ <b>SMM Architecture AI — что это?</b>\n{LINE}\n\n"
    "Ваш личный SMM-отдел в Telegram: <b>стратег, копирайтер, аналитик и наставник</b> в одном боте. "
    "Работает 24/7 и знает ваш бизнес.\n\n"
    "<b>Что умеет:</b>\n"
    "🧭 <b>AI-стратег</b> — задаёт вопросы о вашем бизнесе, ставит диагноз и составляет план роста на 30 дней. "
    "Потом с ним можно советоваться, как с живым консультантом.\n"
    "✍️ <b>Контент</b> — 19 инструментов: посты, карусели, Reels, Stories, воронки, контент-план, промпты для визуала.\n"
    "🕵️ <b>Конкуренты</b> — разбор чужих аккаунтов, сравнение с вами, поиск свободной ниши и план обгона.\n"
    "🎓 <b>Академия</b> — курс «SMM: с нуля до PRO»: 6 модулей, 24 урока, задания с проверкой ИИ-ментора и сертификат.\n"
    "🔥 <b>Разборы</b> — каждый день лучшие и провальные посты с объяснением, что сработало и где ошибка.\n"
    "📤 <b>Автопостинг</b> — публикация в ваш Telegram-канал сразу или по расписанию.\n\n"
    "<b>Для кого:</b>\n"
    "• владельцы бизнеса и эксперты, которые ведут соцсети сами;\n"
    "• SMM-специалисты — делать работу в 5 раз быстрее и брать больше клиентов;\n"
    "• новички — освоить профессию SMM с нуля.\n\n"
    "<b>С чего начать — 3 шага:</b>\n"
    "1️⃣ Пройдите диагностику у AI-стратега (2 минуты).\n"
    "2️⃣ Получите план роста и первые задачи на сегодня.\n"
    "3️⃣ Создавайте контент по плану и учитесь в Академии.\n\n"
    "<b>Бесплатно:</b> диагностика, первый модуль курса, разборы постов, 3 генерации.\n"
    "<b>PRO:</b> всё без ограничений."
)


def about_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("🧭 Пройти диагностику — 2 мин", "strat_start")],
        [btn("🎓 Начать обучение", "cr_menu"), btn("✍️ Создать контент", "create")],
        back_main_row(),
    )


@dp.callback_query(F.data == "about")
async def about_cb(callback: types.CallbackQuery):
    await safe_edit(callback.message, ABOUT_TEXT, about_kb())
    await callback.answer()


@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer(ABOUT_TEXT, reply_markup=about_kb())


WELCOME_NEW = (
    "👋 <b>Добро пожаловать в SMM Architecture AI!</b>\n\n"
    "Я — ваш AI-маркетолог: помогу понять, <b>как продвигать именно ваш бизнес</b>, "
    "напишу контент, разберу конкурентов и научу SMM с нуля.\n\n"
    "🎁 Для старта у вас <b>3 бесплатные генерации</b>, бесплатная диагностика и первый модуль курса.\n\n"
    "<b>С чего лучше начать?</b>\n"
    "🧭 Если у вас есть бизнес или блог — пройдите диагностику: я задам 7 вопросов и составлю план роста.\n"
    "🎓 Если хотите освоить SMM — начните с курса."
)


def welcome_new_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("🧭 Пройти диагностику — 2 мин", "strat_start")],
        [btn("🎓 Начать обучение SMM", "cr_menu")],
        [btn("ℹ️ Что умеет бот", "about"), btn("🏠 Главное меню", "back_to_main")],
    )


# ====================================================
# 🧭 AI-СТРАТЕГ: диагностика бизнеса, план роста, консультации
# ====================================================
# (вопрос, пример ответа, ключ)
STRAT_QUESTIONS = [
    ("Чем вы занимаетесь? Что продаёте и где работаете (город или онлайн)?",
     "Например: «кондитерская на заказ в Днепре, торты и десерты»", "business"),
    ("Сколько стоит ваш продукт и как сейчас приходят клиенты?",
     "Например: «средний чек 1500 грн, клиенты в основном по сарафанному радио»", "price"),
    ("Кто ваш идеальный клиент?",
     "Например: «мамы 25–40 лет, заказывают торты на дни рождения детей»", "audience"),
    ("Какие соцсети вы ведёте, сколько подписчиков и как часто публикуете?",
     "Например: «Instagram 800 подписчиков, пост раз в неделю» или «начинаю с нуля»", "socials"),
    ("Какая главная цель на ближайшие 3 месяца?",
     "Например: «20 заказов в месяц из Instagram» или «запустить онлайн-курс»", "goal"),
    ("Что сейчас не получается? Главная проблема в продвижении?",
     "Например: «мало охватов, посты никто не видит» или «не знаю, что публиковать»", "problem"),
    ("Сколько времени и денег вы готовы вкладывать в продвижение?",
     "Например: «1 час в день, рекламный бюджет до 3000 грн в месяц»", "resources"),
]

STRAT_PLAN_PROMPT = (
    "Ты — топовый SMM-стратег и бизнес-консультант с опытом сотен проектов. Перед тобой ответы клиента "
    "на диагностику. Составь персональную стратегию продвижения. Пиши конкретно под этот бизнес, "
    "без общих фраз, с цифрами и примерами. Структура:\n"
    "🩺 **Диагноз** — где бизнес сейчас и что мешает расти (3–5 пунктов).\n"
    "🎯 **Позиционирование** — 3 варианта УТП, отметь лучший.\n"
    "👥 **Портрет клиента** — боли, желания, возражения.\n"
    "📱 **Площадки и форматы** — где продвигаться и почему, частота публикаций.\n"
    "🗺 **План на 30 дней** — по неделям, конкретные задачи.\n"
    "✅ **Сделайте сегодня** — 3 действия на 1–2 часа.\n"
    "📈 **Метрики** — какие цифры отслеживать и целевые значения через месяц.\n"
    "🛠 **Чем поможет бот** — какие инструменты использовать: Контент-план 30 дней, Сценарий Reels, "
    "Воронка продаж, Анализ конкурентов, курс в Академии (укажи конкретные модули)."
)

STRAT_CHAT_PROMPT = (
    "Ты — личный SMM-стратег клиента. Ты уже провёл диагностику его бизнеса и составил план. "
    "Отвечай как опытный консультант: конкретно, по делу, с примерами для его ниши. "
    "Если для точного совета не хватает данных — задай 1–2 уточняющих вопроса. "
    "Предлагай следующий шаг в конце ответа. Если полезен инструмент бота (контент-план, сценарий Reels, "
    "воронка, анализ конкурентов, урок Академии) — порекомендуй его."
)


class StratInterview(StatesGroup):
    answering = State()


class StratChat(StatesGroup):
    active = State()


def strat_menu_view(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    if not u.get("growth_plan"):
        text = (
            f"🧭 <b>AI-стратег</b>\n{LINE}\n\n"
            "Я проведу <b>диагностику вашего бизнеса</b>, как это делает дорогой консультант:\n\n"
            "1️⃣ Задам 7 вопросов — о продукте, клиентах, соцсетях и целях.\n"
            "2️⃣ Найду, что мешает расти.\n"
            "3️⃣ Составлю <b>персональную стратегию</b>: позиционирование, портрет клиента, "
            "план на 30 дней и 3 действия на сегодня.\n"
            "4️⃣ Дальше — советуйтесь со мной в любой момент, я помню ваш бизнес.\n\n"
            "⏱ Займёт 2–3 минуты. Первая диагностика — бесплатно."
        )
        return text, kb([btn("🚀 Начать диагностику", "strat_start")], back_main_row())
    text = (
        f"🧭 <b>AI-стратег</b>\n{LINE}\n\n"
        "✅ Диагностика пройдена, ваш план роста готов.\n\n"
        "💬 <b>Консультация</b> — спросите что угодно о продвижении: как поднять охваты, что публиковать, "
        "сколько вкладывать в рекламу, как поднять чек. Я отвечу с учётом вашего бизнеса.\n\n"
        "🔄 Бизнес изменился? Пройдите диагностику заново."
    )
    return text, kb(
        [btn("📋 Мой план роста", "strat_plan"), btn("💬 Консультация", "strat_chat")],
        [btn("🔄 Пройти заново", "strat_start")],
        back_main_row(),
    )


@dp.callback_query(F.data == "strat")
async def strat_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = strat_menu_view(get_user_info(callback.from_user.id))
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.message(Command("strategy"))
async def strat_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = strat_menu_view(get_user_info(message.from_user.id))
    await message.answer(text, reply_markup=markup)


async def ask_strat_question(message: types.Message, idx: int):
    question, example, _ = STRAT_QUESTIONS[idx]
    await message.answer(
        f"🧭 <b>Вопрос {idx + 1}/{len(STRAT_QUESTIONS)}</b>\n"
        f"<code>{progress_bar(idx, len(STRAT_QUESTIONS), 7)}</code>\n\n"
        f"<b>{question}</b>\n\n<i>{example}</i>\n\n"
        "💬 Можно ответить текстом в свободной форме.",
        reply_markup=kb([btn("✖️ Отмена", "strat")]),
    )


@dp.callback_query(F.data == "strat_start")
async def strat_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(StratInterview.answering)
    await state.update_data(idx=0, answers={})
    await callback.answer()
    await ask_strat_question(callback.message, 0)


@dp.message(StratInterview.answering, F.text)
async def strat_answer(message: types.Message, state: FSMContext):
    if message.text.startswith("/"):
        return
    data = await state.get_data()
    idx = data.get("idx", 0)
    answers = data.get("answers", {})
    answers[STRAT_QUESTIONS[idx][2]] = message.text[:800]
    idx += 1
    if idx < len(STRAT_QUESTIONS):
        await state.update_data(idx=idx, answers=answers)
        await ask_strat_question(message, idx)
        return

    await state.clear()
    user_id = message.from_user.id
    u = get_user_info(user_id)
    u["biz"] = answers
    if not u["brand"]:  # заполняем «Мой бренд» из ответов, чтобы все тексты стали точнее
        u["brand"] = {"niche": answers.get("business", "")[:150], "audience": answers.get("audience", "")[:150],
                      "tone": "expert", "samples": []}
    first_time = not u.get("plan_count")
    u["plan_count"] = u.get("plan_count", 0) + 1
    save_data()

    prompt = "Ответы клиента на диагностику:\n" + biz_context(u)
    if not first_time and not await check_limits(message, user_id, u):
        return
    status_msg = await message.answer("🧠 <b>Анализирую ваш бизнес...</b>\nСоставляю стратегию — это займёт до минуты.")
    u["last_prompt"] = prompt
    u["last_answer"] = None
    await run_generation(
        user_id, status_msg,
        [{"role": "system", "content": STRAT_PLAN_PROMPT + lang_rule(u)}, {"role": "user", "content": prompt}],
        u, header="🧭 Ваша стратегия роста", track_key="strategist_plan", charge=not first_time,
        markup_override=kb(
            [btn("💬 Обсудить со стратегом", "strat_chat")],
            [btn("📅 Сделать контент-план", "tool_content_plan")],
            [btn("⭐ Сохранить", "save"), btn("🏠 Меню", "back_to_main")],
        ),
    )
    if u.get("last_answer"):
        u["growth_plan"] = u["last_answer"]
        save_data()


@dp.callback_query(F.data == "strat_plan")
async def strat_plan(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    await callback.answer()
    if not u.get("growth_plan"):
        await callback.message.answer("Сначала пройдите диагностику 🙂", reply_markup=kb([btn("🚀 Начать диагностику", "strat_start")]))
        return
    await callback.message.answer(f"🧭 <b>Ваш план роста</b>\n{LINE}")
    await send_long(callback.message, u["growth_plan"],
                    kb([btn("💬 Консультация", "strat_chat")], [btn("◀️ AI-стратег", "strat")]))


@dp.callback_query(F.data == "strat_chat")
async def strat_chat_start(callback: types.CallbackQuery, state: FSMContext):
    u = get_user_info(callback.from_user.id)
    await callback.answer()
    if not u.get("growth_plan"):
        await callback.message.answer(
            "💬 Чтобы советы были точными, сначала пройдите диагностику — это 2 минуты.",
            reply_markup=kb([btn("🚀 Начать диагностику", "strat_start")]),
        )
        return
    await state.set_state(StratChat.active)
    await callback.message.answer(
        f"💬 <b>Стратег на связи</b>\n{LINE}\n\n"
        "Задайте любой вопрос о продвижении — я знаю ваш бизнес и план.\n\n"
        "<i>Например: «Почему падают охваты?», «Сколько вкладывать в рекламу?», «Как поднять средний чек?»</i>",
        reply_markup=kb([btn("🏁 Завершить консультацию", "strat_end")]),
    )


@dp.callback_query(F.data == "strat_end")
async def strat_chat_end(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    text, markup = main_menu(callback.from_user)
    await callback.message.answer("✅ Консультация завершена. Возвращайтесь в любой момент!\n\n" + text, reply_markup=markup)


@dp.message(StratChat.active, F.text)
async def strat_chat_message(message: types.Message):
    if message.text.startswith("/"):
        return
    user_id = message.from_user.id
    u = get_user_info(user_id)
    if not await check_limits(message, user_id, u):
        return
    history = u.get("strat_chat", [])[-8:]
    system = (
        STRAT_CHAT_PROMPT
        + "\n\nПрофиль бизнеса:\n" + biz_context(u)
        + "\n\nСтратегия, которую ты составил ранее (кратко):\n" + (u.get("growth_plan") or "")[:2500]
        + lang_rule(u)
    )
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": message.text[:3000]}]
    u["last_prompt"] = message.text[:3000]
    u["last_answer"] = None
    status_msg = await message.answer("🧭 Думаю над ответом...")
    await run_generation(user_id, status_msg, messages, u, header="🧭 AI-стратег", track_key="strategist_chat",
                         markup_override=kb([btn("⭐ Сохранить", "save"), btn("🏁 Завершить консультацию", "strat_end")]))
    if not u.get("last_answer"):
        return
    u["strat_chat"] = (history + [
        {"role": "user", "content": message.text[:1500]},
        {"role": "assistant", "content": (u.get("last_answer") or "")[:2500]},
    ])[-10:]
    save_data()


# ====================================================
# 🕵️ АНАЛИЗ КОНКУРЕНТОВ
# ====================================================
MAX_COMPETITORS_FREE = 1
MAX_COMPETITORS_PRO = 5

COMP_ANALYSIS_PROMPT = (
    "Ты — SMM-аналитик и маркетолог. Сделай глубокий разбор конкурента по данным ниже. Структура:\n"
    "🎯 **Позиционирование и УТП** — как он себя подаёт и на кого рассчитан.\n"
    "💰 **Оффер и цены** — что продаёт, как упакован продукт.\n"
    "📱 **Контент-стратегия** — форматы, рубрики, хуки, тон, частота.\n"
    "🔁 **Воронка** — как превращает подписчиков в клиентов.\n"
    "📊 **Оценка 1–10** — упаковка профиля, контент, вовлечение, оффер, визуал, продажи.\n"
    "💪 **Сильные стороны** и ⚠️ **Слабые места**.\n"
    "🧲 **Что взять себе** — 3 приёма.\n"
    "🚀 **Где его обойти** — 3 конкретных хода.\n"
    "Если данных мало — делай выводы осторожно и скажи, какой информации не хватает."
)
COMP_COMPARE_PROMPT = (
    "Ты — SMM-стратег. Сравни бизнес клиента с конкурентом. Пройди по параметрам: позиционирование, "
    "оффер и цены, упаковка профиля, контент, вовлечение, продажи. По каждому: кто сильнее и почему. "
    "В конце — 5 конкретных действий, чтобы обогнать конкурента за 30 дней."
)
COMP_GAP_PROMPT = (
    "Ты — маркетолог-стратег. По данным о конкурентах найди свободное место на рынке:\n"
    "🕳 **Незанятые позиции** — что никто не делает.\n"
    "😣 **Боли клиентов, которые игнорируют** конкуренты.\n"
    "💡 **3 варианта уникального УТП** для клиента.\n"
    "📝 **10 тем контента**, которых нет у конкурентов.\n"
    "🎯 **Рекомендация** — какую позицию занять и почему."
)
COMP_ATTACK_PROMPT = (
    "Ты — SMM-стратег. Составь план обгона конкурентов на 30 дней для клиента: позиционирование, "
    "контент по неделям, механики роста, офферы, реклама, метрики. Только конкретика под эту нишу."
)


class CompAdd(StatesGroup):
    name = State()
    data = State()


def comp_menu_view(user_id: int, u: dict) -> tuple[str, InlineKeyboardMarkup]:
    comps = u.get("competitors", [])
    limit = MAX_COMPETITORS_PRO if is_sub_active(user_id, u) else MAX_COMPETITORS_FREE
    text = (
        f"🕵️ <b>Анализ конкурентов</b>\n{LINE}\n\n"
        "Добавьте конкурентов — и я разберу их стратегию: позиционирование, оффер, контент, воронку, "
        "сильные и слабые стороны.\n\n"
        "🔍 <b>Разбор</b> — что работает у конкурента и что взять себе\n"
        "⚔️ <b>Сравнение со мной</b> — где вы сильнее, где отстаёте\n"
        "🕳 <b>Свободная ниша</b> — что упускают все конкуренты\n"
        "🚀 <b>План обгона</b> — стратегия на 30 дней\n\n"
        f"📋 Конкурентов: <b>{len(comps)}/{limit}</b>\n"
        "<i>Бот не заходит в чужие аккаунты: анализ строится на данных, которые вы пришлёте.</i>"
    )
    rows = [[btn(f"🔍 {c['name'][:30]}", f"comp_v_{i}")] for i, c in enumerate(comps)]
    if len(comps) < limit:
        rows.append([btn("➕ Добавить конкурента", "comp_add")])
    elif not is_sub_active(user_id, u):
        rows.append([btn("🔒 До 5 конкурентов — в PRO", "buy_menu")])
    if comps:
        rows.append([btn("🕳 Свободная ниша", "comp_gap"), btn("🚀 План обгона", "comp_attack")])
    rows.append(back_main_row())
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "comp")
async def comp_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    u = get_user_info(callback.from_user.id)
    text, markup = comp_menu_view(callback.from_user.id, u)
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.callback_query(F.data == "comp_add")
async def comp_add_start(callback: types.CallbackQuery, state: FSMContext):
    u = get_user_info(callback.from_user.id)
    limit = MAX_COMPETITORS_PRO if is_sub_active(callback.from_user.id, u) else MAX_COMPETITORS_FREE
    if len(u.get("competitors", [])) >= limit:
        await callback.answer("Достигнут лимит конкурентов", show_alert=True)
        return
    await state.set_state(CompAdd.name)
    await callback.answer()
    await callback.message.answer(
        "🕵️ <b>Шаг 1 из 2</b>\n\nКак называется конкурент? Можно @никнейм или ссылку на профиль.",
        reply_markup=kb([btn("✖️ Отмена", "comp")]),
    )


@dp.message(CompAdd.name, F.text)
async def comp_add_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:60])
    await state.set_state(CompAdd.data)
    await message.answer(
        "🕵️ <b>Шаг 2 из 2</b>\n\nПришлите всё, что знаете о конкуренте, <b>одним сообщением</b>:\n"
        "• шапку профиля (bio)\n• что продаёт и по каким ценам\n• 2–3 текста его постов\n"
        "• сколько подписчиков, что вам у него нравится\n\n"
        "<i>Чем больше данных — тем точнее разбор. Можно просто переслать его пост.</i>",
        reply_markup=kb([btn("✖️ Отмена", "comp")]),
    )


@dp.message(CompAdd.data, F.text | F.caption)
async def comp_add_data(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    u = get_user_info(message.from_user.id)
    u.setdefault("competitors", []).append({
        "name": data.get("name", "Конкурент"),
        "data": (message.text or message.caption)[:3500],
    })
    save_data()
    idx = len(u["competitors"]) - 1
    await message.answer(
        f"✅ Конкурент <b>{esc(u['competitors'][idx]['name'])}</b> добавлен.",
        reply_markup=kb([btn("🔍 Сделать разбор", f"comp_a_{idx}")], [btn("◀️ Конкуренты", "comp")]),
    )


def get_comp(u: dict, data: str, prefix: str) -> tuple[int, dict | None]:
    try:
        idx = int(data.removeprefix(prefix))
        return idx, u.get("competitors", [])[idx]
    except (ValueError, IndexError):
        return -1, None


@dp.callback_query(F.data.startswith("comp_v_"))
async def comp_view(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    idx, c = get_comp(u, callback.data, "comp_v_")
    if not c:
        await callback.answer("Конкурент не найден", show_alert=True)
        return
    await safe_edit(
        callback.message,
        f"🕵️ <b>{esc(c['name'])}</b>\n{LINE}\n\n<i>{esc(c['data'][:700])}</i>",
        kb(
            [btn("🔍 Полный разбор", f"comp_a_{idx}"), btn("⚔️ Сравнить со мной", f"comp_c_{idx}")],
            [btn("🗑 Удалить", f"comp_d_{idx}"), btn("◀️ Конкуренты", "comp")],
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("comp_d_"))
async def comp_delete(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    idx, c = get_comp(u, callback.data, "comp_d_")
    if c:
        u["competitors"].pop(idx)
        save_data()
    text, markup = comp_menu_view(callback.from_user.id, u)
    await safe_edit(callback.message, text, markup)
    await callback.answer("Удалено")


@dp.callback_query(F.data.startswith("comp_a_"))
async def comp_analyze(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    idx, c = get_comp(u, callback.data, "comp_a_")
    await callback.answer()
    if not c:
        return
    await run_ai_task(
        callback.message, callback.from_user.id, COMP_ANALYSIS_PROMPT + lang_rule(u),
        f"Конкурент: {c['name']}\nДанные:\n{c['data']}\n\nКонтекст моего бизнеса:\n{biz_context(u)}",
        f"🔍 Разбор конкурента: {c['name']}", "comp_analysis",
    )


@dp.callback_query(F.data.startswith("comp_c_"))
async def comp_compare(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    idx, c = get_comp(u, callback.data, "comp_c_")
    await callback.answer()
    if not c:
        return
    if not u.get("biz") and not u.get("brand"):
        await callback.message.answer(
            "⚔️ Для сравнения мне нужно знать ваш бизнес. Пройдите диагностику у AI-стратега — 2 минуты.",
            reply_markup=kb([btn("🚀 Начать диагностику", "strat_start")]),
        )
        return
    await run_ai_task(
        callback.message, callback.from_user.id, COMP_COMPARE_PROMPT + lang_rule(u),
        f"Мой бизнес:\n{biz_context(u)}\n\nКонкурент: {c['name']}\n{c['data']}",
        f"⚔️ Вы vs {c['name']}", "comp_compare", pro_feature="⚔️ Сравнение с конкурентом",
    )


def all_comps_text(u: dict) -> str:
    return "\n\n".join(f"Конкурент {i + 1}: {c['name']}\n{c['data'][:1500]}" for i, c in enumerate(u.get("competitors", [])))


@dp.callback_query(F.data == "comp_gap")
async def comp_gap(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    await callback.answer()
    await run_ai_task(
        callback.message, callback.from_user.id, COMP_GAP_PROMPT + lang_rule(u),
        f"Мой бизнес:\n{biz_context(u)}\n\n{all_comps_text(u)}",
        "🕳 Свободная ниша и УТП", "comp_gap", pro_feature="🕳 Поиск свободной ниши",
    )


@dp.callback_query(F.data == "comp_attack")
async def comp_attack(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    await callback.answer()
    await run_ai_task(
        callback.message, callback.from_user.id, COMP_ATTACK_PROMPT + lang_rule(u),
        f"Мой бизнес:\n{biz_context(u)}\n\n{all_comps_text(u)}",
        "🚀 План обгона конкурентов", "comp_attack", pro_feature="🚀 План обгона конкурентов",
    )


# ====================================================
# 🎓 АКАДЕМИЯ: курс «SMM: с нуля до PRO»
# ====================================================
# Тексты уроков пишет ИИ по программе ниже — один раз на каждый язык, дальше берутся из базы.
COURSE = [
    {"id": "m1", "title": "🧱 Фундамент SMM", "free": True,
     "desc": "Как работает SMM, целевая аудитория, позиционирование и упаковка профиля.",
     "lessons": [
         ("Как работает SMM и зачем он бизнесу", "роль SMM в воронке продаж; органика и реклама; задачи: узнаваемость, доверие, продажи; частые ошибки новичков; какие метрики считать успехом"),
         ("Целевая аудитория: портрет, боли и желания", "сегментация; метод Jobs To Be Done; 5 болей, 5 желаний, 5 возражений; где искать инсайты: комментарии, отзывы конкурентов, опросы; шаблон портрета клиента"),
         ("Позиционирование и УТП", "формула УТП; отстройка от конкурентов; типы позиционирования; сильные и слабые примеры; упражнение на своё УТП"),
         ("Упаковка профиля, который продаёт", "имя и никнейм с ключевым словом; шапка по формуле; аватар; закреплённые посты и Highlights; первые 9 постов; чек-лист упаковки"),
     ]},
    {"id": "m2", "title": "✍️ Контент, который читают", "free": False,
     "desc": "Контент-стратегия, хуки и копирайтинг, Reels, Stories и прогревы.",
     "lessons": [
         ("Контент-стратегия и рубрикатор", "цели контента; баланс охват / доверие / продажи 40/40/20; рубрики; частота публикаций; как собрать контент-план на месяц"),
         ("Хуки и копирайтинг", "10 формул хуков с примерами; модели AIDA и PAS; структура поста; сильный призыв к действию; редактура текста"),
         ("Reels и короткие видео", "первые 3 секунды; структура сценария; тренды и вечнозелёный контент; монтаж и субтитры; как удерживать досмотры"),
         ("Stories и прогревы", "роль Stories в продажах; интерактивы; сторителлинг; прогрев на 5 дней; дожим"),
     ]},
    {"id": "m3", "title": "📈 Рост и алгоритмы", "free": False,
     "desc": "Как работают алгоритмы, органический рост, комьюнити и аналитика.",
     "lessons": [
         ("Как работают алгоритмы Instagram, TikTok и Telegram", "сигналы ранжирования; досмотры, сохранения, репосты; время публикации; популярные мифы"),
         ("Органический рост без бюджета", "коллаборации и взаимопиар; комментинг; UGC; кросс-постинг; поиск и ключевые слова в соцсетях"),
         ("Вовлечение и комьюнити", "диалог с аудиторией; ответы на комментарии; работа с негативом; амбассадоры бренда; механики вовлечения"),
         ("Аналитика и метрики", "охват, ER, ERR, CTR, конверсия; как читать статистику; еженедельный отчёт; решения на основе данных"),
     ]},
    {"id": "m4", "title": "💰 Продажи через соцсети", "free": False,
     "desc": "Воронки, офферы, продажи в директе и запуски.",
     "lessons": [
         ("Воронка продаж в соцсетях", "этапы воронки; лид-магнит; автоворонки в директе; точки касания с клиентом"),
         ("Офферы, от которых сложно отказаться", "формула оффера; ценность и цена; бонусы; дедлайны; гарантии"),
         ("Продажи в директе и возражения", "скрипт переписки; квалификация клиента; 7 главных возражений и ответы; дожим"),
         ("Запуски и акции", "план запуска; прогрев; открытие продаж; дожим; разбор результатов"),
     ]},
    {"id": "m5", "title": "🎯 Реклама и масштабирование", "free": False,
     "desc": "Таргет, креативы, реклама у блогеров и юнит-экономика.",
     "lessons": [
         ("Таргетированная реклама: основы", "структура кабинета Meta Ads; цели кампаний; аудитории; бюджеты; тестирование"),
         ("Креативы, которые конвертируют", "форматы; хуки в рекламе; UGC-креативы; A/B-тесты; выгорание креативов"),
         ("Реклама у блогеров", "поиск блогеров; проверка на накрутку; техническое задание; расчёт CPM и CPA; договорённости"),
         ("Юнит-экономика рекламы", "CPC, CPL, CPA, ROMI, LTV; когда масштабировать; когда отключать рекламу"),
     ]},
    {"id": "m6", "title": "🚀 SMM как профессия", "free": False,
     "desc": "Первые клиенты, упаковка услуги, работа с клиентом и своё агентство.",
     "lessons": [
         ("Как найти первых клиентов", "портфолио с нуля; бесплатный аудит как вход; холодные сообщения; биржи; нетворкинг"),
         ("Упаковка услуги и цены", "пакеты услуг; ценообразование; коммерческое предложение; бриф"),
         ("Работа с клиентом: бриф, отчёты, удержание", "онбординг клиента; регулярные отчёты; коммуникация; допродажи"),
         ("От фрилансера к агентству", "делегирование; найм; процессы и шаблоны; как масштабировать доход"),
     ]},
]
COURSE_TOTAL = sum(len(m["lessons"]) for m in COURSE)
MODULES = {m["id"]: m for m in COURSE}

LESSON_PROMPT = (
    "Ты — топовый SMM-эксперт и преподаватель, автор платного курса «SMM: с нуля до PRO». "
    "Напиши урок. Пиши живо, конкретно, с примерами из разных ниш и цифрами, без воды. Структура:\n"
    "🎯 **Что вы узнаете** — 2–3 строки.\n"
    "Основная часть — раскрой каждый пункт программы с подзаголовками и примерами.\n"
    "⚠️ **Частые ошибки** — 3–4 пункта.\n"
    "✅ **Чек-лист** — 4–6 пунктов.\n"
    "📝 **Практическое задание** — одно конкретное задание на 15–30 минут; результат — текст, "
    "который студент пришлёт на проверку.\n"
    "Объём — 2500–3500 символов."
)
HW_PROMPT = (
    "Ты — строгий, но доброжелательный ментор SMM-курса. Проверь выполненное задание студента по уроку. "
    "Структура ответа:\n"
    "🎯 **Оценка: X/10**\n✅ **Что получилось**\n❌ **Что улучшить** — конкретно и почему\n"
    "✍️ **Как сделать на 10/10** — пример улучшенного варианта\n"
    "Если ответ не по теме задания — поставь низкую оценку и объясни, что нужно сделать."
)
APPLY_PROMPT = (
    "Ты — SMM-наставник. Студент прошёл урок. Покажи, как применить его именно к бизнесу студента: "
    "5–7 конкретных шагов с готовыми примерами (тексты, идеи, цифры) для его ниши. В конце — что сделать сегодня."
)

lessons_cache: dict = {}            # {"uk:m1l1": "текст урока"}
_lesson_locks: dict[str, asyncio.Lock] = {}


def lesson_id(mid: str, n: int) -> str:
    return f"{mid}l{n + 1}"


def find_lesson(lid: str):
    try:
        mid, num = lid.split("l", 1)
        n = int(num) - 1
    except ValueError:
        return None, None, None
    module = MODULES.get(mid)
    if not module or not 0 <= n < len(module["lessons"]):
        return None, None, None
    return module, n, module["lessons"][n]


def module_unlocked(user_id: int, u: dict, module: dict) -> bool:
    return module["free"] or is_sub_active(user_id, u)


async def get_lesson_text(lang: str, lid: str) -> str:
    key = f"{lang}:{lid}"
    if key in lessons_cache:
        return lessons_cache[key]
    lock = _lesson_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if key in lessons_cache:
            return lessons_cache[key]
        module, n, (title, points) = find_lesson(lid)
        text = await ai_complete(
            LESSON_PROMPT + FORMAT_HINT + " " + LANG_INSTRUCTION.get(lang, LANG_INSTRUCTION["uk"]),
            f"Модуль: {module['title']}\nУрок {n + 1}: {title}\nПрограмма урока: {points}",
        )
        lessons_cache[key] = text
        _pending.add("lessons")
        return text


ACADEMY_TEXT = (
    f"🎓 <b>SMM Академия</b>\n{LINE}\n\n"
    "📚 <b>Курс «SMM: с нуля до PRO»</b> — 6 модулей и 24 урока: от целевой аудитории до рекламы "
    "и своего агентства. После каждого урока — задание, которое проверяет ИИ-ментор, а в конце — сертификат.\n\n"
    "🧪 <b>Тесты</b> — проверьте знания за минуту.\n"
    "🔥 <b>Разборы постов</b> — учитесь на чужих успехах и ошибках.\n"
    "🔍 <b>Проверка вашего текста</b> — оценка и улучшенная версия.\n\n"
    "🎁 Первый модуль — бесплатно."
)


@dp.callback_query(F.data == "academy_menu")
async def academy_menu(callback: types.CallbackQuery):
    await safe_edit(
        callback.message, ACADEMY_TEXT,
        kb(
            [btn("📚 Курс «SMM: с нуля до PRO»", "cr_menu")],
            [btn("🧪 Тесты", "test_menu"), btn("🔥 Разборы постов", "cases")],
            [btn("🔍 Проверить мой текст", "tool_audit")],
            back_main_row(),
        ),
    )
    await callback.answer()


def course_menu_view(user_id: int, u: dict) -> tuple[str, InlineKeyboardMarkup]:
    done = set(u.get("course_done", []))
    text = (
        f"📚 <b>Курс «SMM: с нуля до PRO»</b>\n{LINE}\n\n"
        f"6 модулей · {COURSE_TOTAL} урока · задания с проверкой ИИ-ментора · сертификат\n\n"
        f"📊 Прогресс: <code>{progress_bar(len(done), COURSE_TOTAL)}</code> {len(done)}/{COURSE_TOTAL}\n\n"
    )
    rows = []
    for i, m in enumerate(COURSE, 1):
        m_done = sum(1 for n in range(len(m["lessons"])) if lesson_id(m["id"], n) in done)
        if module_unlocked(user_id, u, m):
            mark = "✅" if m_done == len(m["lessons"]) else f"{m_done}/{len(m['lessons'])}"
        else:
            mark = "🔒"
        free = " · бесплатно" if m["free"] else ""
        text += f"{m['title']} — <i>{m['desc']}</i>{free}\n\n"
        rows.append([btn(f"{i}. {m['title']} · {mark}", f"cr_mod_{m['id']}")])
    if len(done) >= COURSE_TOTAL:
        rows.append([btn("🎓 Получить сертификат", "cr_cert")])
    rows.append([btn("◀️ Академия", "academy_menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "cr_menu")
async def course_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = course_menu_view(callback.from_user.id, get_user_info(callback.from_user.id))
    await safe_edit(callback.message, text, markup)
    await callback.answer()


@dp.message(Command("course"))
async def course_cmd(message: types.Message):
    text, markup = course_menu_view(message.from_user.id, get_user_info(message.from_user.id))
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("cr_mod_"))
async def course_module(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    u = get_user_info(user_id)
    module = MODULES.get(callback.data.removeprefix("cr_mod_"))
    if not module:
        await callback.answer()
        return
    if not module_unlocked(user_id, u, module):
        text, markup = pro_upsell(module["title"])
        await safe_edit(callback.message, text, markup)
        await callback.answer()
        return
    done = set(u.get("course_done", []))
    text = f"{module['title']}\n{LINE}\n\n<i>{module['desc']}</i>\n\n"
    rows = []
    for n, (title, _) in enumerate(module["lessons"]):
        lid = lesson_id(module["id"], n)
        mark = "✅" if lid in done else "▫️"
        rows.append([btn(f"{mark} {n + 1}. {title}", f"cr_l_{lid}")])
    rows.append([btn("◀️ Курс", "cr_menu")])
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


def lesson_kb(lid: str) -> InlineKeyboardMarkup:
    module, n, _ = find_lesson(lid)
    return kb(
        [btn("📝 Сдать задание на проверку", f"cr_hw_{lid}")],
        [btn("🎯 Применить к моему бизнесу", f"cr_app_{lid}")],
        [btn("✅ Урок пройден → дальше", f"cr_done_{lid}")],
        [btn("◀️ К модулю", f"cr_mod_{module['id']}")],
    )


async def open_lesson(message: types.Message, user_id: int, lid: str):
    u = get_user_info(user_id)
    module, n, lesson = find_lesson(lid)
    if not module:
        return
    if not module_unlocked(user_id, u, module):
        text, markup = pro_upsell(module["title"])
        await message.answer(text, reply_markup=markup)
        return
    status = await message.answer("📖 Открываю урок...")
    try:
        body = await get_lesson_text(u["lang"], lid)
    except Exception as e:
        logging.error(f"Lesson error: {e}")
        await safe_edit(status, "⚠️ Не удалось загрузить урок. Попробуйте ещё раз через минуту.")
        return
    u["last_lesson"] = lid
    save_data()
    num = COURSE.index(module) + 1
    await safe_edit(status, f"🎓 <b>Модуль {num} · Урок {n + 1}/{len(module['lessons'])}</b>\n<b>{lesson[0]}</b>\n{LINE}")
    await send_long(message, body, lesson_kb(lid))


@dp.callback_query(F.data.startswith("cr_l_"))
async def course_lesson(callback: types.CallbackQuery):
    await callback.answer()
    await open_lesson(callback.message, callback.from_user.id, callback.data.removeprefix("cr_l_"))


def next_lesson_id(lid: str) -> str | None:
    order = [lesson_id(m["id"], n) for m in COURSE for n in range(len(m["lessons"]))]
    i = order.index(lid)
    return order[i + 1] if i + 1 < len(order) else None


def mark_done(u: dict, lid: str) -> bool:
    done = u.setdefault("course_done", [])
    if lid not in done:
        done.append(lid)
        save_data()
        return True
    return False


@dp.callback_query(F.data.startswith("cr_done_"))
async def course_done(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    u = get_user_info(user_id)
    lid = callback.data.removeprefix("cr_done_")
    if not find_lesson(lid)[0]:
        await callback.answer()
        return
    mark_done(u, lid)
    await callback.answer("Урок засчитан ✅")
    if len(u["course_done"]) >= COURSE_TOTAL:
        await callback.message.answer("🎉 <b>Вы прошли весь курс!</b>", reply_markup=kb([btn("🎓 Получить сертификат", "cr_cert")]))
        return
    nxt = next_lesson_id(lid)
    if nxt:
        await open_lesson(callback.message, user_id, nxt)


class CourseHW(StatesGroup):
    answer = State()


@dp.callback_query(F.data.startswith("cr_hw_"))
async def course_hw_start(callback: types.CallbackQuery, state: FSMContext):
    lid = callback.data.removeprefix("cr_hw_")
    if not find_lesson(lid)[0]:
        await callback.answer()
        return
    await state.set_state(CourseHW.answer)
    await state.update_data(lid=lid)
    await callback.answer()
    await callback.message.answer(
        "📝 <b>Отправьте выполненное задание</b> одним сообщением.\n\n"
        "ИИ-ментор проверит его, поставит оценку и покажет, как сделать на 10/10. "
        "При оценке от 7 урок засчитается автоматически.",
        reply_markup=kb([btn("✖️ Отмена", f"cr_l_{lid}")]),
    )


@dp.message(CourseHW.answer, F.text)
async def course_hw_check(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    lid = data.get("lid", "")
    module, n, lesson = find_lesson(lid)
    if not module:
        return
    user_id = message.from_user.id
    u = get_user_info(user_id)
    lesson_text = lessons_cache.get(f"{u['lang']}:{lid}", "")
    status = await message.answer("🧑‍🏫 Ментор проверяет задание...")
    try:
        feedback = await ai_complete(
            HW_PROMPT + lang_rule(u),
            f"Урок: {lesson[0]}\nТекст урока:\n{lesson_text[:3500]}\n\nОтвет студента:\n{message.text[:3000]}",
        )
    except Exception as e:
        logging.error(f"HW error: {e}")
        await safe_edit(status, "⚠️ Ментор временно недоступен. Попробуйте позже — попытка не списана.")
        return
    track_generation("course_homework")
    score = re.search(r"(\d{1,2})\s*/\s*10", feedback)
    passed = bool(score and int(score.group(1)) >= 7)
    if passed:
        mark_done(u, lid)
    await safe_edit(status, f"🧑‍🏫 <b>Проверка задания</b> · {lesson[0]}\n{LINE}")
    nxt = next_lesson_id(lid)
    rows = [[btn("🔄 Сдать ещё раз", f"cr_hw_{lid}")]]
    if passed:
        rows.insert(0, [btn("✅ Урок засчитан → следующий" if nxt else "🎓 Получить сертификат",
                            f"cr_l_{nxt}" if nxt else "cr_cert")])
    rows.append([btn("◀️ К модулю", f"cr_mod_{module['id']}")])
    await send_long(message, feedback, InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("cr_app_"))
async def course_apply(callback: types.CallbackQuery):
    lid = callback.data.removeprefix("cr_app_")
    module, n, lesson = find_lesson(lid)
    await callback.answer()
    if not module:
        return
    u = get_user_info(callback.from_user.id)
    if not u.get("biz") and not u.get("brand"):
        await callback.message.answer(
            "🎯 Чтобы применить урок к вашему бизнесу, мне нужно его знать. Пройдите диагностику — 2 минуты.",
            reply_markup=kb([btn("🚀 Начать диагностику", "strat_start")]),
        )
        return
    await run_ai_task(
        callback.message, callback.from_user.id, APPLY_PROMPT + lang_rule(u),
        f"Урок: {lesson[0]}\nПрограмма: {lesson[1]}\n\nБизнес студента:\n{biz_context(u)}",
        f"🎯 {lesson[0]} — для вашего бизнеса", "course_apply", pro_feature="🎯 Персональное применение уроков",
    )


@dp.callback_query(F.data == "cr_cert")
async def course_certificate(callback: types.CallbackQuery):
    u = get_user_info(callback.from_user.id)
    await callback.answer()
    if len(u.get("course_done", [])) < COURSE_TOTAL:
        await callback.message.answer("Сертификат откроется после прохождения всех уроков 🎓")
        return
    if not u.get("cert_no"):
        u["cert_no"] = f"SMM-{datetime.now(TZ):%Y}-{callback.from_user.id % 100000:05d}"
        u["cert_date"] = datetime.now(TZ).strftime("%d.%m.%Y")
        save_data()
    await callback.message.answer(
        "🎓 <b>СЕРТИФИКАТ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Настоящим подтверждается, что\n<b>{esc(callback.from_user.full_name)}</b>\n\n"
        "успешно прошёл(-ла) курс\n<b>«SMM: с нуля до PRO»</b>\n"
        f"6 модулей · {COURSE_TOTAL} урока · практические задания\n\n"
        f"📅 {u['cert_date']}\n🔢 № {u['cert_no']}\n\n"
        "<i>SMM Architecture AI Academy</i>\n"
        "━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb(back_main_row()),
    )


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
    f"💎 <b>PRO — ваш SMM-отдел в кармане</b>\n{LINE}\n\n"
    + PRO_VALUE
    + "\n\n<b>Тарифы:</b>\n"
    "💎 PRO месяц — <s>700</s> <b>500 ⭐</b>\n"
    "👑 VIP год — <s>3500</s> <b>1000 ⭐</b> · выгода 70%\n"
    "📦 10 генераций — <b>50 ⭐</b>\n\n"
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
    names = {
        **{k: t["name"] for k, t in TOOLS.items()},
        "case_practice": "🧩 Практика кейсов",
        "strategist_plan": "🧭 Диагностика стратега",
        "strategist_chat": "💬 Консультации стратега",
        "comp_analysis": "🔍 Разбор конкурента",
        "comp_compare": "⚔️ Сравнение с конкурентом",
        "comp_gap": "🕳 Свободная ниша",
        "comp_attack": "🚀 План обгона",
        "course_homework": "📝 ДЗ курса",
        "course_apply": "🎯 Урок → мой бизнес",
    }
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
        "19": ("🌆 Сегодня в 19:00" if today_19.date() == now.date() else "🌆 Завтра в 19:00", today_19),
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
        f"⏰ Запланировано на <b>{when:%d.%m %H:%M}</b> (Киев) в <b>{esc(u['channel']['title'])}</b>.",
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


CASE_TITLES = {
    ("best", "day"): "🏆 Лучший пост дня",
    ("best", "week"): "🏆 Лучший пост недели",
    ("best", "month"): "🏆 Лучший пост месяца",
    ("worst", "day"): "❌ Провальный пост дня",
    ("worst", "week"): "❌ Провальный пост недели",
    ("worst", "month"): "❌ Провальный пост месяца",
}


def case_title(c: dict) -> str:
    if c["kind"] == "practice":
        return "🧩 Кейс для практики"
    return CASE_TITLES.get((c["kind"], c.get("period") or "day"), CASE_TITLES[(c["kind"], "day")])


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
            GRADE_PROMPT + FORMAT_HINT + " " + LANG_INSTRUCTION.get(u["lang"], ""),
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
    # языковую инструкцию ставим последней — так модель надёжнее её соблюдает
    prompt += FORMAT_HINT + " " + LANG_INSTRUCTION.get(u["lang"], LANG_INSTRUCTION["uk"])
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


async def run_generation(
    user_id: int, status_msg: types.Message, messages: list, u: dict,
    header: str | None = None, track_key: str | None = None, charge: bool = True, markup_override=None,
):
    sub = is_sub_active(user_id, u)
    tool = TOOLS[u["tool"]]
    await bot.send_chat_action(status_msg.chat.id, ChatAction.TYPING)
    # запоминаем системную роль — кнопки «Короче», «Ещё вариант» дорабатывают в том же режиме
    if messages and messages[0]["role"] == "system":
        u["last_system"] = messages[0]["content"]

    try:
        response = await ai_client.chat.completions.create(model="deepseek-chat", messages=messages)
        ai_text = response.choices[0].message.content or ""
    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        await notify_admin(f"DeepSeek: {e}", user_id)
        await safe_edit(status_msg, "⚠️ Временный сбой ИИ. Генерация не списана — попробуйте ещё раз.")
        return

    if not sub and charge:
        u["attempts"] -= 1
    u["total_gens"] += 1
    track_generation(track_key or u["tool"])
    u["last_answer"] = ai_text[:6000]
    save_data()

    chunks = split_text(ai_text)
    header = f"{header or tool['name']}\n{LINE}\n\n"
    footer = f"\n\n{LINE}\n{status_line(user_id, u)}"
    for i, chunk in enumerate(chunks):
        is_first, is_last = i == 0, i == len(chunks) - 1
        body = (header if is_first else "") + md_to_html(chunk) + (footer if is_last else "")
        markup = (markup_override or result_keyboard()) if is_last else None
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
        {"role": "system", "content": u.get("last_system") or build_system_prompt(u)},
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
    bot.session.middleware(TranslateMiddleware())
    if DATABASE_URL:
        await db_init()  # без базы бот не стартует — чтобы не потерять данные
        asyncio.create_task(db_loop())
    else:
        logging.warning("DATABASE_URL не задан — данные хранятся в файлах и пропадут при деплое!")
    await bot.delete_webhook(drop_pending_updates=True)
    public_commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="strategy", description="🧭 AI-стратег"),
        BotCommand(command="course", description="🎓 Курс SMM"),
        BotCommand(command="help", description="ℹ️ О боте"),
        BotCommand(command="profile", description="👤 Профиль и баланс"),
        BotCommand(command="brand", description="🎯 Настроить мой бренд"),
        BotCommand(command="buy", description="💎 PRO-доступ"),
        BotCommand(command="id", description="🆔 Мой ID"),
        BotCommand(command="partner", description="🤝 Сотрудничество"),
    ]
    await bot.set_my_commands(public_commands)
    for lang_code in ("uk", "en"):  # меню команд на языке приложения Telegram
        try:
            await bot.set_my_commands(
                [BotCommand(command=c.command, description=tr(lang_code, c.description)) for c in public_commands],
                language_code=lang_code,
            )
        except Exception as e:
            logging.warning(f"Commands for {lang_code} not set: {e}")
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
