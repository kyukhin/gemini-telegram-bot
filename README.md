# gemini-telegram-bot

A Telegram bot powered by the Gemini API with persistent per-chat conversation history.

## Features

- Gemini 2.0 Flash via the `google-genai` SDK
- Per-chat and per-topic (forum mode) conversation history stored in SQLite
- `/start`, `/clear`, and `/model` commands
- Dynamic per-chat model switching with inline keyboard
- Vision support — send a photo and the bot will analyze it
- Smart message splitting for long responses with unclosed-tag repair
- MarkdownV2 formatting — bold, italic, code, links preserved from Gemini output

## Obtaining API keys

### Telegram Bot Token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts — choose a display name and a username (must end in `bot`).
3. BotFather will reply with a token like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. Copy it.
4. Paste the token as `TELEGRAM_TOKEN` in your `.env` file.

### Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/apikey).
2. Sign in with your Google account and click **Create API key**.
3. Select or create a Google Cloud project when prompted.
4. Copy the generated key.
5. Paste the key as `GEMINI_API_KEY` in your `.env` file.

## Commands

| Command  | Description |
|----------|-------------|
| `/start` | Show a welcome message with available commands |
| `/clear` | Erase conversation history for the current chat/topic |
| `/model` | Switch the Gemini model for the current chat/topic |

### `/model` — Dynamic model selection

Send `/model` to see an inline keyboard with available models. The currently active model is marked with a bullet (`•`). Tap a button to switch — the choice is saved per chat (and per forum topic) and persists across bot restarts.

Available models: `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.0-flash`.

The default model is `gemini-2.0-flash` (or whatever is set via the `GEMINI_MODEL` environment variable). If a previously selected model becomes unavailable, the bot will reply with a friendly error suggesting you run `/model` again.

## Vision (photo analysis)

Send a photo to the bot and it will analyze it using Gemini's multimodal capabilities. Add a caption to use as the prompt (e.g. "What's in this image?" or "Translate the text in this photo"). If no caption is provided, the bot defaults to "Describe this image".

The bot remembers image context — you can send a photo, then ask follow-up questions about it in plain text.

## Markdown formatting

All responses are sent using Telegram's MarkdownV2 parse mode. The bot converts Gemini's standard Markdown output:

- `**bold**` renders as **bold**
- `*italic*` and `_italic_` render as italic
- `` `inline code` `` and fenced code blocks are preserved as-is (no escaping inside)
- `[links](url)` are rendered as clickable links
- `~~strikethrough~~` renders as strikethrough
- All other special characters (`_*[]()~>#+-=|{}.!`) are escaped automatically

If MarkdownV2 parsing fails for a particular message, the bot falls back to plain text.

## Long message handling

Gemini can produce responses that exceed Telegram's 4096-character message limit. The bot automatically splits long replies into multiple messages, preferring to break at:

1. Code block boundaries — keeps code blocks intact
2. Paragraph breaks
3. Line breaks
4. Spaces — avoids splitting mid-word

When a split occurs mid-formatting (e.g. inside a bold section or code block), the bot automatically closes the open tag at the end of the chunk and reopens it at the start of the next one, preventing MarkdownV2 parse errors.

## Using with forum topics (group channels)

The bot supports Telegram's **Topics** (forum mode), which lets you run multiple independent conversations in a single group. Each topic gets its own conversation history, model selection, and `/clear` scope — so you can use one topic for coding help with `gemini-2.5-pro` and another for casual chat with `gemini-2.5-flash`.

### Setup

1. **Disable privacy mode** — open **@BotFather**, send `/mybots`, select your bot → *Bot Settings* → *Group Privacy* → **Turn off**. By default bots only see commands; with privacy mode disabled the bot can read all messages in the group.
2. **Create a group** (or use an existing one). Open group settings and enable **Topics** (under Edit → Topics). Private groups work fine.
3. **Add the bot** to the group.
4. **Promote the bot to admin** — go to the group's member list, tap the bot, and select *Promote to Admin*. Enable at least:
   - **Read messages** — so the bot can see messages in topics.
   - **Send messages** — so the bot can reply.
5. **Create topics** — each topic acts as a separate chat. Create as many as you need (e.g. "Code review", "Brainstorm", "Translation").
6. **Start chatting** — send messages in any topic and the bot will reply with independent context per topic.

> **Note:** If you skip step 1, the bot will only respond to `/commands` and will ignore regular messages.

### How it works

- Conversation history is tracked per topic — messages in "Code review" are invisible to the "Brainstorm" topic.
- `/model` sets the model for the current topic only. You can use different models in different topics simultaneously.
- `/clear` only erases history for the topic where you run it.

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_TOKEN and GEMINI_API_KEY
python3 bot.py
```

## Deploying with systemd

1. Copy the project to the server:

```bash
scp -r . user@server:/opt/gemini-bot/
```

2. On the server, create a virtual environment and install dependencies:

```bash
cd /opt/gemini-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Create the `.env` file with your tokens:

```bash
cp .env.example .env
# edit .env and set TELEGRAM_TOKEN and GEMINI_API_KEY
```

4. Create a dedicated service user (optional but recommended):

```bash
sudo useradd -r -s /usr/sbin/nologin bot
sudo chown -R bot:bot /opt/gemini-bot
```

5. Install the systemd unit file:

```bash
sudo cp /opt/gemini-bot/gemini-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
```

6. Enable and start the service:

```bash
sudo systemctl enable --now gemini-bot
```

7. Check the status and logs:

```bash
sudo systemctl status gemini-bot
sudo journalctl -u gemini-bot -f
```

To restart after code changes:

```bash
sudo systemctl restart gemini-bot
```
