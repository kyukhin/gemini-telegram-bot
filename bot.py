import asyncio
import logging
import os

from google import genai
from google.genai import errors as genai_errors, types
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from typing import Any, Awaitable, Callable

from db import clear_history, get_history, get_model, init_db, save_message, set_model

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ALLOWED_USER_IDS: set[int] = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

client = genai.Client(api_key=GEMINI_API_KEY)

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
MODEL_OPTIONS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]
SYSTEM_INSTRUCTION = (
    "You are a helpful assistant that remembers context. "
    "Answer concisely and use Markdown formatting when appropriate."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


def _thread_id(message: Message) -> int | None:
    return message.message_thread_id if message.is_topic_message else None


# ── Markdown helpers ──────────────────────────────────────────────────

_MARKDOWNV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def _escape_md2(text: str) -> str:
    """Escape MarkdownV2 special characters outside of code blocks."""
    result: list[str] = []
    parts = text.split("```")
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # inside a code block – keep as-is
            result.append(f"```{part}```")
        else:
            # outside code blocks – escape inline code first, then the rest
            segments = part.split("`")
            escaped_segments: list[str] = []
            for j, seg in enumerate(segments):
                if j % 2 == 1:
                    escaped_segments.append(f"`{seg}`")
                else:
                    escaped_segments.append(
                        "".join(f"\\{c}" if c in _MARKDOWNV2_SPECIAL else c for c in seg)
                    )
            result.append("".join(escaped_segments))
    return "".join(result)


async def _reply(message: Message, text: str) -> None:
    """Try sending with MarkdownV2; fall back to plain text on failure."""
    try:
        await message.reply(_escape_md2(text), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        await message.reply(text)


# ── Access control ────────────────────────────────────────────────────

_denied_users: set[int] = set()


def _is_allowed(user_id: int | None) -> bool:
    """Return True if the user is allowed (or if no allowlist is configured)."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id is not None and user_id in ALLOWED_USER_IDS


@dp.my_chat_member(IS_NOT_MEMBER >> IS_MEMBER)
async def on_bot_added(event: ChatMemberUpdated) -> None:
    """Leave the group if the user who added the bot is not allowed."""
    if _is_allowed(event.from_user.id):
        return
    log.warning(
        "Unauthorized user %s added bot to chat %s — leaving",
        event.from_user.id,
        event.chat.id,
    )
    await bot.leave_chat(event.chat.id)


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not ALLOWED_USER_IDS:
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        user_id = user.id if user else None
        if _is_allowed(user_id):
            return await handler(event, data)
        # One-time denial message
        if user_id and user_id not in _denied_users:
            _denied_users.add(user_id)
            log.info("Access denied for user %s", user_id)
            try:
                await event.reply("Sorry, this is a private bot. Access denied.")
            except Exception:
                pass
        return None


dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


# ── Handlers ──────────────────────────────────────────────────────────


@dp.message(F.text == "/start")
async def cmd_start(message: Message) -> None:
    await _reply(
        message,
        "Hello! I'm a Gemini-powered assistant. Send me any message and I'll reply with context awareness.\n\n"
        "Commands:\n/clear — erase conversation history for this chat/topic\n/model — switch the Gemini model",
    )


@dp.message(F.text == "/clear")
async def cmd_clear(message: Message) -> None:
    thread = _thread_id(message)
    deleted = clear_history(message.chat.id, thread)
    await _reply(message, f"Cleared {deleted} messages from history.")


@dp.message(F.text == "/model")
async def cmd_model(message: Message) -> None:
    thread = _thread_id(message)
    current = get_model(message.chat.id, thread) or DEFAULT_MODEL
    buttons = [
        [InlineKeyboardButton(
            text=("• " + m if m == current else m),
            callback_data=f"model:{m}",
        )]
        for m in MODEL_OPTIONS
    ]
    await message.reply(
        f"Current model: `{current}`\nChoose a model:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@dp.callback_query(F.data.startswith("model:"))
async def on_model_selected(callback: CallbackQuery) -> None:
    model = callback.data.removeprefix("model:")
    chat_id = callback.message.chat.id
    thread = callback.message.message_thread_id if callback.message.is_topic_message else None
    set_model(chat_id, thread, model)
    await callback.answer(f"Switched to {model}")
    await callback.message.edit_text(f"Model set to `{_escape_md2(model)}`", parse_mode=ParseMode.MARKDOWN_V2)


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    chat_id = message.chat.id
    thread = _thread_id(message)
    user_text = message.text

    # show typing indicator
    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    # resolve model for this chat/thread
    model_name = get_model(chat_id, thread) or DEFAULT_MODEL

    # load history and build contents for Gemini
    history = get_history(chat_id, thread)

    try:
        chat = client.chats.create(
            model=model_name,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
            ),
            history=history,
        )
        response = await asyncio.to_thread(chat.send_message, user_text)
        reply_text = response.text
    except genai_errors.ClientError as exc:
        if exc.status_code == 404:
            log.warning("Model not found: %s", model_name)
            await _reply(
                message,
                f"Model `{model_name}` was not found. Use /model to pick a valid model.",
            )
        else:
            log.exception("Gemini API client error")
            await _reply(message, "Sorry, something went wrong while generating a response.")
        return
    except Exception:
        log.exception("Gemini API error")
        await _reply(message, "Sorry, something went wrong while generating a response.")
        return

    # persist both messages
    save_message(chat_id, thread, "user", user_text)
    save_message(chat_id, thread, "model", reply_text)

    await _reply(message, reply_text)


# ── Entry point ───────────────────────────────────────────────────────


async def main() -> None:
    init_db()
    log.info("Bot starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
