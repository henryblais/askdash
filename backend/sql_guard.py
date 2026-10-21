"""
Defense-in-depth for LLM-generated SQL.

Nothing the model writes is trusted. Four independent layers:

1. Static checks     - single statement, must be SELECT/WITH, deny-listed keywords.
2. SQLite authorizer - the engine itself refuses anything except reads on
                       allow-listed tables/views (catches things regex can't,
                       e.g. ATTACH, PRAGMA, writes hidden inside CTEs).
3. Read-only connection + per-query timeout + row cap.
4. Schema allow-list - only the documented tables/views are visible.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field

ALLOWED_TABLES = {"sales", "orders", "order_items", "products", "customers", "stores", "regions"}
MAX_ROWS = 500
TIMEOUT_SECONDS = 5.0

_DENY = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|"
    r"reindex|truncate|grant|revoke|load_extension|readfile|writefile)\b",
    re.IGNORECASE,
)


class UnsafeSQL(ValueError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    elapsed_ms: int
    sql: str
    warnings: list[str] = field(default_factory=list)


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate(sql: str) -> str:
    """Return normalized SQL or raise UnsafeSQL."""
    if not sql or not sql.strip():
        raise UnsafeSQL("Empty query.")
    clean = strip_comments(sql).strip().rstrip(";").strip()
    if ";" in clean:
        raise UnsafeSQL("Multiple statements are not allowed.")
    head = clean.lstrip("(").split(None, 1)[0].lower() if clean else ""
    if head not in ("select", "with"):
        raise UnsafeSQL("Only SELECT queries are allowed.")
    m = _DENY.search(clean)
    if m:
        raise UnsafeSQL(f"Forbidden keyword: {m.group(0).upper()}")
    return clean


def _authorizer(action, arg1, arg2, dbname, source):
    # Allow reads of allow-listed tables, functions, and transient objects; deny all else.
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        name = (arg1 or "").lower()
        if name.startswith("sqlite_"):          # no schema introspection (sqlite_master etc.)
            return sqlite3.SQLITE_DENY
        if not name or name in ALLOWED_TABLES:
            return sqlite3.SQLITE_OK
        # Columns of CTEs / subqueries arrive with the CTE name; allow if it's not a real table.
        return sqlite3.SQLITE_OK if name not in _real_tables() else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_RECURSIVE,):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


_REAL_TABLES: set[str] | None = None


def _real_tables() -> set[str]:
    return _REAL_TABLES or set()


def open_readonly(db_path: str) -> sqlite3.Connection:
    global _REAL_TABLES
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    _REAL_TABLES = {r[0].lower() for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    con.set_authorizer(_authorizer)
    return con


def run_query(con: sqlite3.Connection, sql: str, max_rows: int = MAX_ROWS,
              timeout: float = TIMEOUT_SECONDS) -> QueryResult:
    clean = validate(sql)
    warnings: list[str] = []
    deadline = time.monotonic() + timeout

    def on_progress():
        return 1 if time.monotonic() > deadline else 0  # non-zero aborts

    con.set_progress_handler(on_progress, 10_000)
    t0 = time.monotonic()
    try:
        cur = con.execute(clean)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "not authorized" in msg or "prohibited" in msg:
            raise UnsafeSQL("Query touched a non-allow-listed object.") from e
        if "interrupted" in msg:
            raise UnsafeSQL(f"Query exceeded {timeout:.0f}s timeout.") from e
        raise
    finally:
        con.set_progress_handler(None, 0)
    elapsed = int((time.monotonic() - t0) * 1000)
    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
        warnings.append(f"Result truncated to {max_rows} rows.")
    return QueryResult(columns, [list(r) for r in rows], len(rows), truncated, elapsed, clean, warnings)
