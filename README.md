# gemini-telegram-bot

A Telegram bot powered by the Gemini API with persistent per-chat conversation history.

## Features

- Gemini 2.0 Flash via the `google-genai` SDK
- Per-chat and per-topic (forum mode) conversation history stored in SQLite
- `/start` and `/clear` commands
- MarkdownV2 rendering with plain-text fallback

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
