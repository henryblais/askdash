"""
Run with:  python -m pytest tests/        (or)   python -m unittest tests.test_askdash
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from backend import generate_data  # noqa: E402
from backend.core import App  # noqa: E402
from backend.llm import MockPlanner, parse_plan, PlanError  # noqa: E402
from backend.sql_guard import UnsafeSQL, open_readonly, run_query, validate  # noqa: E402

TEST_DB = os.path.join(ROOT, "data", "test_retail.db")


def setUpModule():
    generate_data.build(TEST_DB, n_orders=1500, seed=7)


def tearDownModule():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


class SqlGuardStaticTests(unittest.TestCase):
    def test_accepts_select_and_cte(self):
        self.assertTrue(validate("SELECT 1"))
        self.assertTrue(validate("WITH t AS (SELECT 1 AS x) SELECT x FROM t;"))
        self.assertTrue(validate("  (select 1)  "))

    def test_rejects_writes_and_multi_statement(self):
        for bad in ["DELETE FROM orders", "SELECT 1; DROP TABLE orders", "UPDATE orders SET status='x'",
                    "PRAGMA table_info(orders)", "ATTACH 'x.db' AS y", "SELECT load_extension('x')",
                    "INSERT INTO orders VALUES (1)", "", "   "]:
            with self.assertRaises(UnsafeSQL, msg=bad):
                validate(bad)

    def test_comments_do_not_hide_writes(self):
        with self.assertRaises(UnsafeSQL):
            validate("SELECT 1 /* ; */ ; DROP TABLE orders")
        with self.assertRaises(UnsafeSQL):
            validate("-- harmless\nDROP TABLE orders")


class SqlGuardRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.con = open_readonly(TEST_DB)

    def test_runs_query(self):
        r = run_query(self.con, "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region")
        self.assertEqual(r.columns, ["region", "rev"])
        self.assertEqual(r.row_count, 5)

    def test_row_cap(self):
        r = run_query(self.con, "SELECT * FROM order_items", max_rows=10)
        self.assertTrue(r.truncated)
        self.assertEqual(r.row_count, 10)

    def test_authorizer_blocks_sqlite_master(self):
        with self.assertRaises(UnsafeSQL):
            run_query(self.con, "SELECT sql FROM sqlite_master")

    def test_readonly_connection(self):
        # even if validation were bypassed, the connection can't write
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("CREATE TABLE evil(x)")

    def test_timeout(self):
        slow = ("SELECT COUNT(*) FROM order_items a, order_items b, order_items c "
                "WHERE a.quantity = b.quantity AND b.quantity = c.quantity")
        with self.assertRaises(UnsafeSQL) as cm:
            run_query(self.con, slow, timeout=0.2)
        self.assertIn("timeout", str(cm.exception))


class PlanParsingTests(unittest.TestCase):
    def test_parses_fenced_json(self):
        p = parse_plan('```json\n{"action":"add","title":"T","sql":"SELECT 1","chart":{"type":"bar","x":"a","y":"b"}}\n```')
        self.assertEqual(p.action, "add")
        self.assertEqual(p.chart["y"], ["b"])

    def test_bad_chart_type_falls_back(self):
        p = parse_plan('{"action":"add","sql":"SELECT 1","chart":{"type":"hologram"}}')
        self.assertEqual(p.chart["type"], "bar")

    def test_missing_sql_is_error(self):
        with self.assertRaises(PlanError):
            parse_plan('{"action":"add","title":"x"}')

    def test_prose_becomes_clarify(self):
        p = parse_plan("Sure! Here's a chart.")
        self.assertEqual(p.action, "clarify")
        self.assertIn("Sure!", p.explanation)


class MockPlannerTests(unittest.TestCase):
    def setUp(self):
        self.app = App(TEST_DB, planner=MockPlanner())
        self.panels = self.app.dashboard()["panels"]

    def ask(self, q):
        return self.app.ask(q, self.panels, [])

    def test_default_dashboard(self):
        self.assertEqual(len(self.panels), 4)
        for p in self.panels:
            self.assertGreater(p["data"]["row_count"], 0)

    def test_add_panel(self):
        r = self.ask("Show me gross margin by category for 2025")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["plan"]["action"], "add")
        self.assertEqual(r["panel"]["chart"]["type"], "bar")
        self.assertEqual(r["panel"]["data"]["columns"], ["category", "gross_margin"])

    def test_replace_targets_right_panel(self):
        r = self.ask("Make the revenue by region chart a pie chart")
        self.assertEqual(r["plan"]["action"], "replace")
        self.assertEqual(r["plan"]["panel_id"], "p-revenue-region")
        self.assertEqual(r["panel"]["chart"]["type"], "pie")

    def test_remove(self):
        r = self.ask("Remove the channel chart")
        self.assertEqual(r["plan"]["action"], "remove")
        self.assertEqual(r["plan"]["panel_id"], "p-channel-trend")

    def test_kpi_question(self):
        r = self.ask("What was average order value in Q4 2025?")
        self.assertEqual(r["plan"]["action"], "answer")
        self.assertEqual(r["panel"]["chart"]["type"], "kpi")
        self.assertEqual(r["panel"]["data"]["row_count"], 1)

    def test_top_n(self):
        r = self.ask("Top 10 products by revenue")
        self.assertEqual(r["panel"]["data"]["row_count"], 10)
        self.assertEqual(r["panel"]["chart"]["type"], "hbar")

    def test_two_dimensions(self):
        r = self.ask("Revenue by region and channel")
        self.assertEqual(r["panel"]["chart"]["series"], "channel")

    def test_rate_limit(self):
        self.app.limiter._hits.clear()
        for _ in range(10):
            self.assertTrue(self.ask("revenue by region")["ok"])
        self.assertFalse(self.ask("revenue by region")["ok"])

    def test_power_user_query_guarded(self):
        self.assertTrue(self.app.query("SELECT COUNT(*) FROM sales")["ok"])
        self.assertFalse(self.app.query("DROP TABLE sales")["ok"])


if __name__ == "__main__":
    unittest.main()
