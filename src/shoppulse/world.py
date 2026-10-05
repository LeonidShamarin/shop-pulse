"""A fictional online shop, generated deterministically into SQLite.

The tables mirror what the real sources hand over:
* `oc_product`, `oc_order`, `oc_order_product` follow OpenCart's own tables
  (trimmed to the columns the dashboard reads);
* `ad_spend` is the shape of a Google Sheet a marketer keeps by hand: one row per
  day and channel.

Three incidents are planted on purpose, so the alert rules can be checked against
a known answer instead of against "looks plausible":
1. SITE_DOWN_DAY: the checkout broke for most of a day, orders fell to ~25%.
2. STOCKOUT: the best seller ran out and was not restocked; its sales stop.
3. META_FATIGUE: Meta spend keeps growing over the last 3 weeks while the orders
   it brings fall, so its return on ad spend sinks below 2.
"""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

END = date(2026, 10, 4)          # last full day of data; fixed so the demo is stable
DAYS = 180
SITE_DOWN_DAY = date(2026, 9, 12)
STOCKOUT_SKU = "KT-1001"
STOCKOUT_FROM = date(2026, 9, 27)
META_FATIGUE_FROM = date(2026, 9, 14)

SCHEMA = """
CREATE TABLE oc_product (
    product_id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    quantity INTEGER NOT NULL
);
CREATE TABLE oc_order (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    date_added TEXT NOT NULL,
    order_status TEXT NOT NULL,      -- complete | processing | pending | canceled | refunded
    channel TEXT NOT NULL,           -- utm source as the shop records it
    total REAL NOT NULL
);
CREATE TABLE oc_order_product (
    order_id INTEGER NOT NULL REFERENCES oc_order(order_id),
    product_id INTEGER NOT NULL REFERENCES oc_product(product_id),
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    cost REAL NOT NULL
);
CREATE TABLE ad_spend (
    day TEXT NOT NULL,
    channel TEXT NOT NULL,
    spend REAL NOT NULL,
    PRIMARY KEY (day, channel)
);
CREATE INDEX ix_order_date ON oc_order(date_added);
CREATE INDEX ix_op_order ON oc_order_product(order_id);
"""

CATEGORIES = {
    "Кухня": ["Сковорода", "Каструля", "Ніж кухарський", "Дошка обробна", "Набір контейнерів",
              "Чайник", "Форма для випікання", "Терка", "Млинниця", "Сито"],
    "Текстиль": ["Плед", "Подушка", "Комплект постілі", "Рушник банний", "Скатертина",
                 "Штора", "Килимок", "Ковдра"],
    "Зберігання": ["Кошик плетений", "Органайзер", "Вакуумний пакет", "Полиця навісна",
                   "Короб з кришкою", "Вішалка"],
    "Сад": ["Шланг", "Секатор", "Лійка", "Рукавиці садові", "Горщик", "Розпилювач"],
    "Декор": ["Свічка ароматична", "Ваза", "Рамка для фото", "Годинник настінний", "Дзеркало"],
}
VARIANTS = ["", " 24 см", " 28 см", " сірий", " бежевий", " 2 шт", " великий", " малий"]

CHANNELS = {  # share of orders on an ordinary day
    "google_ads": 0.30,
    "meta_ads": 0.22,
    "organic": 0.26,
    "email": 0.10,
    "direct": 0.12,
}
WEEKDAY = [1.08, 1.05, 1.02, 1.0, 0.97, 0.86, 0.90]   # Mon..Sun


@dataclass(frozen=True)
class Product:
    product_id: int
    sku: str
    name: str
    category: str
    price: float
    cost: float
    weight: float      # relative popularity


def _products(rng: random.Random) -> list[Product]:
    out: list[Product] = []
    pid = 1000
    for cat, names in CATEGORIES.items():
        for base in names:
            for variant in rng.sample(VARIANTS, k=rng.randint(2, 3)):
                pid += 1
                price = round(rng.uniform(150, 1600) / 10) * 10 - 1
                margin = rng.uniform(0.28, 0.55)
                out.append(Product(pid, "", (base + variant).strip(), cat, float(price),
                                   round(price * (1 - margin), 2), 0.0))
    rng.shuffle(out)
    # Zipf-like popularity: a few best sellers, a long tail. SKUs follow rank so
    # KT-1001 is the best seller, which the stockout incident relies on.
    ranked = []
    for rank, p in enumerate(out, start=1):
        ranked.append(Product(p.product_id, f"KT-{1000 + rank}", p.name, p.category, p.price, p.cost,
                              1.0 / rank ** 0.85))
    return ranked


def _daily_orders(rng: random.Random, day: date, start: date) -> int:
    t = (day - start).days / DAYS
    base = 42 * (1 + 0.18 * t) * WEEKDAY[day.weekday()]
    if day == SITE_DOWN_DAY:
        base *= 0.25
    # Poisson via inversion is slow for ~50; a rounded normal is close enough here.
    return max(0, round(rng.gauss(base, math.sqrt(base))))


def _channel_shares(day: date) -> dict[str, float]:
    shares = dict(CHANNELS)
    if day >= META_FATIGUE_FROM:
        k = (day - META_FATIGUE_FROM).days
        shares["meta_ads"] = max(0.07, 0.22 - 0.0075 * k)
    total = sum(shares.values())
    return {c: s / total for c, s in shares.items()}


def _spend(rng: random.Random, day: date, start: date) -> dict[str, float]:
    t = (day - start).days / DAYS
    google = 3100 * (1 + 0.15 * t) * rng.uniform(0.9, 1.1)
    meta = 2500 * (1 + 0.10 * t) * rng.uniform(0.9, 1.1)
    if day >= META_FATIGUE_FROM:
        meta *= 1 + 0.035 * (day - META_FATIGUE_FROM).days
    return {"google_ads": round(google, 2), "meta_ads": round(meta, 2)}


def build(conn: sqlite3.Connection | None = None, seed: int = 21) -> sqlite3.Connection:
    """Fill a fresh SQLite database with ~180 days of a shop and return it."""
    rng = random.Random(seed)
    conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(SCHEMA)
    products = _products(rng)
    weights = [p.weight for p in products]
    stockout = next(p for p in products if p.sku == STOCKOUT_SKU)
    start = END - timedelta(days=DAYS - 1)

    orders, lines, sold = [], [], {p.product_id: 0 for p in products}
    recent_sold = {p.product_id: 0 for p in products}
    customers = 0
    returning_pool: list[int] = []
    order_id = 5000
    for d in range(DAYS):
        day = start + timedelta(days=d)
        shares = _channel_shares(day)
        chans, cweights = list(shares), list(shares.values())
        for _ in range(_daily_orders(rng, day, start)):
            order_id += 1
            if returning_pool and rng.random() < 0.27:
                cust = rng.choice(returning_pool)
            else:
                customers += 1
                cust = customers
                returning_pool.append(cust)
            ts = datetime(day.year, day.month, day.day, rng.randint(7, 23), rng.randint(0, 59))
            age = (END - day).days
            if age <= 1:
                status = rng.choices(["processing", "pending", "complete"], [0.5, 0.2, 0.3])[0]
            else:
                status = rng.choices(["complete", "canceled", "refunded"], [0.90, 0.07, 0.03])[0]
            total = 0.0
            picked: set[int] = set()
            for _ in range(rng.choices([1, 2, 3, 4], [0.55, 0.28, 0.12, 0.05])[0]):
                p = rng.choices(products, weights)[0]
                if p.product_id in picked or (p is stockout and day >= STOCKOUT_FROM):
                    continue
                picked.add(p.product_id)
                qty = rng.choices([1, 2, 3], [0.8, 0.16, 0.04])[0]
                lines.append((order_id, p.product_id, qty, p.price, p.cost))
                total += qty * p.price
                if status not in ("canceled", "refunded"):
                    sold[p.product_id] += qty
                    if age < 28:
                        recent_sold[p.product_id] += qty
            if not picked:
                order_id -= 1
                continue
            orders.append((order_id, cust, ts.isoformat(sep=" "), status, rng.choices(chans, cweights)[0],
                           round(total, 2)))

    # Current stock: weeks of cover drawn per product, so some sit low on purpose.
    rows = []
    for p in products:
        daily = recent_sold[p.product_id] / 28
        weeks = rng.choice([0.4, 0.7, 1.5, 3, 4, 6, 8, 12])
        qty = 0 if p is stockout else max(2, round(daily * 7 * weeks + rng.randint(0, 3)))
        rows.append((p.product_id, p.sku, p.name, p.category, p.price, p.cost, qty))

    spend_rows = []
    for d in range(DAYS):
        day = start + timedelta(days=d)
        for ch, amount in _spend(rng, day, start).items():
            spend_rows.append((day.isoformat(), ch, amount))

    with conn:
        conn.executemany("INSERT INTO oc_product VALUES (?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO oc_order VALUES (?,?,?,?,?,?)", orders)
        conn.executemany("INSERT INTO oc_order_product VALUES (?,?,?,?,?)", lines)
        conn.executemany("INSERT INTO ad_spend VALUES (?,?,?)", spend_rows)
    return conn
