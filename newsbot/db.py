"""Хранилище: какие новости видели, отбраковали, опубликовали."""
import sqlite3
import time


class DB:
    def __init__(self, path: str = "data/newsbot.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS items(
                id TEXT PRIMARY KEY,
                url TEXT, title TEXT, summary TEXT, source TEXT,
                published REAL, created REAL,
                status TEXT,            -- new | rejected | approved | published | failed
                score REAL, reason TEXT,
                post TEXT, published_at REAL, message_id INTEGER
            )"""
        )
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(items)")}
        if "image" not in cols:  # обновление старой базы
            self.conn.execute("ALTER TABLE items ADD COLUMN image TEXT")
        self.conn.commit()

    def exists(self, item_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is not None

    def add(self, it: dict):
        self.conn.execute(
            "INSERT OR IGNORE INTO items(id,url,title,summary,source,published,created,status,image) "
            "VALUES(?,?,?,?,?,?,?,'new',?)",
            (it["id"], it["url"], it["title"], it["summary"], it["source"], it["published"], time.time(),
             it.get("image")),
        )
        self.conn.commit()

    def by_status(self, status: str, limit: int = 200):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM items WHERE status=? ORDER BY published DESC LIMIT ?", (status, limit))]

    def update(self, item_id: str, **fields):
        keys = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE items SET {keys} WHERE id=?", (*fields.values(), item_id))
        self.conn.commit()

    def recent_published_titles(self, hours: int = 72):
        since = time.time() - hours * 3600
        return [r["title"] for r in self.conn.execute(
            "SELECT title FROM items WHERE status='published' AND published_at>? ORDER BY published_at DESC",
            (since,))]

    def published_count_since(self, ts: float) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE status='published' AND published_at>?", (ts,)).fetchone()[0]

    def last_published_at(self) -> float:
        r = self.conn.execute("SELECT MAX(published_at) FROM items WHERE status='published'").fetchone()[0]
        return r or 0.0

    def cleanup(self, days: int = 30):
        self.conn.execute("DELETE FROM items WHERE created<?", (time.time() - days * 86400,))
        self.conn.commit()
