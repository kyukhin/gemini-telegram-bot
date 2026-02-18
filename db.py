import sqlite3
from pathlib import Path

from google.genai import types

DB_PATH = Path(__file__).parent / "conversations.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                role TEXT NOT NULL CHECK(role IN ('user', 'model')),
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_thread
            ON conversations (chat_id, thread_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                current_model TEXT NOT NULL,
                UNIQUE(chat_id, thread_id)
            )
            """
        )


def save_message(chat_id: int, thread_id: int | None, role: str, content: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations (chat_id, thread_id, role, content) VALUES (?, ?, ?, ?)",
            (chat_id, thread_id, role, content),
        )


def get_history(chat_id: int, thread_id: int | None, limit: int = 50) -> list[types.Content]:
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id
                FROM conversations
                WHERE chat_id = ? AND thread_id IS ?
                ORDER BY id DESC
                LIMIT ?
            ) sub ORDER BY id ASC
            """,
            (chat_id, thread_id, limit),
        ).fetchall()
    return [
        types.Content(role=role, parts=[types.Part(text=content)])
        for role, content in rows
    ]


def get_model(chat_id: int, thread_id: int | None) -> str | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT current_model FROM settings WHERE chat_id = ? AND thread_id IS ?",
            (chat_id, thread_id),
        ).fetchone()
    return row[0] if row else None


def set_model(chat_id: int, thread_id: int | None, model: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (chat_id, thread_id, current_model) VALUES (?, ?, ?)",
            (chat_id, thread_id, model),
        )


def clear_history(chat_id: int, thread_id: int | None) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE chat_id = ? AND thread_id IS ?",
            (chat_id, thread_id),
        )
        return cur.rowcount
