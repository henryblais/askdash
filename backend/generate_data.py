"""
Synthetic retail dataset generator.

Builds a realistic, multi-table e-commerce / retail schema and loads it into
SQLite. Deterministic (seeded) so the demo is reproducible.

Tables:
    regions, stores, products, customers, orders, order_items
Views:
    sales  - one row per order line, pre-joined with revenue/cost/margin.
             This is the table the LLM is steered toward; flattening the
             schema is the single biggest win for text-to-SQL reliability.

Usage:
    python generate_data.py [--db data/retail.db] [--orders 20000] [--seed 42]
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
from datetime import date, timedelta

START_DATE = date(2024, 1, 1)
END_DATE = date(2026, 9, 30)

REGIONS = ["Northeast", "Southeast", "Midwest", "West", "Southwest"]
REGION_WEIGHTS = [0.26, 0.19, 0.20, 0.25, 0.10]

STORES = [
    # (name, region, channel)
    ("Boston Flagship", "Northeast", "Retail"),
    ("NYC SoHo", "Northeast", "Retail"),
    ("Philadelphia Center City", "Northeast", "Retail"),
    ("Atlanta Buckhead", "Southeast", "Retail"),
    ("Miami Design District", "Southeast", "Retail"),
    ("Chicago Loop", "Midwest", "Retail"),
    ("Minneapolis Uptown", "Midwest", "Retail"),
    ("Seattle Capitol Hill", "West", "Retail"),
    ("San Francisco Union Sq", "West", "Retail"),
    ("Los Angeles Melrose", "West", "Retail"),
    ("Austin Domain", "Southwest", "Retail"),
    ("Denver LoDo", "Southwest", "Retail"),
    ("Web Store", "Northeast", "Online"),
    ("Mobile App", "West", "Online"),
]

# category -> subcategory -> list of (product name, unit_cost, unit_price)
CATALOG = {
    "Electronics": {
        "Audio": [("Wireless Earbuds Pro", 42, 129), ("Studio Headphones", 70, 199), ("Portable Speaker", 28, 79)],
        "Wearables": [("Fitness Tracker", 35, 99), ("Smartwatch S3", 110, 299)],
        "Accessories": [("USB-C Hub", 14, 49), ("Fast Charger 65W", 12, 39), ("Laptop Stand", 18, 59)],
    },
    "Home & Kitchen": {
        "Kitchen": [("Espresso Maker", 95, 249), ("Air Fryer XL", 48, 129), ("Chef Knife Set", 40, 119)],
        "Decor": [("Ceramic Vase", 9, 34), ("Linen Throw", 16, 59), ("Scented Candle Trio", 8, 32)],
        "Bedding": [("Bamboo Sheet Set", 38, 109), ("Weighted Blanket", 30, 89)],
    },
    "Apparel": {
        "Outerwear": [("Puffer Jacket", 55, 159), ("Rain Shell", 40, 119)],
        "Footwear": [("Trail Runner", 45, 129), ("Everyday Sneaker", 32, 89)],
        "Basics": [("Merino Tee", 14, 45), ("Jogger Pant", 20, 65), ("Beanie", 5, 24)],
    },
    "Sports & Outdoors": {
        "Fitness": [("Adjustable Dumbbells", 120, 299), ("Yoga Mat Pro", 15, 59), ("Resistance Band Set", 6, 29)],
        "Camping": [("2-Person Tent", 70, 189), ("Insulated Bottle", 9, 34), ("Headlamp", 11, 39)],
    },
    "Beauty": {
        "Skincare": [("Vitamin C Serum", 7, 38), ("Daily SPF 50", 6, 28), ("Night Repair Cream", 12, 54)],
        "Hair": [("Ionic Hair Dryer", 35, 119), ("Argan Oil Treatment", 5, 26)],
    },
}

SEGMENTS = ["Consumer", "Small Business", "Corporate"]
SEGMENT_WEIGHTS = [0.72, 0.20, 0.08]
STATUSES = ["Completed", "Completed", "Completed", "Completed", "Completed", "Returned", "Cancelled"]

FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Avery", "Quinn", "Sam", "Jamie",
               "Drew", "Reese", "Parker", "Rowan", "Skyler", "Emerson", "Hayden", "Finley", "Dakota", "Sage"]
LAST_NAMES = ["Nguyen", "Patel", "Garcia", "Kim", "Okafor", "Silva", "Rossi", "Haddad", "Larsen", "Ivanova",
              "Murphy", "Chen", "Schmidt", "Dubois", "Tanaka", "Mensah", "Novak", "Ortega", "Reyes", "Walsh"]


def seasonality(d: date) -> float:
    """Multiplicative demand factor: holiday bump, summer dip, weekend lift, YoY growth."""
    month_factor = {1: 0.80, 2: 0.78, 3: 0.90, 4: 0.95, 5: 1.00, 6: 0.97, 7: 0.92,
                    8: 0.98, 9: 1.02, 10: 1.08, 11: 1.45, 12: 1.60}[d.month]
    weekend = 1.18 if d.weekday() >= 5 else 1.0
    years = (d - START_DATE).days / 365.25
    growth = (1.14) ** years  # ~14% YoY growth
    return month_factor * weekend * growth


def build(db_path: str, n_orders: int, seed: int) -> None:
    rng = random.Random(seed)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.executescript("""
    CREATE TABLE regions (
        region_id   INTEGER PRIMARY KEY,
        region_name TEXT NOT NULL
    );
    CREATE TABLE stores (
        store_id   INTEGER PRIMARY KEY,
        store_name TEXT NOT NULL,
        region_id  INTEGER NOT NULL REFERENCES regions(region_id),
        channel    TEXT NOT NULL CHECK (channel IN ('Retail','Online'))
    );
    CREATE TABLE products (
        product_id   INTEGER PRIMARY KEY,
        product_name TEXT NOT NULL,
        category     TEXT NOT NULL,
        subcategory  TEXT NOT NULL,
        unit_cost    REAL NOT NULL,
        unit_price   REAL NOT NULL
    );
    CREATE TABLE customers (
        customer_id   INTEGER PRIMARY KEY,
        customer_name TEXT NOT NULL,
        segment       TEXT NOT NULL,
        region_id     INTEGER NOT NULL REFERENCES regions(region_id),
        signup_date   TEXT NOT NULL
    );
    CREATE TABLE orders (
        order_id    INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
        store_id    INTEGER NOT NULL REFERENCES stores(store_id),
        order_date  TEXT NOT NULL,
        status      TEXT NOT NULL
    );
    CREATE TABLE order_items (
        order_item_id INTEGER PRIMARY KEY,
        order_id      INTEGER NOT NULL REFERENCES orders(order_id),
        product_id    INTEGER NOT NULL REFERENCES products(product_id),
        quantity      INTEGER NOT NULL,
        unit_price    REAL NOT NULL,
        discount_pct  REAL NOT NULL DEFAULT 0
    );
    """)

    # regions / stores
    region_ids = {name: i + 1 for i, name in enumerate(REGIONS)}
    cur.executemany("INSERT INTO regions VALUES (?,?)", [(i, n) for n, i in region_ids.items()])
    store_rows = [(i + 1, name, region_ids[reg], ch) for i, (name, reg, ch) in enumerate(STORES)]
    cur.executemany("INSERT INTO stores VALUES (?,?,?,?)", store_rows)
    retail_stores_by_region: dict[int, list[int]] = {}
    online_stores = []
    for sid, _, rid, ch in store_rows:
        if ch == "Online":
            online_stores.append(sid)
        else:
            retail_stores_by_region.setdefault(rid, []).append(sid)

    # products
    product_rows = []
    pid = 1
    for cat, subs in CATALOG.items():
        for sub, items in subs.items():
            for name, cost, price in items:
                # catalog costs are wholesale; landed cost (freight, handling, returns) ~1.5x
                product_rows.append((pid, name, cat, sub, round(cost * 1.5, 2), float(price)))
                pid += 1
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", product_rows)
    # popularity weights: a few hits, long tail
    product_weights = [rng.paretovariate(1.6) for _ in product_rows]

    # customers
    n_customers = max(500, n_orders // 6)
    customer_rows = []
    for cid in range(1, n_customers + 1):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        seg = rng.choices(SEGMENTS, SEGMENT_WEIGHTS)[0]
        rid = region_ids[rng.choices(REGIONS, REGION_WEIGHTS)[0]]
        signup = START_DATE - timedelta(days=rng.randint(0, 700))
        customer_rows.append((cid, name, seg, rid, signup.isoformat()))
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customer_rows)
    customers_by_region: dict[int, list[tuple]] = {}
    for row in customer_rows:
        customers_by_region.setdefault(row[3], []).append(row)

    # orders: sample dates proportional to seasonality
    days = [START_DATE + timedelta(days=i) for i in range((END_DATE - START_DATE).days + 1)]
    day_weights = [seasonality(d) for d in days]

    order_rows, item_rows = [], []
    oid = iid = 1
    for _ in range(n_orders):
        d = rng.choices(days, day_weights)[0]
        rid = region_ids[rng.choices(REGIONS, REGION_WEIGHTS)[0]]
        customer = rng.choice(customers_by_region[rid])
        # online share grows over time: ~35% -> ~50%
        years = (d - START_DATE).days / 365.25
        online_share = 0.35 + 0.06 * years
        if rng.random() < online_share:
            sid = rng.choice(online_stores)
        else:
            sid = rng.choice(retail_stores_by_region[rid])
        status = rng.choice(STATUSES)
        order_rows.append((oid, customer[0], sid, d.isoformat(), status))

        seg_mult = {"Consumer": 1, "Small Business": 2, "Corporate": 4}[customer[2]]
        n_lines = min(6, max(1, int(rng.expovariate(1 / 1.6)) + 1))
        chosen = set()
        for _ in range(n_lines):
            p = rng.choices(product_rows, product_weights)[0]
            if p[0] in chosen:
                continue
            chosen.add(p[0])
            qty = max(1, int(rng.expovariate(1 / (1.3 * seg_mult))))
            # promo discounts cluster in Nov/Dec
            disc = 0.0
            if rng.random() < (0.35 if d.month in (11, 12) else 0.10):
                disc = rng.choice([0.05, 0.10, 0.15, 0.20, 0.25])
            item_rows.append((iid, oid, p[0], qty, p[5], disc))
            iid += 1
        oid += 1

    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", order_rows)
    cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", item_rows)

    cur.executescript("""
    CREATE INDEX idx_orders_date ON orders(order_date);
    CREATE INDEX idx_orders_customer ON orders(customer_id);
    CREATE INDEX idx_items_order ON order_items(order_id);
    CREATE INDEX idx_items_product ON order_items(product_id);

    -- Flattened, analysis-ready view. One row per order line.
    CREATE VIEW sales AS
    SELECT
        oi.order_item_id,
        o.order_id,
        o.order_date,
        strftime('%Y', o.order_date)            AS year,
        strftime('%Y-%m', o.order_date)         AS month,
        'Q' || ((CAST(strftime('%m', o.order_date) AS INTEGER) + 2) / 3) AS quarter,
        o.status,
        c.customer_id,
        c.customer_name,
        c.segment,
        r.region_name                           AS region,
        s.store_name,
        s.channel,
        p.product_name,
        p.category,
        p.subcategory,
        oi.quantity,
        oi.unit_price,
        oi.discount_pct,
        ROUND(oi.quantity * oi.unit_price * (1 - oi.discount_pct), 2) AS revenue,
        ROUND(oi.quantity * p.unit_cost, 2)                            AS cost,
        ROUND(oi.quantity * oi.unit_price * (1 - oi.discount_pct)
              - oi.quantity * p.unit_cost, 2)                          AS gross_margin
    FROM order_items oi
    JOIN orders    o ON o.order_id = oi.order_id
    JOIN customers c ON c.customer_id = o.customer_id
    JOIN stores    s ON s.store_id = o.store_id
    JOIN regions   r ON r.region_id = c.region_id   -- customer's region, so online orders attribute correctly
    JOIN products  p ON p.product_id = oi.product_id;
    """)
    con.commit()

    n_items = cur.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
    rev = cur.execute("SELECT ROUND(SUM(revenue)/1e6,2) FROM sales WHERE status='Completed'").fetchone()[0]
    con.close()
    print(f"Built {db_path}: {len(product_rows)} products, {n_customers} customers, "
          f"{n_orders} orders, {n_items} order lines, ${rev}M completed revenue.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "retail.db"))
    ap.add_argument("--orders", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    build(a.db, a.orders, a.seed)
