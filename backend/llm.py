"""
Natural language -> (SQL + chart spec) via Claude.

The model never touches the database. It receives:
  - a curated schema description (the flattened `sales` view first),
  - the current dashboard state (so "make that a line chart" resolves),
  - the user's request,
and must return a strict JSON object describing ONE dashboard action.
The SQL it returns is validated and executed by sql_guard, never trusted.

If ANTHROPIC_API_KEY is unset, a deterministic MockPlanner answers a handful
of common requests so the demo runs with zero external dependencies.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from typing import Any

MODEL = os.environ.get("ASKDASH_MODEL", "claude-sonnet-5-5")
API_URL = "https://api.anthropic.com/v1/messages"

CHART_TYPES = {"line", "bar", "hbar", "stacked_bar", "pie", "table", "kpi"}

SCHEMA_DOC = """
PRIMARY TABLE (use this for almost everything) -------------------------------
sales  -- one row per order line item, pre-joined. 2024-01-01 .. 2026-09-30
  order_id        INTEGER
  order_date      TEXT   'YYYY-MM-DD'
  year            TEXT   '2025'
  month           TEXT   '2025-11'   (use for monthly trends; ORDER BY month)
  quarter         TEXT   'Q1'..'Q4'  (combine with year for a specific quarter)
  status          TEXT   'Completed' | 'Returned' | 'Cancelled'
  customer_id     INTEGER
  customer_name   TEXT
  segment         TEXT   'Consumer' | 'Small Business' | 'Corporate'
  region          TEXT   'Northeast' | 'Southeast' | 'Midwest' | 'West' | 'Southwest'
  store_name      TEXT
  channel         TEXT   'Retail' | 'Online'
  product_name    TEXT
  category        TEXT   'Electronics' | 'Home & Kitchen' | 'Apparel' | 'Sports & Outdoors' | 'Beauty'
  subcategory     TEXT
  quantity        INTEGER
  unit_price      REAL
  discount_pct    REAL   0.0 .. 0.25
  revenue         REAL   net of discount
  cost            REAL
  gross_margin    REAL   revenue - cost

BASE TABLES (only if sales is insufficient) -----------------------------------
regions(region_id, region_name)
stores(store_id, store_name, region_id, channel)
products(product_id, product_name, category, subcategory, unit_cost, unit_price)
customers(customer_id, customer_name, segment, region_id, signup_date)
orders(order_id, customer_id, store_id, order_date, status)
order_items(order_item_id, order_id, product_id, quantity, unit_price, discount_pct)

CONVENTIONS
- Dialect: SQLite. Use strftime for dates. ROUND money to 2 places.
- Unless the user says otherwise, filter status = 'Completed' for revenue/margin.
- "Margin %" = SUM(gross_margin) / SUM(revenue) * 100.
- Distinct order count: COUNT(DISTINCT order_id). Average order value: SUM(revenue)/COUNT(DISTINCT order_id).
- Keep results small: aggregate, and LIMIT 50 for rankings.
"""

SYSTEM_PROMPT = f"""You are the analytics engine behind an executive sales dashboard.
Non-technical executives type requests in plain English; you translate each request into
exactly one dashboard action expressed as JSON. You never chat, you only return JSON.

{SCHEMA_DOC}

DASHBOARD ACTIONS
- "add":     add a new panel (default when the request asks to see something new)
- "replace": modify an existing panel (when the user refers to a panel, e.g. "make that a line chart",
             "change the revenue chart to show margin"). Set panel_id to the panel being replaced.
- "remove":  remove a panel. Set panel_id. No sql needed.
- "answer":  the request is a single number / short factual question. Provide sql returning one row and
             a chart of type "kpi".
- "clarify": the request cannot be mapped to the data at all. Put a short question or suggestion
             in "explanation". No sql.

INTERPRETATION FEEDBACK (always):
- "interpreted_as": restate the request as the precise question you actually answered, in one short
  sentence an executive would recognise, e.g. "Completed revenue by product category for 2025."
- "suggestions": 2-3 concrete follow-up or alternative requests, phrased exactly as someone could type
  them (e.g. "Gross margin % by category for 2025"). Vary them: a different cut, a different metric,
  a drill-down.

VAGUE OR OPEN-ENDED REQUESTS ("tell me about our products", "how are we doing?", "anything
interesting in the West?"): do NOT answer in prose. Choose the single most useful panel for that
topic, return it as an "add" action, say in "interpreted_as" how you read the question, and put the
most likely alternative readings in "suggestions" so the user can pick a different one in one click.
Greetings or requests unrelated to sales data get "clarify": a one-sentence note in "explanation"
and 3 good starter requests in "suggestions".

CHART SPEC
  type: one of line, bar, hbar, stacked_bar, pie, table, kpi
  x:      column name for the category/time axis (omit for kpi)
  y:      list of numeric column names to plot
  series: optional column whose distinct values become separate series (for line/stacked_bar)
Choose: line for time trends, bar/hbar for comparisons across categories, stacked_bar for
category-over-time breakdowns, pie only for share-of-total with <= 6 slices, table for lists
with many columns, kpi for a single number.

OUTPUT FORMAT - return ONLY this JSON object, no prose, no markdown fences:
{{
  "action": "add" | "replace" | "remove" | "answer" | "clarify",
  "panel_id": "<id of existing panel, when relevant>",
  "title": "<short panel title, e.g. 'Revenue by Region, Q3 2025'>",
  "sql": "<single SELECT statement>",
  "chart": {{"type": "...", "x": "...", "y": ["..."], "series": "..."}},
  "explanation": "<one plain-English sentence describing what the panel shows>",
  "interpreted_as": "<the precise question you answered>",
  "suggestions": ["<follow-up request>", "<alternative request>", "<drill-down request>"]
}}"""


@dataclass
class Plan:
    action: str
    title: str = ""
    sql: str = ""
    chart: dict[str, Any] | None = None
    panel_id: str | None = None
    explanation: str = ""
    interpreted_as: str = ""
    suggestions: list[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


class PlanError(ValueError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise PlanError("Model did not return JSON.")
        return json.loads(m.group(0))


def parse_plan(text: str) -> Plan:
    try:
        obj = _extract_json(text)
    except PlanError:
        # Model answered in prose. Surface it as a clarification rather than failing.
        prose = re.sub(r"\s+", " ", text).strip()
        if not prose:
            raise
        return Plan(action="clarify", explanation=prose[:400], raw=text)
    action = str(obj.get("action", "add")).lower()
    if action not in {"add", "replace", "remove", "answer", "clarify"}:
        raise PlanError(f"Unknown action {action!r}")
    chart = obj.get("chart") or {}
    if action in {"add", "replace", "answer"}:
        if not obj.get("sql"):
            raise PlanError("Plan is missing SQL.")
        ctype = str(chart.get("type", "bar")).lower()
        if ctype not in CHART_TYPES:
            ctype = "bar"
        chart["type"] = ctype
        if isinstance(chart.get("y"), str):
            chart["y"] = [chart["y"]]
    return Plan(
        action=action,
        title=str(obj.get("title", "")).strip() or "Untitled",
        sql=str(obj.get("sql", "")).strip(),
        chart=chart or None,
        panel_id=obj.get("panel_id") or None,
        explanation=str(obj.get("explanation", "")).strip(),
        interpreted_as=str(obj.get("interpreted_as", "")).strip(),
        suggestions=[str(x).strip() for x in (obj.get("suggestions") or []) if str(x).strip()][:4],
        raw=text,
    )


def _describe_dashboard(panels: list[dict]) -> str:
    if not panels:
        return "(dashboard is empty)"
    lines = []
    for p in panels:
        ch = p.get("chart", {}) or {}
        lines.append(f"- id={p.get('id')} | title={p.get('title')!r} | chart={ch.get('type')} | sql={p.get('sql','')[:300]}")
    return "\n".join(lines)


def build_user_message(request: str, panels: list[dict], history: list[dict] | None = None) -> str:
    hist = ""
    if history:
        hist = "\nRECENT REQUESTS (oldest first):\n" + "\n".join(
            f"- user: {h.get('request')} -> {h.get('action')} {h.get('title')!r}" for h in history[-5:])
    return (f"CURRENT DASHBOARD PANELS:\n{_describe_dashboard(panels)}\n{hist}\n\n"
            f"USER REQUEST: {request}")


# --------------------------------------------------------------------------- #
# Live planner (Anthropic Messages API)
# --------------------------------------------------------------------------- #
class ClaudePlanner:
    def __init__(self, api_key: str, model: str = MODEL):
        self.api_key = api_key
        self.model = model
        try:
            import anthropic  # optional dependency
            self._client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            self._client = None

    @property
    def name(self) -> str:
        return f"claude:{self.model}"

    def plan(self, request: str, panels: list[dict], history: list[dict] | None = None) -> Plan:
        user_msg = build_user_message(request, panels, history)
        if self._client is not None:
            resp = self._client.messages.create(
                model=self.model, max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(getattr(b, "text", "") for b in resp.content)
        else:  # no SDK installed: raw HTTP
            body = json.dumps({
                "model": self.model, "max_tokens": 800,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            }).encode()
            req = urllib.request.Request(API_URL, data=body, headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                try:
                    detail = json.loads(detail).get("error", {}).get("message", detail)
                except (json.JSONDecodeError, AttributeError):
                    pass
                raise PlanError(f"API {e.code}: {detail}") from None
            text = "".join(b.get("text", "") for b in data.get("content", []))
        return parse_plan(text)


# --------------------------------------------------------------------------- #
# Mock planner: keyword rules so the demo runs with no API key.
# --------------------------------------------------------------------------- #
_DIM_WORDS = {
    "region": "region", "regions": "region",
    "category": "category", "categories": "category",
    "channel": "channel", "channels": "channel", "online": "channel", "retail": "channel",
    "segment": "segment", "segments": "segment",
    "store": "store_name", "stores": "store_name",
    "product": "product_name", "products": "product_name",
    "subcategory": "subcategory",
    "month": "month", "monthly": "month", "trend": "month", "over time": "month", "time": "month",
    "quarter": "quarter", "quarterly": "quarter",
    "year": "year", "yearly": "year", "annual": "year",
}
_METRIC_WORDS = [
    ("margin %", "ROUND(SUM(gross_margin)/SUM(revenue)*100,1)", "margin_pct"),
    ("margin percent", "ROUND(SUM(gross_margin)/SUM(revenue)*100,1)", "margin_pct"),
    ("margin", "ROUND(SUM(gross_margin),2)", "gross_margin"),
    ("profit", "ROUND(SUM(gross_margin),2)", "gross_margin"),
    ("units", "SUM(quantity)", "units"),
    ("quantity", "SUM(quantity)", "units"),
    ("orders", "COUNT(DISTINCT order_id)", "orders"),
    ("order count", "COUNT(DISTINCT order_id)", "orders"),
    ("average order value", "ROUND(SUM(revenue)/COUNT(DISTINCT order_id),2)", "avg_order_value"),
    ("aov", "ROUND(SUM(revenue)/COUNT(DISTINCT order_id),2)", "avg_order_value"),
    ("customers", "COUNT(DISTINCT customer_id)", "customers"),
    ("revenue", "ROUND(SUM(revenue),2)", "revenue"),
    ("sales", "ROUND(SUM(revenue),2)", "revenue"),
]


class MockPlanner:
    name = "mock"

    def plan(self, request: str, panels: list[dict], history: list[dict] | None = None) -> Plan:
        q = request.lower().strip()
        last = panels[-1] if panels else None

        # --- panel edits -----------------------------------------------------
        if last and re.search(r"\b(remove|delete|get rid of|hide)\b", q):
            target = self._find_panel(q, panels) or last
            return Plan(action="remove", panel_id=target["id"], title=target["title"],
                        explanation=f"Removed '{target['title']}'.")
        m = re.search(r"\b(line|bar|pie|table|horizontal bar|stacked)\b", q)
        if last and m and re.search(r"\b(make|change|switch|turn|convert|show .* as|instead)\b", q):
            target = self._find_panel(q, panels) or last
            ctype = {"horizontal bar": "hbar", "stacked": "stacked_bar"}.get(m.group(1), m.group(1))
            chart = dict(target.get("chart") or {})
            chart["type"] = ctype
            return Plan(action="replace", panel_id=target["id"], title=target["title"],
                        sql=target["sql"], chart=chart,
                        explanation=f"Changed '{target['title']}' to a {ctype} chart.")

        # --- build a fresh query ---------------------------------------------
        metric_sql, metric_col = "ROUND(SUM(revenue),2)", "revenue"
        metric_matched = False
        for word, sql, col in _METRIC_WORDS:
            if word in q:
                metric_sql, metric_col = sql, col
                metric_matched = True
                break
        dims = []
        for word, col in _DIM_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", q) and col not in dims:
                dims.append(col)
        where = ["status = 'Completed'"]
        label_bits = []
        ym = re.search(r"\b(20\d\d)\b", q)
        if ym:
            where.append(f"year = '{ym.group(1)}'"); label_bits.append(ym.group(1))
        qm = re.search(r"\bq([1-4])\b", q)
        if qm:
            where.append(f"quarter = 'Q{qm.group(1)}'"); label_bits.insert(0, f"Q{qm.group(1)}")
        for val, col in [("online", "channel"), ("retail", "channel"), ("corporate", "segment"),
                         ("consumer", "segment"), ("small business", "segment"),
                         ("electronics", "category"), ("apparel", "category"), ("beauty", "category"),
                         ("home & kitchen", "category"), ("sports", "category"),
                         ("northeast", "region"), ("southeast", "region"), ("midwest", "region"),
                         ("west", "region"), ("southwest", "region")]:
            if re.search(rf"\b{re.escape(val)}\b", q) and (col == "channel" or col not in dims):
                v = {"sports": "Sports & Outdoors", "home & kitchen": "Home & Kitchen"}.get(val, val.title())
                if val == "west" and ("northwest" in q or "southwest" in q or "midwest" in q):
                    continue
                where.append(f"{col} = '{v}'"); label_bits.append(v)
                if col in dims:
                    dims.remove(col)
        if "return" in q:
            where = [w for w in where if not w.startswith("status")] + ["status = 'Returned'"]
            label_bits.append("Returns")

        top = re.search(r"\btop\s+(\d+)\b", q)
        if not (metric_matched or dims or top or len(label_bits) > 0):
            return Plan(action="clarify",
                        explanation="I couldn't map that to the sales data. Try a metric (revenue, margin, "
                                    "orders, units) by a dimension (region, category, channel, segment, "
                                    "product, month).",
                        suggestions=["Revenue by region for 2026", "Top 10 products by gross margin",
                                     "Monthly orders by channel"])
        if top and not dims:
            dims = ["product_name"]
        is_question = re.match(r"^(what|how much|how many|total|overall)\b", q) is not None
        if not dims and is_question:
            metric_label = metric_col.replace("_", " ").title()
            sql = f"SELECT {metric_sql} AS {metric_col} FROM sales WHERE {' AND '.join(where)}"
            title = metric_label + (f" ({', '.join(label_bits)})" if label_bits else "")
            return Plan(action="answer", title=title, sql=sql, chart={"type": "kpi", "y": [metric_col]},
                        explanation=f"Total {metric_label.lower()}"
                                    + (f" for {', '.join(label_bits)}" if label_bits else "") + ".")
        if not dims:
            dims = ["month"]
        if len(dims) > 2:
            dims = dims[:2]

        metric_label = metric_col.replace("_", " ").title()
        title = f"{metric_label} by {' and '.join(d.replace('_name', '').replace('_', ' ').title() for d in dims)}"
        if label_bits:
            title += f" ({', '.join(label_bits)})"

        time_dims = [d for d in dims if d in ("month", "quarter", "year")]
        if top:
            n = int(top.group(1))
            sql = (f"SELECT {dims[0]}, {metric_sql} AS {metric_col} FROM sales WHERE {' AND '.join(where)} "
                   f"GROUP BY {dims[0]} ORDER BY {metric_col} DESC LIMIT {n}")
            chart = {"type": "hbar", "x": dims[0], "y": [metric_col]}
            title = f"Top {n} {dims[0].replace('_name','').title()}s by {metric_label}"
        elif len(dims) == 2:
            x = time_dims[0] if time_dims else dims[0]
            series = [d for d in dims if d != x][0]
            sql = (f"SELECT {x}, {series}, {metric_sql} AS {metric_col} FROM sales WHERE {' AND '.join(where)} "
                   f"GROUP BY {x}, {series} ORDER BY {x}, {series}")
            chart = {"type": "line" if time_dims else "stacked_bar", "x": x, "y": [metric_col], "series": series}
        else:
            d = dims[0]
            order = d if d in time_dims else f"{metric_col} DESC"
            sql = (f"SELECT {d}, {metric_sql} AS {metric_col} FROM sales WHERE {' AND '.join(where)} "
                   f"GROUP BY {d} ORDER BY {order} LIMIT 50")
            chart = {"type": "line" if d in time_dims else "bar", "x": d, "y": [metric_col]}
        if "pie" in q and chart["type"] == "bar":
            chart["type"] = "pie"
        if "table" in q:
            chart["type"] = "table"
        reading = f"{metric_label} by {', '.join(dims)}" + (f" for {', '.join(label_bits)}" if label_bits else "")
        return Plan(action="add", title=title, sql=sql, chart=chart,
                    explanation=f"{metric_label} grouped by {', '.join(dims)}"
                                + (f" filtered to {', '.join(label_bits)}" if label_bits else "") + ".",
                    interpreted_as=reading + " (completed orders).",
                    suggestions=[f"{metric_label} by {d}" + (f" for {', '.join(label_bits)}" if label_bits else "")
                                 for d in ("region", "category", "channel", "segment", "month") if d not in dims][:3])

    @staticmethod
    def _find_panel(q: str, panels: list[dict]) -> dict | None:
        """Best-overlap match between the request and panel titles."""
        best, best_score = None, 0.0
        for p in panels:
            words = {w for w in re.findall(r"[a-z]+", p["title"].lower()) if len(w) > 3}
            if not words:
                continue
            hits = sum(w in q for w in words)
            score = hits / len(words) + hits * 0.01
            if hits and score > best_score:
                best, best_score = p, score
        return best


def get_planner():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return ClaudePlanner(key)
    return MockPlanner()
