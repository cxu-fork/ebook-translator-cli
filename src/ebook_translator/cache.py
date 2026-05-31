"""SQLite-backed translation cache for resume support."""
import hashlib
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Paragraph:
    """A translatable paragraph with its cached translation."""
    id: str
    md5: str
    raw: str          # Original HTML/XML markup
    original: str     # Plain text to translate
    ignored: bool = False
    attributes: str | None = None
    page: str | None = None
    translation: str | None = None
    engine_name: str | None = None
    target_lang: str | None = None


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class TranslationCache:
    """Per-book SQLite cache that enables resume after interruption."""

    def __init__(self, db_path: str, persistence: bool = True):
        self.db_path = db_path
        self.persistence = persistence
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "  id TEXT UNIQUE, md5 TEXT UNIQUE, raw TEXT, original TEXT,"
            "  ignored INTEGER DEFAULT 0, attributes TEXT, page TEXT,"
            "  translation TEXT, engine_name TEXT, target_lang TEXT"
            ")"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS info (key TEXT UNIQUE, value TEXT)"
        )
        self.conn.commit()

    # ---- info helpers ----
    def set_info(self, key: str, value: str):
        with self._lock:
            self.conn.execute(
                "INSERT INTO info VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def get_info(self, key: str) -> str | None:
        cur = self.conn.execute("SELECT value FROM info WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    # ---- paragraph CRUD ----
    def save_paragraphs(self, paragraphs: list[tuple]):
        """Bulk-insert original paragraphs. Each tuple:
        (id, md5, raw, original, ignored, attributes, page)
        Uses INSERT OR IGNORE so existing rows survive (resume-safe).
        """
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO cache "
                "(id, md5, raw, original, ignored, attributes, page, "
                " translation, engine_name, target_lang) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                paragraphs,
            )
            self.conn.commit()

    def get_untranslated(self) -> list[Paragraph]:
        """Return paragraphs that have no translation yet."""
        cur = self.conn.execute(
            "SELECT id, md5, raw, original, ignored, attributes, page, "
            "translation, engine_name, target_lang "
            "FROM cache WHERE NOT ignored AND translation IS NULL"
        )
        return [Paragraph(*row) for row in cur.fetchall()]

    def get_all(self) -> list[Paragraph]:
        """Return all non-ignored paragraphs (translated or not)."""
        cur = self.conn.execute(
            "SELECT id, md5, raw, original, ignored, attributes, page, "
            "translation, engine_name, target_lang "
            "FROM cache WHERE NOT ignored"
        )
        return [Paragraph(*row) for row in cur.fetchall()]

    def get_all_with_ignored(self) -> list[Paragraph]:
        """Return all paragraphs including ignored ones."""
        cur = self.conn.execute(
            "SELECT id, md5, raw, original, ignored, attributes, page, "
            "translation, engine_name, target_lang FROM cache"
        )
        return [Paragraph(*row) for row in cur.fetchall()]

    def update_translation(self, pid: str, translation: str,
                           engine_name: str, target_lang: str):
        with self._lock:
            self.conn.execute(
                "UPDATE cache SET translation=?, engine_name=?, target_lang=? "
                "WHERE id=?",
                (translation, engine_name, target_lang, pid),
            )
            self.conn.commit()

    def translated_count(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM cache WHERE NOT ignored AND translation IS NOT NULL"
        )
        return cur.fetchone()[0]

    def total_count(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM cache WHERE NOT ignored"
        )
        return cur.fetchone()[0]

    def close(self):
        self.conn.close()
