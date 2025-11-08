#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import re
import time
from datetime import timedelta
from typing import List, Tuple, Set
from urllib.parse import urlparse

import yaml
from telegram import Update, MessageEntity
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- маркер, что файл стартовал ---
print(">>> TOP of bot.py reached")

# === токен из переменной окружения (Railway Variables) ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Please set BOT_TOKEN environment variable")

# === логирование ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("antispam-bot")

# === аптайм/счётчики ===
START_TS = time.time()
DELETE_COUNTER = 0
ALBUM_PHOTO_LIMIT = 3

def _fmt_uptime(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))

# === конфиг по умолчанию (если нет config.yml) ===
DEFAULT_CONFIG = {
    "admin_ids": [],
    "allowed_tme": ["t.me/your_channel", "t.me/your_chat"],
    "allowed_domains": ["your-site.com"],
    "keywords_block": [
        # RU
        "заработок онлайн", "лёгкий заработок", "лёгкие деньги", "легкий заработок",
        "пассивный доход", "инвестиции от 0", "доход без вложений", "быстрые деньги", "ставки",
        "бинарные опционы", "сигналы", "приватный канал", "вип-канал",
        "обучение с нуля", "100% гарантия", "проверенная схема", "доверительное управление",
        "приглашаю в чат", "приглашаю в канал", "откаты", "арбитраж трафика",
        "переходи по ссылке", "жми сюда", "заработаешь", "посмотри книжку", "бесплатная книга",
        "pdf книга", "раздам курс", "слив курса", "вакансия", "удалённая работа", "удаленная работа",
        "млм", "сетевой маркетинг", "пирамида", "кэшбек 50%", "airdrop за реф", "реферальная ссылка", "join my channel", "work from home", "easy money", "работа из дома",
        "онлайн-заработка", "удалёнка с доходом", "без ставок, без закладок", "быстрым стартом", "легальный способ заработка", "могу отправить тем",
        "заинтерисовало могу скинуть", "проходит обучение для новичков", "удaленно из дома", "белое направление", "СРОЧНО нужны 2 удаленщика",
        "легально и безопасно", "Детали в ЛС", "пишите «+»", "Работа на дому с компьютера", "Дистанционная деятельность", "долларов в неделю",
        "Ищем сотрудников для аналитической работы", "Исключительно 18+", "Стабильно и легально", "СРОЧНО В ПУБЛИЧНОМ ДОСТУПЕ", "Рассылка вашего объявления",
        "Стабильная оплата", "Заработок от", "Работа для всех", "Ребят я тут прочитал одну книгу",
        # EN
        "easy money", "passive income", "work from home", "dm me", "click here", "join my channel", "private", "vip",
    ],
    "keywords_allow": ["биткоин", "bitcoin", "эфир", "ethereum", "usdt", "binance", "okx", "bybit", "airdrops"],
}

URL_RE = re.compile(
    r"""(?ix)
    \b(
        (?:https?://|www\.)[^\s<>]+
        |t\.me/[^\s<>]+
        |\@[\w\d_]{4,}
    )\b
    """
)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s()]{9,})(?!\d)")
INVITE_PATTERNS: Tuple[str, ...] = (
    "t.me/joinchat/", "t.me/+",
    "chat.whatsapp.com/", "join.skype.com/",
    "discord.gg/", "discord.com/invite/",
)

def load_config() -> dict:
    path = os.getenv("CONFIG_PATH", "config.yml")
    if not os.path.exists(path):
        logger.warning("Config file %s not found, using defaults.", path)
        return DEFAULT_CONFIG.copy()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cfg = DEFAULT_CONFIG.copy()
    for k, v in data.items():
        cfg[k] = v
    return cfg

CONFIG = load_config()

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------

def text_of_message(update: Update) -> str:
    msg = update.effective_message
    parts = []
    if msg.text:
        parts.append(msg.text)
    if msg.caption:
        parts.append(msg.caption)
    return " ".join(parts).strip()

def extract_urls_from_entities(entities: List[MessageEntity], text: str) -> List[str]:
    urls = []
    if not entities:
        return urls
    for ent in entities:
        try:
            if ent.type in ("url", "text_link"):
                if ent.type == "text_link" and ent.url:
                    urls.append(ent.url)
                else:
                    urls.append(text[ent.offset: ent.offset + ent.length])
        except Exception:
            continue
    return urls

def extract_all_urls(update: Update) -> List[str]:
    msg = update.effective_message
    text = text_of_message(update)
    urls = extract_urls_from_entities(msg.entities, text)
    urls += extract_urls_from_entities(msg.caption_entities, text)
    urls += URL_RE.findall(text)  # fallback по regex
    cleaned = []
    seen = set()
    for u in urls:
        u = u.strip().strip(".,)>(").lower()
        if u and u not in seen:
            seen.add(u)
            cleaned.append(u)
    return cleaned

def contains_forbidden_invite(urls: List[str]) -> bool:
    return any(any(p in u for p in INVITE_PATTERNS) for u in urls)

def allowed_link(url: str) -> bool:
    """Разрешаем ссылку, если она входит в явный whitelist."""
    u = url.lower()

    # Разрешить конкретные t.me пути (строгий whitelist)
    for path in CONFIG.get("allowed_tme", []):
        if path and path.lower() in u:
            return True

    # Разрешить домены (учитывая поддомены)
    try:
        if not u.startswith(("http://", "https://")):
            u = "http://" + u
        host = urlparse(u).netloc.split(":")[0]
    except Exception:
        host = ""

    for dom in CONFIG.get("allowed_domains", []):
        d = dom.lower().strip()
        if d and (host == d or host.endswith("." + d)):
            return True

    return False

def contains_phone(text: str) -> bool:
    return bool(PHONE_RE.search(text))

def contains_block_keywords(text: str) -> bool:
    low = text.lower()
    for allow in CONFIG.get("keywords_allow", []):
        if allow.lower() in low:
            low = low.replace(allow.lower(), "")
    return any(k.lower() in low for k in CONFIG.get("keywords_block", []))

def is_admin(user_id: int) -> bool:
    return user_id in CONFIG.get("admin_ids", [])

# ---------- КОМАНДЫ ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("👋 Я анти-спам бот. Дайте права Delete Messages и настройте config.yml.")
    else:
        await update.message.reply_text("Привет! Добавь меня в группу и выдай права на удаление. Настройки — в config.yml.")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Ваш Telegram user_id: {user.id if user else '—'}")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    up = _fmt_uptime(time.time() - START_TS)
    await update.message.reply_text(
        f"📊 Статистика\n"
        f"⏱️ Uptime: {up}\n"
        f"🗑️ Удалено сообщений: {DELETE_COUNTER}"
    )

async def getadmins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эта команда работает только в группе/супергруппе.")
        return
    admins = await context.bot.get_chat_administrators(chat.id)
    lines = [f"{adm.user.full_name} — {adm.user.id} ({adm.status})" for adm in admins]
    await update.message.reply_text("Администраторы чата:\n" + "\n".join(lines))

# авто-уведомление админу при старте
async def on_startup(app: Application):
    admin_ids = CONFIG.get("admin_ids", [])
    if not admin_ids:
        return
    try:
        up = _fmt_uptime(time.time() - START_TS)
        await app.bot.send_message(
            chat_id=admin_ids[0],
            text=(
                f"✅ Бот запущен (перезапущен).\n"
                f"⏱️ Uptime (на момент старта): {up}\n"
                f"🗑️ Удалено ранее (с момента старта процесса): {DELETE_COUNTER}"
            )
        )
    except Exception as e:
        logger.warning("Startup notify failed: %s", e)

# ---------- ОСНОВНОЙ ХЕНДЛЕР СООБЩЕНИЙ ----------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DELETE_COUNTER
    msg = update.effective_message
    user = msg.from_user
    chat = update.effective_chat

    # 0) игнор сообщений "от имени канала"
    if msg.sender_chat is not None:
        return

    # 1) боты/пустые — мимо
    if not user or user.is_bot:
        return

    # 2) whitelist: админы из config.yml
    if is_admin(user.id):
        return

    # --- антиспам по альбомам (слишком много фото) ---
    mgid = msg.media_group_id
    if mgid:
        key = f"album:{mgid}"
        rec = context.chat_data.get(key)
        if not rec:
            rec = {"ids": [], "ts": time.time()}
            context.chat_data[key] = rec

        rec["ids"].append(msg.message_id)

        # если превышен лимит, удаляем все фото из альбома
        if len(rec["ids"]) > ALBUM_PHOTO_LIMIT:
            global DELETE_COUNTER
            deleted = 0
            for mid in list(rec["ids"]):
                try:
                    await context.bot.delete_message(chat_id=chat.id, message_id=mid)
                    DELETE_COUNTER += 1
                    deleted += 1
                except Exception:
                    pass

            logger.info(
                "Deleted full album %s in chat %s: %d photos (> %d)",
                mgid, chat.id, len(rec["ids"]), ALBUM_PHOTO_LIMIT
            )

            # удаляем запись, чтобы не трогать её снова
            context.chat_data.pop(key, None)
            return

    text = text_of_message(update)
    urls = extract_all_urls(update)
    has_forbidden_invite = contains_forbidden_invite(urls)
    has_phone = contains_phone(text)
    has_bad_kw = contains_block_keywords(text)
    has_external_link = any(not allowed_link(u) for u in urls)

    should_delete = False
    reasons = []

    if has_forbidden_invite:
        should_delete = True; reasons.append("forbidden invite link")
    if has_external_link:
        should_delete = True; reasons.append("external link")
    if has_phone:
        should_delete = True; reasons.append("phone contact")
    if has_bad_kw:
        should_delete = True; reasons.append("blocked keywords")
    if should_delete:
        try:
            # считаем удалённые сообщения
            DELETE_COUNTER += 1

            await msg.delete()
            logger.info("Deleted in %s by %s. Reasons: %s", chat.id, user.id, ", ".join(reasons))
        except Exception as e:
            logger.error("Failed to delete message: %s", e)
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="⚠️ Не удалось удалить подозрительное сообщение. Убедитесь, что у меня есть права администратора."
                )
            except Exception:
                pass

# ---------- ЗАПУСК ----------

def main():
    app = Application.builder().token(TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping",  ping_cmd))
    app.add_handler(CommandHandler("myid",  myid_cmd))
    app.add_handler(CommandHandler("getadmins", getadmins_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_message))

    logger.info("Bot started. Waiting for updates...")
    print("✅ Anti-spam bot is running. Send /ping to me in Telegram to test.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
