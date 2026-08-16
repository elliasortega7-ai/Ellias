"""The SQL part: persistent storage of inverter readings in SQLite.

The in-memory history buffer is lost every time the poller restarts. This
module writes every decoded reading to a SQLite database file on disk, so
history survives restarts and you can run SQL queries against your inverter
data (daily energy, peaks, averages, etc.).

SQLite is used because it needs no server and ships with Python - ideal for
a Raspberry Pi edge gateway. The table columns are generated automatically
from register_map.json, so when you swap in the real inverter's register map
the schema follows along (delete the .db file once to rebuild it).

If you later move to a networked database, the same shape maps onto
Postgres/MySQL or a time-series DB like InfluxDB with minimal change.
"""

import logging
import sqlite3
import threading


class ReadingStore:
    """A tiny SQLite-backed store for decoded inverter readings."""

    def __init__(self, db_path, register_map):
        self.db_path = str(db_path)
        self.columns = [reg["name"] for reg in register_map]
        # check_same_thread=False: the poll thread writes, Flask threads read.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_table()
        logging.info("SQLite store ready at %s", self.db_path)

    def _create_table(self):
        cols = ", ".join(f'"{c}" REAL' for c in self.columns)
        with self._lock, self._conn:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS readings ("
                f"id INTEGER PRIMARY KEY AUTOINCREMENT, "
                f"timestamp REAL NOT NULL, {cols})"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(timestamp)"
            )

    def insert(self, reading):
        """Persist one decoded reading (a dict of name -> value plus timestamp)."""
        fields = ["timestamp"] + self.columns
        placeholders = ", ".join("?" for _ in fields)
        values = [reading.get("timestamp")] + [reading.get(c) for c in self.columns]
        column_list = ", ".join('"' + f + '"' for f in fields)
        sql = f"INSERT INTO readings ({column_list}) VALUES ({placeholders})"
        with self._lock, self._conn:
            self._conn.execute(sql, values)

    def recent(self, limit):
        """Return up to `limit` most recent readings, oldest-first (for charts)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM readings ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in reversed(rows):
            d = {k: row[k] for k in row.keys() if k != "id"}
            out.append(d)
        return out

    def row_count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
