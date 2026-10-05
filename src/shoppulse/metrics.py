"""Показники магазину: чисті SQL-запити над таблицями OpenCart і витратами на рекламу.

Ці ж функції є інструментами ШІ-консультанта, тому кожна повертає прості dict
з округленими числами: що бачить дашборд, те саме бачить модель, і відповідь
можна перевірити по цих числах.

Виручка рахується лише по замовленнях, які не скасовані і не повернуті.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from shoppulse.world import END

PAID = "order_status NOT IN ('canceled', 'refunded')"
CHANNEL_NAMES = {
    "google_ads": "Google Ads",
    "meta_ads": "Meta (Facebook/Instagram)",
    "organic": "Органічний пошук",
    "email": "Email-розсилка",
    "direct": "Прямі заходи",
}


def period(days: int, end: date = END) -> tuple[str, str]:
    """[start, end] включно, як рядки ISO."""
    if not 1 <= days <= 180:
        raise ValueError("days must be within 1..180")
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def _range_args(start: str, end: str) -> tuple[str, str]:
    # date_added зберігається як 'YYYY-MM-DD HH:MM', тож верхня межа включна до кінця дня
    return start, end + " 23:59:59"


def _pct(new: float, old: float) -> float | None:
    return None if not old else round((new - old) / old * 100, 1)


def _kpis_raw(conn: sqlite3.Connection, start: str, end: str) -> dict:
    a, b = _range_args(start, end)
    rev, orders = conn.execute(
        f"SELECT COALESCE(SUM(total),0), COUNT(*) FROM oc_order WHERE {PAID} AND date_added BETWEEN ? AND ?",
        (a, b)).fetchone()
    margin = conn.execute(
        f"""SELECT COALESCE(SUM(op.quantity * (op.price - op.cost)),0)
            FROM oc_order_product op JOIN oc_order o USING(order_id)
            WHERE o.{PAID} AND o.date_added BETWEEN ? AND ?""", (a, b)).fetchone()[0]
    all_orders, canceled = conn.execute(
        """SELECT COUNT(*), SUM(order_status IN ('canceled','refunded'))
           FROM oc_order WHERE date_added BETWEEN ? AND ?""", (a, b)).fetchone()
    repeat = conn.execute(
        f"""SELECT COUNT(*) FROM oc_order o WHERE o.{PAID} AND o.date_added BETWEEN ? AND ?
            AND EXISTS (SELECT 1 FROM oc_order p WHERE p.customer_id = o.customer_id
                        AND p.date_added < o.date_added)""", (a, b)).fetchone()[0]
    spend = conn.execute("SELECT COALESCE(SUM(spend),0) FROM ad_spend WHERE day BETWEEN ? AND ?",
                         (start, end)).fetchone()[0]
    return {
        "revenue": round(rev, 2),
        "orders": orders,
        "aov": round(rev / orders, 2) if orders else 0.0,
        "gross_margin": round(margin, 2),
        "margin_pct": round(margin / rev * 100, 1) if rev else 0.0,
        "cancel_rate_pct": round((canceled or 0) / all_orders * 100, 1) if all_orders else 0.0,
        "repeat_share_pct": round(repeat / orders * 100, 1) if orders else 0.0,
        "ad_spend": round(spend, 2),
    }


def kpis(conn: sqlite3.Connection, days: int = 30, end: date = END) -> dict:
    """Головні показники за останні `days` днів і зміна до попереднього такого ж періоду."""
    start, stop = period(days, end)
    prev_end = date.fromisoformat(start) - timedelta(days=1)
    pstart, pstop = period(days, prev_end)
    cur, prev = _kpis_raw(conn, start, stop), _kpis_raw(conn, pstart, pstop)
    return {
        "period": {"from": start, "to": stop, "days": days},
        "previous_period": {"from": pstart, "to": pstop},
        "current": cur,
        "previous": prev,
        "change_pct": {k: _pct(cur[k], prev[k]) for k in ("revenue", "orders", "aov", "gross_margin", "ad_spend")},
    }


def daily(conn: sqlite3.Connection, days: int = 60, end: date = END) -> list[dict]:
    """Виручка і кількість замовлень по днях, без пропусків для днів без продажів."""
    start, stop = period(days, end)
    a, b = _range_args(start, stop)
    got = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"""SELECT substr(date_added,1,10) d, ROUND(SUM(total),2), COUNT(*) FROM oc_order
            WHERE {PAID} AND date_added BETWEEN ? AND ? GROUP BY d""", (a, b))}
    out, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(stop):
        rev, n = got.get(d.isoformat(), (0.0, 0))
        out.append({"day": d.isoformat(), "revenue": rev, "orders": n})
        d += timedelta(days=1)
    return out


def top_products(conn: sqlite3.Connection, days: int = 30, limit: int = 10, end: date = END) -> list[dict]:
    """Товари з найбільшою виручкою за період."""
    limit = max(1, min(limit, 50))
    start, stop = period(days, end)
    a, b = _range_args(start, stop)
    rows = conn.execute(
        f"""SELECT p.sku, p.name, p.category, SUM(op.quantity) qty,
                   ROUND(SUM(op.quantity*op.price),2) rev,
                   ROUND(SUM(op.quantity*(op.price-op.cost)),2) margin, p.quantity stock
            FROM oc_order_product op JOIN oc_order o USING(order_id) JOIN oc_product p USING(product_id)
            WHERE o.{PAID} AND o.date_added BETWEEN ? AND ?
            GROUP BY p.product_id ORDER BY rev DESC LIMIT ?""", (a, b, limit)).fetchall()
    return [dict(zip(("sku", "name", "category", "units", "revenue", "gross_margin", "stock"), r)) for r in rows]


def stock_cover(conn: sqlite3.Connection, max_days: float = 14, limit: int = 15, end: date = END) -> list[dict]:
    """Товари, яких вистачить менш ніж на `max_days` днів при продажах як за останні 28 днів.

    Товари без продажів за 28 днів сюди не потрапляють: їм «кінчатись» нема від чого.
    Товар із нулем на складі показується з втратою виручки на день.
    """
    start, stop = period(28, end)
    a, b = _range_args(start, stop)
    rows = conn.execute(
        f"""SELECT p.sku, p.name, p.quantity, p.price,
                   COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN op.quantity END),0) sold28,
                   MAX(o.date_added) last_sale
            FROM oc_product p
            LEFT JOIN oc_order_product op ON op.product_id = p.product_id
            LEFT JOIN oc_order o ON o.order_id = op.order_id AND o.{PAID} AND o.date_added BETWEEN ? AND ?
            GROUP BY p.product_id""", (a, b)).fetchall()
    # Для товару, що вже скінчився, продажі за 28 днів занижені днями без залишку,
    # тому темп беремо за 28 днів до останнього продажу.
    out = []
    for sku, name, qty, price, sold28, _ in rows:
        rate = sold28 / 28
        if qty == 0:
            hist = conn.execute(
                f"""SELECT MAX(o.date_added), COALESCE(SUM(op.quantity),0)
                    FROM oc_order_product op JOIN oc_order o USING(order_id)
                    JOIN oc_product p USING(product_id)
                    WHERE p.sku = ? AND o.{PAID}""", (sku,)).fetchone()
            last = hist[0]
            if last:
                lday = date.fromisoformat(last[:10])
                s, e = _range_args((lday - timedelta(days=27)).isoformat(), lday.isoformat())
                rate = conn.execute(
                    f"""SELECT COALESCE(SUM(op.quantity),0) FROM oc_order_product op
                        JOIN oc_order o USING(order_id) JOIN oc_product p USING(product_id)
                        WHERE p.sku = ? AND o.{PAID} AND o.date_added BETWEEN ? AND ?""",
                    (sku, s, e)).fetchone()[0] / 28
            if rate <= 0:
                continue
            out.append({"sku": sku, "name": name, "stock": 0, "per_day": round(rate, 2), "days_left": 0.0,
                        "out_since": (last or "")[:10], "lost_revenue_per_day": round(rate * price, 2)})
            continue
        if rate <= 0:
            continue
        left = qty / rate
        if left < max_days:
            out.append({"sku": sku, "name": name, "stock": qty, "per_day": round(rate, 2),
                        "days_left": round(left, 1), "out_since": None, "lost_revenue_per_day": 0.0})
    out.sort(key=lambda r: (r["days_left"], -r["lost_revenue_per_day"]))
    return out[:limit]


def channels(conn: sqlite3.Connection, days: int = 30, end: date = END) -> list[dict]:
    """Замовлення, виручка, витрати і ROAS (виручка / витрати) по каналах."""
    start, stop = period(days, end)
    a, b = _range_args(start, stop)
    spend = dict(conn.execute("SELECT channel, SUM(spend) FROM ad_spend WHERE day BETWEEN ? AND ? GROUP BY channel",
                              (start, stop)).fetchall())
    rows = conn.execute(
        f"""SELECT channel, COUNT(*), ROUND(SUM(total),2) FROM oc_order
            WHERE {PAID} AND date_added BETWEEN ? AND ? GROUP BY channel ORDER BY 3 DESC""", (a, b)).fetchall()
    out = []
    for ch, n, rev in rows:
        s = round(spend.get(ch, 0.0), 2)
        out.append({"channel": ch, "name": CHANNEL_NAMES.get(ch, ch), "orders": n, "revenue": rev,
                    "spend": s, "roas": round(rev / s, 2) if s else None,
                    "cost_per_order": round(s / n, 2) if s and n else None})
    return out


def channel_weekly(conn: sqlite3.Connection, channel: str, weeks: int = 8, end: date = END) -> list[dict]:
    """ROAS одного каналу по тижнях (7-денні вікна, що закінчуються в `end`)."""
    if channel not in CHANNEL_NAMES:
        raise ValueError(f"unknown channel {channel!r}")
    weeks = max(1, min(weeks, 25))
    out = []
    for w in range(weeks - 1, -1, -1):
        stop = end - timedelta(days=7 * w)
        start = stop - timedelta(days=6)
        a, b = _range_args(start.isoformat(), stop.isoformat())
        n, rev = conn.execute(
            f"""SELECT COUNT(*), COALESCE(ROUND(SUM(total),2),0) FROM oc_order
                WHERE {PAID} AND channel = ? AND date_added BETWEEN ? AND ?""", (channel, a, b)).fetchone()
        s = conn.execute("SELECT COALESCE(SUM(spend),0) FROM ad_spend WHERE channel = ? AND day BETWEEN ? AND ?",
                         (channel, start.isoformat(), stop.isoformat())).fetchone()[0]
        out.append({"week_from": start.isoformat(), "week_to": stop.isoformat(), "orders": n, "revenue": rev,
                    "spend": round(s, 2), "roas": round(rev / s, 2) if s else None})
    return out
