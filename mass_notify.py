"""Mass notifications for a python-telegram-bot 21.4 group bot.

The Bot API cannot enumerate every group member.  This module therefore keeps a
small persistent directory of users seen in messages or chat-member updates.
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from telegram import Update, User
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ChatMemberHandler, CommandHandler, ContextTypes


KNOWN_USERS_FILE = Path("known_users.json")
TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_MENTIONS_PER_MESSAGE = 7
MAX_MENTIONS_PER_MESSAGE = 10

CONFIG: dict[str, Any] = {}
_is_admin: Callable[[int], bool] | None = None
known_users: dict[str, dict[str, dict[str, Any]]] = {}


def configure_mass_notify(
    config: dict[str, Any],
    is_admin_func: Callable[[int], bool],
    known_users_file: str | Path = KNOWN_USERS_FILE,
) -> None:
    """Connect this module to the host bot's CONFIG and is_admin function."""
    global CONFIG, _is_admin, KNOWN_USERS_FILE, known_users
    CONFIG = config
    _is_admin = is_admin_func
    KNOWN_USERS_FILE = Path(known_users_file)
    known_users = load_known_users()


def load_known_users() -> dict[str, dict[str, dict[str, Any]]]:
    """Load the known-user directory; keep running if the file is damaged."""
    if not KNOWN_USERS_FILE.exists():
        return {}
    try:
        data = json.loads(KNOWN_USERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_known_users() -> None:
    """Persist the complete directory using an atomic file replacement."""
    KNOWN_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = KNOWN_USERS_FILE.with_suffix(KNOWN_USERS_FILE.suffix + ".tmp")
    temporary_file.write_text(
        json.dumps(known_users, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(KNOWN_USERS_FILE)


def remember_user(chat_id: int, user: User) -> None:
    """Remember a non-bot user observed in a group."""
    if not user or user.is_bot:
        return

    chat_key = str(chat_id)
    user_key = str(user.id)
    chat_users = known_users.setdefault(chat_key, {})
    chat_users[user_key] = {
        "user_id": user.id,
        "full_name": user.full_name,
        "username": user.username,
        "chat_id": chat_id,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "is_bot": False,
    }
    try:
        save_known_users()
    except OSError:
        # A temporary filesystem problem must not break anti-spam processing.
        pass


def get_known_users_for_chat(chat_id: int) -> list[dict[str, Any]]:
    """Return users for a chat, most recently seen first."""
    users = known_users.get(str(chat_id), {}).values()
    return sorted(users, key=lambda item: item.get("last_seen", ""), reverse=True)


def build_hidden_mentions(users: Iterable[dict[str, Any]]) -> str:
    """Build invisible HTML links which trigger Telegram mentions."""
    return "".join(
        f'<a href="tg://user?id={int(user["user_id"])}">\u200e</a>'
        for user in users
        if not user.get("is_bot", False)
    )


def _admin_allowed(user_id: int) -> bool:
    return bool(_is_admin and _is_admin(user_id))


async def _require_admin(update: Update) -> bool:
    user = update.effective_user
    if user and _admin_allowed(user.id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ Команда доступна только администраторам.",
            parse_mode=ParseMode.HTML,
        )
    return False


async def _eligible_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    chat = update.effective_chat
    if not chat:
        return []
    users = get_known_users_for_chat(chat.id)
    bot_id = context.bot.id
    users = [u for u in users if not u.get("is_bot") and u.get("user_id") != bot_id]

    if CONFIG.get("skip_admins_in_tagall", False):
        try:
            admin_ids = {member.user.id for member in await context.bot.get_chat_administrators(chat.id)}
            users = [u for u in users if u.get("user_id") not in admin_ids]
        except TelegramError:
            # Failure to read admins should not turn the whole call into a failure.
            pass
    return users


def _batch_messages(text: str, users: list[dict[str, Any]], batch_size: int) -> list[str]:
    """Build batches while respecting both mention-count and 4096-char limits."""
    escaped_text = html.escape(text)
    messages: list[str] = []
    current: list[dict[str, Any]] = []

    for user in users:
        candidate = current + [user]
        candidate_message = f"{escaped_text}\n{build_hidden_mentions(candidate)}"
        if current and (len(candidate) > batch_size or len(candidate_message) > TELEGRAM_MESSAGE_LIMIT):
            messages.append(f"{escaped_text}\n{build_hidden_mentions(current)}")
            current = [user]
        else:
            current = candidate

        if len(f"{escaped_text}\n{build_hidden_mentions(current)}") > TELEGRAM_MESSAGE_LIMIT:
            raise ValueError("Текст слишком длинный для сообщения Telegram.")

    if current:
        messages.append(f"{escaped_text}\n{build_hidden_mentions(current)}")
    return messages


async def _run_call(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    messages: list[str],
) -> None:
    chat_data = context.chat_data
    try:
        for index, message in enumerate(messages):
            if chat_data.get("tagall_stop_requested"):
                break
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
            if index + 1 < len(messages):
                await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        # /stopcall cancels the background task immediately.
        pass
    except TelegramError as error:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Не удалось продолжить созыв: {html.escape(str(error))}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    finally:
        chat_data["tagall_running"] = False
        chat_data["tagall_stop_requested"] = False
        chat_data.pop("tagall_task", None)


async def _start_call(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    users: list[dict[str, Any]],
    batch_size: int,
    text: str,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if context.chat_data.get("tagall_running"):
        await message.reply_text(
            "Созыв уже идёт. Используйте /stopcall, чтобы остановить его.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not users:
        await message.reply_text(
            "Пока бот знает 0 участников. База будет пополняться, когда пользователи "
            "пишут сообщения или вступают в чат.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        messages = _batch_messages(text, users, batch_size)
    except ValueError as error:
        await message.reply_text(html.escape(str(error)), parse_mode=ParseMode.HTML)
        return

    context.chat_data["tagall_running"] = True
    context.chat_data["tagall_stop_requested"] = False
    task = context.application.create_task(
        _run_call(context, chat.id, messages),
        update=update,
        name=f"tagall-{chat.id}",
    )
    context.chat_data["tagall_task"] = task


async def tagall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    message = update.effective_message
    if not message:
        return
    text = " ".join(context.args).strip()
    if not text:
        await message.reply_text("Использование: /tagall текст", parse_mode=ParseMode.HTML)
        return

    users = await _eligible_users(update, context)
    try:
        configured_size = int(CONFIG.get("tagall_batch_size", DEFAULT_MENTIONS_PER_MESSAGE))
    except (TypeError, ValueError):
        configured_size = DEFAULT_MENTIONS_PER_MESSAGE
    batch_size = max(DEFAULT_MENTIONS_PER_MESSAGE, min(configured_size, MAX_MENTIONS_PER_MESSAGE))
    await _start_call(update, context, users, batch_size, text)


async def call_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    message = update.effective_message
    if not message:
        return
    if len(context.args) < 3:
        await message.reply_text(
            "Использование: /call 100 7 Срочное объявление",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        count = int(context.args[0])
        batch_size = int(context.args[1])
    except ValueError:
        count = batch_size = 0
    if count < 1 or batch_size < 1 or batch_size > MAX_MENTIONS_PER_MESSAGE:
        await message.reply_text(
            "Количество участников должно быть больше 0, а размер пачки — от 1 до 10.",
            parse_mode=ParseMode.HTML,
        )
        return

    text = " ".join(context.args[2:]).strip()
    users = (await _eligible_users(update, context))[:count]
    await _start_call(update, context, users, batch_size, text)


async def knownusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    users = get_known_users_for_chat(chat.id)
    if not users:
        await message.reply_text(
            "Пока бот знает 0 участников. База будет пополняться, когда пользователи "
            "пишут сообщения или вступают в чат.",
            parse_mode=ParseMode.HTML,
        )
        return
    last_seen = max(user.get("last_seen", "") for user in users)
    try:
        updated = datetime.fromisoformat(last_seen).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        updated = "неизвестно"
    await message.reply_text(
        f"Бот знает {len(users)} участников в этом чате\n"
        f"Последнее обновление базы: {html.escape(updated)}",
        parse_mode=ParseMode.HTML,
    )


async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    text = " ".join(context.args).strip()
    if not text:
        await message.reply_text("Использование: /announce текст", parse_mode=ParseMode.HTML)
        return
    escaped_text = html.escape(text)
    if len(escaped_text) > TELEGRAM_MESSAGE_LIMIT:
        await message.reply_text("Текст слишком длинный для сообщения Telegram.", parse_mode=ParseMode.HTML)
        return
    try:
        announcement = await context.bot.send_message(
            chat_id=chat.id,
            text=escaped_text,
            parse_mode=ParseMode.HTML,
        )
        await context.bot.pin_chat_message(
            chat_id=chat.id,
            message_id=announcement.message_id,
            disable_notification=False,
        )
    except (BadRequest, Forbidden) as error:
        await message.reply_text(
            "Не удалось закрепить сообщение. Проверьте, что бот — администратор и имеет "
            f"право закреплять сообщения.\n{html.escape(str(error))}",
            parse_mode=ParseMode.HTML,
        )


async def stopcall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    message = update.effective_message
    if not message:
        return
    task = context.chat_data.get("tagall_task")
    if not context.chat_data.get("tagall_running") or not task:
        await message.reply_text("Активного созыва нет.", parse_mode=ParseMode.HTML)
        return
    context.chat_data["tagall_stop_requested"] = True
    task.cancel()
    await message.reply_text("Созыв остановлен.", parse_mode=ParseMode.HTML)


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember a user after they join or become a member of the chat."""
    change = update.chat_member
    if not change:
        return
    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status
    member_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.RESTRICTED,
    }
    if new_status in member_statuses and old_status not in member_statuses:
        remember_user(change.chat.id, change.new_chat_member.user)


def register_mass_notify_handlers(application: Any) -> None:
    """Register all handlers; call configure_mass_notify first."""
    application.add_handler(CommandHandler("tagall", tagall_cmd))
    application.add_handler(CommandHandler("call", call_cmd))
    application.add_handler(CommandHandler("knownusers", knownusers_cmd))
    application.add_handler(CommandHandler("announce", announce_cmd))
    application.add_handler(CommandHandler("stopcall", stopcall_cmd))
    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))
