import asyncio
import io
import logging
import os
import re

from google import genai
from google.genai import errors as genai_errors, types
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from PIL import Image

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

_MD2_SPECIAL_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')
_CODE_BLOCK_RE = re.compile(r'(```(?:[^\n]*\n)?[\s\S]*?```)')
_INLINE_CODE_RE = re.compile(r'(`[^`\n]+`)')
_FMT_RE = re.compile(
    r'\*\*(?P<bold>.+?)\*\*'
    r'|(?<!\*)\*(?!\*)(?P<italic_s>.+?)(?<!\*)\*(?!\*)'
    r'|(?<!\w)_(?P<italic_u>[^_]+?)_(?!\w)'
    r'|~~(?P<strike>.+?)~~'
    r'|\[(?P<lt>[^\]]+)\]\((?P<lu>[^)]+)\)'
)


def _esc(text: str) -> str:
    """Escape all MarkdownV2 special characters (for plain text regions)."""
    return _MD2_SPECIAL_RE.sub(r'\\\1', text)


def _esc_code(text: str) -> str:
    """Escape \\ and ` inside code/pre entities (Telegram MarkdownV2 requirement)."""
    return text.replace('\\', '\\\\').replace('`', '\\`')


def _esc_url(url: str) -> str:
    """Escape \\ and ) inside inline-link URLs (Telegram MarkdownV2 requirement)."""
    return url.replace('\\', '\\\\').replace(')', '\\)')


def _convert_formatting(text: str) -> str:
    """Convert bold/italic/strike/links to MarkdownV2 and escape the rest."""
    result: list[str] = []
    last = 0
    for m in _FMT_RE.finditer(text):
        result.append(_esc(text[last:m.start()]))
        if m.group('bold'):
            result.append(f"*{_esc(m.group('bold'))}*")
        elif m.group('italic_s'):
            result.append(f"_{_esc(m.group('italic_s'))}_")
        elif m.group('italic_u'):
            result.append(f"_{_esc(m.group('italic_u'))}_")
        elif m.group('strike'):
            result.append(f"~{_esc(m.group('strike'))}~")
        elif m.group('lt'):
            result.append(f"[{_esc(m.group('lt'))}]({_esc_url(m.group('lu'))})")
        last = m.end()
    result.append(_esc(text[last:]))
    return ''.join(result)


def _convert_inline(text: str) -> str:
    """Process text outside code blocks: escape inline code content, convert formatting."""
    result: list[str] = []
    for part in _INLINE_CODE_RE.split(text):
        if len(part) >= 2 and part.startswith('`') and part.endswith('`'):
            inner = part[1:-1]
            result.append(f"`{_esc_code(inner)}`")
        else:
            result.append(_convert_formatting(part))
    return ''.join(result)


def _md_to_mdv2(text: str) -> str:
    """Convert standard Markdown (from Gemini) to Telegram MarkdownV2."""
    result: list[str] = []
    for part in _CODE_BLOCK_RE.split(text):
        if part.startswith('```') and part.endswith('```'):
            inner = part[3:-3]
            result.append(f"```{_esc_code(inner)}```")
        else:
            result.append(_convert_inline(part))
    return ''.join(result)


# ── Message splitting ────────────────────────────────────────────────

MAX_MSG_LEN = 4000


def _split_message(text: str) -> list[str]:
    """Split text into <=MAX_MSG_LEN chunks on paragraph / code-block boundaries."""
    if len(text) <= MAX_MSG_LEN:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= MAX_MSG_LEN:
            chunks.append(remaining)
            break

        slice_ = remaining[:MAX_MSG_LEN]

        # try splitting at a code block boundary (```)
        cut = slice_.rfind("\n```")
        if cut > MAX_MSG_LEN // 4:
            cut += 1  # include the newline, split before ```
        else:
            # try splitting at a paragraph break
            cut = slice_.rfind("\n\n")
        if cut <= MAX_MSG_LEN // 4:
            # try a single newline
            cut = slice_.rfind("\n")
        if cut <= MAX_MSG_LEN // 4:
            # try a space
            cut = slice_.rfind(" ")
        if cut <= 0:
            cut = MAX_MSG_LEN

        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    return chunks


def _fix_chunks(chunks: list[str]) -> list[str]:
    """Close unclosed Markdown formatting at chunk ends, reopen at next chunk start."""
    if len(chunks) <= 1:
        return chunks

    fixed: list[str] = []
    reopen = ""

    for chunk in chunks:
        chunk = reopen + chunk
        reopen = ""
        close = ""

        # Code blocks: if odd count of ```, the block is unclosed
        if chunk.count("```") % 2 == 1:
            close = "\n```"
            reopen = "```\n"
            fixed.append(chunk + close)
            continue

        # Strip complete code blocks for inline analysis
        stripped = re.sub(r'```[\s\S]*?```', '', chunk)

        # Inline code
        if stripped.count('`') % 2 == 1:
            close += "`"
            reopen += "`"

        # Strip inline code for further checks
        stripped = re.sub(r'`[^`]*`', '', stripped)

        # Bold (**)
        if stripped.count('**') % 2 == 1:
            close += "**"
            reopen += "**"

        # Strikethrough (~~)
        if stripped.count('~~') % 2 == 1:
            close += "~~"
            reopen += "~~"

        # Italic (*) — remove ** pairs first, count remaining *
        no_bold = stripped.replace('**', '')
        if no_bold.count('*') % 2 == 1:
            close += "*"
            reopen += "*"

        # Italic (_) — remove __ pairs first, count remaining _
        no_dunder = stripped.replace('__', '')
        if no_dunder.count('_') % 2 == 1:
            close += "_"
            reopen += "_"

        fixed.append(chunk + close)

    return fixed


async def _reply(message: Message, text: str) -> None:
    """Send text as MarkdownV2, splitting long messages and fixing unclosed tags."""
    chunks = _fix_chunks(_split_message(text))
    for chunk in chunks:
        try:
            await message.reply(_md_to_mdv2(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            log.debug("MarkdownV2 conversion failed, trying escaped fallback", exc_info=True)
            try:
                await message.reply(_esc(chunk), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception:
                log.debug("Escaped MarkdownV2 also failed, sending plain text", exc_info=True)
                await message.reply(chunk)


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
        f"Current model: `{_esc_code(current)}`\nChoose a model:",
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
    await callback.message.edit_text(f"Model set to `{_esc_code(model)}`", parse_mode=ParseMode.MARKDOWN_V2)


async def _ask_gemini(
    message: Message,
    chat_id: int,
    thread: int | None,
    user_content: str | list,
    user_text_for_history: str,
) -> None:
    """Shared helper: send content to Gemini, handle errors, persist & reply."""
    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    model_name = get_model(chat_id, thread) or DEFAULT_MODEL
    history = get_history(chat_id, thread)

    try:
        chat = client.chats.create(
            model=model_name,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
            ),
            history=history,
        )
        response = await asyncio.to_thread(chat.send_message, user_content)
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

    save_message(chat_id, thread, "user", user_text_for_history)
    save_message(chat_id, thread, "model", reply_text)

    await _reply(message, reply_text)


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    chat_id = message.chat.id
    thread = _thread_id(message)
    caption = message.caption or "Describe this image"

    # download highest-resolution photo
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)
    image = Image.open(buf)

    await _ask_gemini(
        message,
        chat_id,
        thread,
        user_content=[caption, image],
        user_text_for_history=f"[Image] {caption}",
    )


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    chat_id = message.chat.id
    thread = _thread_id(message)

    await _ask_gemini(
        message,
        chat_id,
        thread,
        user_content=message.text,
        user_text_for_history=message.text,
    )


# ── Entry point ───────────────────────────────────────────────────────


async def main() -> None:
    init_db()
    log.info("Bot starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
