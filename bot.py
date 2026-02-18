import asyncio
import logging
import os

from google import genai
from google.genai import types
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.types import Message
from dotenv import load_dotenv

from db import clear_history, get_history, init_db, save_message

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-2.0-flash"
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


# ── Handlers ──────────────────────────────────────────────────────────


@dp.message(F.text == "/start")
async def cmd_start(message: Message) -> None:
    await _reply(
        message,
        "Hello! I'm a Gemini-powered assistant. Send me any message and I'll reply with context awareness.\n\n"
        "Commands:\n/clear — erase conversation history for this chat/topic.",
    )


@dp.message(F.text == "/clear")
async def cmd_clear(message: Message) -> None:
    thread = _thread_id(message)
    deleted = clear_history(message.chat.id, thread)
    await _reply(message, f"Cleared {deleted} messages from history.")


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    chat_id = message.chat.id
    thread = _thread_id(message)
    user_text = message.text

    # show typing indicator
    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    # load history and build contents for Gemini
    history = get_history(chat_id, thread)

    try:
        chat = client.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
            ),
            history=history,
        )
        response = await asyncio.to_thread(chat.send_message, user_text)
        reply_text = response.text
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
