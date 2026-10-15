import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                appid INTEGER NOT NULL,
                source TEXT NOT NULL,
                scope TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS observation_lookup
            ON observations(appid, source, scope, id DESC)
        """)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def latest(self, appid, source, scope):
        row = self.connection.execute("""
            SELECT fetched_at, payload FROM observations
            WHERE appid=? AND source=? AND scope=? ORDER BY id DESC LIMIT 1
        """, (appid, source, scope)).fetchone()
        return None if row is None else {
            "fetched_at": row[0], "data": json.loads(row[1])
        }

    def save(self, appid, source, scope, payload, fetched_at=None):
        with self.connection:
            self.connection.execute("""
                INSERT INTO observations(appid, source, scope, fetched_at, payload)
                VALUES (?, ?, ?, ?, ?)
            """, (appid, source, scope, time.time() if fetched_at is None else fetched_at,
                  json.dumps(payload, ensure_ascii=False, allow_nan=False)))

    def export(self):
        rows = self.connection.execute("""
            SELECT o.appid, o.source, o.scope, o.fetched_at, o.payload
            FROM observations o
            JOIN (
                SELECT MAX(id) AS id FROM observations GROUP BY appid, source, scope
            ) latest ON o.id=latest.id
            ORDER BY o.appid, o.source, o.scope
        """)
        current = None
        for appid, source, scope, fetched_at, payload in rows:
            if current is None or current["appid"] != appid:
                if current is not None:
                    yield current
                current = {"appid": appid, "sources": {}}
            current["sources"].setdefault(source, {})[scope] = {
                "fetched_at": fetched_at, "data": json.loads(payload)
            }
        if current is not None:
            yield current
