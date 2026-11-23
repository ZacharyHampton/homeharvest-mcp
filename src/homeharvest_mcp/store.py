"""DuckDB cache: fetched rows plus a coverage table of which slices we hold.

Connections are short-lived (opened per operation, closed right after), so
several MCP processes (Claude Desktop + Claude Code + Cursor) can share one
cache file: DuckDB allows one writer process at a time, and we retry on lock.
"""

from __future__ import annotations

import hashlib
import os
import random
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from .schema import STORED_COLUMNS

_LOCK = threading.RLock()  # DuckDB file handles are per process; serialize our own threads


def default_cache_dir() -> Path:
    env = os.environ.get("HOMEHARVEST_MCP_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = Path(base) / "homeharvest-mcp"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return path
    except OSError:
        return Path(tempfile.gettempdir()) / "homeharvest-mcp"


def slice_id(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


SLICES_DDL = """
CREATE TABLE IF NOT EXISTS t_slices (
    slice_id VARCHAR PRIMARY KEY,
    key VARCHAR,
    location_key VARCHAR,
    status VARCHAR,
    bucket VARCHAR,
    filter_sig VARCHAR,
    fetched_at TIMESTAMP,
    updated_at TIMESTAMP,
    next_offset INTEGER,
    source_total INTEGER,
    pages INTEGER,
    rows INTEGER,
    complete BOOLEAN,
    capped_reason VARCHAR,
    children VARCHAR
);
CREATE TABLE IF NOT EXISTS t_locations (
    location_key VARCHAR PRIMARY KEY,
    text VARCHAR,
    area_type VARCHAR,
    lat DOUBLE,
    lon DOUBLE,
    mpr_id VARCHAR,
    fetched_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS t_enriched (
    property_id VARCHAR PRIMARY KEY,
    fetched_at TIMESTAMP
);
"""


def _table_ddl(table: str) -> str:
    cols = list(STORED_COLUMNS[table])
    if table not in ("price_history", "tax_history"):
        cols = [("slice_id", "VARCHAR", "")] + cols
    body = ", ".join(f'"{n}" {t}' for n, t, _ in cols)
    return f"CREATE TABLE IF NOT EXISTS t_{table} ({body});"


class Store:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            d = default_cache_dir()
            d.mkdir(parents=True, exist_ok=True)
            path = d / "cache.duckdb"
        self.path = str(path)
        with self.connect() as con:
            # Drop the cache when the table layout changes (it is only a cache).
            version = hashlib.sha1((repr(STORED_COLUMNS) + SLICES_DDL).encode()).hexdigest()[:12]
            con.execute("CREATE TABLE IF NOT EXISTS t_meta (k VARCHAR PRIMARY KEY, v VARCHAR)")
            row = con.execute("SELECT v FROM t_meta WHERE k = 'schema'").fetchone()
            if not row or row[0] != version:
                for (name,) in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 't\\_%' ESCAPE '\\' AND table_name <> 't_meta'"
                ).fetchall():
                    con.execute(f'DROP TABLE IF EXISTS "{name}"')
                con.execute("DELETE FROM t_meta WHERE k = 'schema'")
                con.execute("INSERT INTO t_meta VALUES ('schema', ?)", [version])
            con.execute(SLICES_DDL)
            for t in STORED_COLUMNS:
                con.execute(_table_ddl(t))

    @contextmanager
    def connect(self, timeout: float = 60.0):
        """Open the cache file, retrying while another process holds it."""
        with _LOCK:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    con = duckdb.connect(self.path, config={"enable_external_access": False})
                    break
                except duckdb.IOException as e:
                    if "lock" not in str(e).lower() or time.monotonic() > deadline:
                        raise
                    time.sleep(0.2 + random.random() * 0.3)
            try:
                yield con
            finally:
                con.close()

    # -- slices ---------------------------------------------------------------
    def get_slice(self, con, sid: str) -> dict | None:
        cur = con.execute("SELECT * FROM t_slices WHERE slice_id = ?", [sid])
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def find_unfiltered(self, con, location_key: str, status: str, bucket: str, fresh_after: datetime) -> dict | None:
        cur = con.execute(
            "SELECT * FROM t_slices WHERE location_key = ? AND status = ? AND bucket = ? AND filter_sig = '' "
            "AND complete AND fetched_at >= ? ORDER BY fetched_at DESC LIMIT 1",
            [location_key, status, bucket, fresh_after],
        )
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def reset_slice(self, con, sid: str, spec, now: datetime):
        for t in ("properties", "agents", "offices", "open_houses"):
            con.execute(f"DELETE FROM t_{t} WHERE slice_id = ?", [sid])
        con.execute("DELETE FROM t_slices WHERE slice_id = ?", [sid])
        con.execute(
            "INSERT INTO t_slices VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, 0, FALSE, NULL, NULL)",
            [sid, spec.key, spec.location_key, spec.status, spec.bucket, spec.filter_sig, now, now],
        )

    def update_slice(self, con, sid: str, **fields):
        fields["updated_at"] = datetime.now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        con.execute(f"UPDATE t_slices SET {sets} WHERE slice_id = ?", [*fields.values(), sid])

    # -- rows -----------------------------------------------------------------
    def insert_rows(self, con, table: str, rows: list[dict], sid: str | None = None):
        if not rows:
            return
        cols = [c[0] for c in STORED_COLUMNS[table]]
        data = {c: [r.get(c) for r in rows] for c in cols}
        if sid is not None:
            data = {"slice_id": [sid] * len(rows), **data}
        df = pd.DataFrame(data)
        con.register("_incoming", df)
        try:
            casts = ", ".join(
                f'CAST(_incoming."{n}" AS {t}) AS "{n}"' for n, t, _ in STORED_COLUMNS[table]
            )
            prefix = "slice_id, " if sid is not None else ""
            con.execute(f"INSERT INTO t_{table} BY NAME SELECT {prefix}{casts} FROM _incoming")
        finally:
            con.unregister("_incoming")

    # -- locations ------------------------------------------------------------
    def get_location(self, con, key: str, fresh_after: datetime):
        return con.execute(
            "SELECT text, area_type, lat, lon, mpr_id FROM t_locations WHERE location_key = ? AND fetched_at >= ?",
            [key, fresh_after],
        ).fetchone()

    def put_location(self, con, key: str, loc):
        c = loc.centroid or {}
        con.execute("DELETE FROM t_locations WHERE location_key = ?", [key])
        con.execute(
            "INSERT INTO t_locations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [key, loc.text, loc.area_type, c.get("lat"), c.get("lon"), loc.mpr_id, datetime.now()],
        )

    # -- history --------------------------------------------------------------
    def missing_history(self, con, ids: list[str], fresh_after: datetime) -> list[str]:
        if not ids:
            return []
        df = pd.DataFrame({"property_id": ids})
        con.register("_ids", df)
        try:
            rows = con.execute(
                "SELECT i.property_id FROM _ids i LEFT JOIN t_enriched e USING (property_id) "
                "WHERE e.property_id IS NULL OR e.fetched_at < ?",
                [fresh_after],
            ).fetchall()
        finally:
            con.unregister("_ids")
        want = {r[0] for r in rows}
        return [i for i in ids if i in want]

    def put_history(self, con, by_id: dict[str, dict], flatten_history):
        if not by_id:
            return
        ids = list(by_id)
        ph, th = [], []
        for pid, data in by_id.items():
            t = flatten_history(pid, data)
            ph += t["price_history"]
            th += t["tax_history"]
        df = pd.DataFrame({"property_id": ids})
        con.register("_ids", df)
        try:
            for t in ("price_history", "tax_history"):
                con.execute(f"DELETE FROM t_{t} WHERE property_id IN (SELECT property_id FROM _ids)")
            con.execute("DELETE FROM t_enriched WHERE property_id IN (SELECT property_id FROM _ids)")
        finally:
            con.unregister("_ids")
        self.insert_rows(con, "price_history", ph)
        self.insert_rows(con, "tax_history", th)
        now = datetime.now()
        self.insert_rows_raw(con, "t_enriched", ["property_id", "fetched_at"], [(i, now) for i in ids])

    def insert_rows_raw(self, con, table: str, cols: list[str], rows: list[tuple]):
        if not rows:
            return
        df = pd.DataFrame(rows, columns=cols)
        con.register("_raw", df)
        try:
            con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _raw")
        finally:
            con.unregister("_raw")

    # -- maintenance ----------------------------------------------------------
    def purge_older_than(self, con, cutoff: datetime):
        old = [r[0] for r in con.execute("SELECT slice_id FROM t_slices WHERE fetched_at < ?", [cutoff]).fetchall()]
        for sid in old:
            for t in ("properties", "agents", "offices", "open_houses"):
                con.execute(f"DELETE FROM t_{t} WHERE slice_id = ?", [sid])
            con.execute("DELETE FROM t_slices WHERE slice_id = ?", [sid])
        return len(old)

    def stats(self) -> dict:
        with self.connect() as con:
            counts = {t: con.execute(f"SELECT count(*) FROM t_{t}").fetchone()[0] for t in STORED_COLUMNS}
            s = con.execute(
                "SELECT count(*), count(*) FILTER (WHERE complete), min(fetched_at), max(fetched_at) FROM t_slices"
            ).fetchone()
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {
            "path": self.path,
            "size_mb": round(size / 1e6, 2),
            "slices": s[0],
            "complete_slices": s[1],
            "oldest_fetch": s[2].isoformat(timespec="seconds") if s[2] else None,
            "newest_fetch": s[3].isoformat(timespec="seconds") if s[3] else None,
            "rows": counts,
        }


def fresh_cutoff(ttl_hours: float) -> datetime:
    return datetime.now() - timedelta(hours=ttl_hours)
