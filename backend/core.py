"""
Framework-agnostic application core.

Every HTTP endpoint (FastAPI in app.py, stdlib in devserver.py) is a thin
wrapper over the functions here, which take/return plain dicts.

Design note: the dashboard state lives in the browser and is sent with every
request. The server is stateless, which keeps hosting trivial (any free tier)
and makes the LLM context explicit: whatever panels the client sends is what
the model can see and modify.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import defaultdict, deque

from . import llm
from .sql_guard import UnsafeSQL, open_readonly, run_query

DB_PATH = os.environ.get("ASKDASH_DB", os.path.join(os.path.dirname(__file__), "..", "data", "retail.db"))

# Abuse controls for a public demo
RATE_LIMIT_PER_MIN = int(os.environ.get("ASKDASH_RATE_PER_MIN", "10"))
DAILY_LLM_CAP = int(os.environ.get("ASKDASH_DAILY_CAP", "500"))

DEFAULT_PANELS = [
    {
        "id": "p-revenue-trend",
        "title": "Monthly Revenue",
        "sql": "SELECT month, ROUND(SUM(revenue),2) AS revenue FROM sales "
               "WHERE status = 'Completed' GROUP BY month ORDER BY month",
        "chart": {"type": "line", "x": "month", "y": ["revenue"]},
        "explanation": "Completed revenue by month across the whole business.",
    },
    {
        "id": "p-revenue-region",
        "title": "Revenue by Region (2026 YTD)",
        "sql": "SELECT region, ROUND(SUM(revenue),2) AS revenue FROM sales "
               "WHERE status = 'Completed' AND year = '2026' GROUP BY region ORDER BY revenue DESC",
        "chart": {"type": "bar", "x": "region", "y": ["revenue"]},
        "explanation": "Year-to-date completed revenue by customer region.",
    },
    {
        "id": "p-category-mix",
        "title": "Revenue Share by Category (2026 YTD)",
        "sql": "SELECT category, ROUND(SUM(revenue),2) AS revenue FROM sales "
               "WHERE status = 'Completed' AND year = '2026' GROUP BY category ORDER BY revenue DESC",
        "chart": {"type": "pie", "x": "category", "y": ["revenue"]},
        "explanation": "Share of year-to-date revenue by product category.",
    },
    {
        "id": "p-channel-trend",
        "title": "Monthly Revenue by Channel",
        "sql": "SELECT month, channel, ROUND(SUM(revenue),2) AS revenue FROM sales "
               "WHERE status = 'Completed' GROUP BY month, channel ORDER BY month, channel",
        "chart": {"type": "line", "x": "month", "y": ["revenue"], "series": "channel"},
        "explanation": "Online vs. retail revenue over time.",
    },
]

SUGGESTIONS = [
    "Show me gross margin by category for 2025",
    "Top 10 products by revenue",
    "Make the revenue by region chart a pie chart",
    "Monthly orders by segment",
    "What was average order value in Q4 2025?",
    "Revenue by region and channel",
    "Remove the channel chart",
]


class _RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._daily = 0
        self._daily_day = time.strftime("%Y-%m-%d")
        self._lock = threading.Lock()

    def check(self, client_id: str) -> str | None:
        """Return an error string if the request should be refused."""
        now = time.time()
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._daily_day:
                self._daily, self._daily_day = 0, today
            q = self._hits[client_id]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= RATE_LIMIT_PER_MIN:
                return f"Rate limit: max {RATE_LIMIT_PER_MIN} requests per minute. Try again shortly."
            if self._daily >= DAILY_LLM_CAP:
                return "This demo has hit its daily request budget. Please try again tomorrow."
            q.append(now)
            self._daily += 1
        return None


class App:
    def __init__(self, db_path: str = DB_PATH, planner=None):
        self.db_path = db_path
        self.con = open_readonly(db_path)
        self.lock = threading.Lock()
        self.planner = planner or llm.get_planner()
        self.limiter = _RateLimiter()

    # ---- read-only helpers --------------------------------------------------
    def schema(self) -> dict:
        return {"schema": llm.SCHEMA_DOC.strip(), "planner": self.planner.name,
                "suggestions": SUGGESTIONS}

    def _execute(self, sql: str) -> dict:
        with self.lock:
            r = run_query(self.con, sql)
        return {"columns": r.columns, "rows": r.rows, "row_count": r.row_count,
                "truncated": r.truncated, "elapsed_ms": r.elapsed_ms, "sql": r.sql,
                "warnings": r.warnings}

    def dashboard(self) -> dict:
        panels = []
        for p in DEFAULT_PANELS:
            panel = dict(p)
            panel["data"] = self._execute(p["sql"])
            panels.append(panel)
        return {"panels": panels, "planner": self.planner.name, "suggestions": SUGGESTIONS}

    def query(self, sql: str) -> dict:
        """Run a user-edited SQL statement (power-user path, same guard)."""
        try:
            return {"ok": True, "data": self._execute(sql)}
        except UnsafeSQL as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # sqlite syntax errors etc.
            return {"ok": False, "error": f"SQL error: {e}"}

    # ---- the main event -----------------------------------------------------
    def ask(self, request: str, panels: list[dict], history: list[dict] | None,
            client_id: str = "anon") -> dict:
        request = (request or "").strip()
        if not request:
            return {"ok": False, "error": "Empty request."}
        if len(request) > 500:
            return {"ok": False, "error": "Request too long (500 characters max)."}
        limited = self.limiter.check(client_id)
        if limited:
            return {"ok": False, "error": limited}

        # Strip bulky data from the panels before handing them to the model.
        slim = [{k: p.get(k) for k in ("id", "title", "sql", "chart")} for p in (panels or [])]

        t0 = time.time()
        try:
            plan = self.planner.plan(request, slim, history)
        except llm.PlanError as e:
            return {"ok": False, "error": f"Couldn't interpret that request: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"Model call failed: {type(e).__name__}: {e}"}
        llm_ms = int((time.time() - t0) * 1000)

        out = {"ok": True, "plan": plan.to_dict(), "llm_ms": llm_ms, "planner": self.planner.name}

        if plan.action == "clarify":
            return out
        if plan.action == "remove":
            if not plan.panel_id:
                return {"ok": False, "error": "Which panel should I remove?"}
            return out

        # add / replace / answer: run the SQL, with one retry on SQL error.
        try:
            data = self._execute(plan.sql)
        except UnsafeSQL as e:
            return {"ok": False, "error": f"Generated SQL was rejected by the safety layer: {e}",
                    "plan": plan.to_dict()}
        except Exception as e:
            fixed = self._retry_with_error(request, slim, history, plan, str(e))
            if fixed is None:
                return {"ok": False, "error": f"Generated SQL failed: {e}", "plan": plan.to_dict()}
            plan, data = fixed
            out["plan"] = plan.to_dict()
            out["retried"] = True

        panel = {
            "id": plan.panel_id if (plan.action == "replace" and plan.panel_id) else f"p-{uuid.uuid4().hex[:8]}",
            "title": plan.title,
            "sql": data["sql"],
            "chart": plan.chart or {"type": "table"},
            "explanation": plan.explanation,
            "data": data,
            "source": "llm",
        }
        out["panel"] = panel
        return out

    def _retry_with_error(self, request, slim, history, plan, error):
        """Feed the SQL error back to the model once; mock planner has no retry."""
        if not isinstance(self.planner, llm.ClaudePlanner):
            return None
        hint = (f"{request}\n\n(Your previous SQL failed with this SQLite error: {error}\n"
                f"Previous SQL: {plan.sql}\nReturn a corrected plan.)")
        try:
            plan2 = self.planner.plan(hint, slim, history)
            data = self._execute(plan2.sql)
            return plan2, data
        except Exception:
            return None
