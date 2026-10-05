"""Сповіщення про проблеми: правила в коді, не LLM.

Правило дає однакову відповідь на однакових даних, його можна перевірити тестом,
і воно не «вигадає» проблему. LLM тут лише пояснює вже знайдене.

Пороги винесені в константи, щоб власник магазину міг їх змінити.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from shoppulse import metrics
from shoppulse.world import END

LOW_STOCK_DAYS = 7          # «закінчиться менш ніж за тиждень»
ORDERS_DROP_RATIO = 0.6     # день, коли замовлень < 60% від медіани того ж дня тижня
MIN_ROAS = 2.0              # реклама, що повертає менше 2 грн на 1 грн витрат
LOOKBACK_DAYS = 30          # за який період шукаємо провали замовлень


def _orders_by_day(conn: sqlite3.Connection, start: date, end: date) -> dict[date, int]:
    return {date.fromisoformat(r["day"]): r["orders"]
            for r in metrics.daily(conn, (end - start).days + 1, end)}


def orders_drops(conn: sqlite3.Connection, end: date = END, lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """Дні, коли замовлень було помітно менше, ніж зазвичай у цей день тижня.

    «Зазвичай» = медіана того самого дня тижня за 4 попередні тижні. Медіана, а не
    середнє: один поганий день не тягне за собою норму на місяць уперед.
    """
    start = end - timedelta(days=lookback - 1)
    series = _orders_by_day(conn, start - timedelta(days=28), end)
    out = []
    d = start
    while d <= end:
        base = [series[d - timedelta(days=7 * k)] for k in range(1, 5) if d - timedelta(days=7 * k) in series]
        if len(base) == 4:
            norm = statistics.median(base)
            if norm and series[d] < ORDERS_DROP_RATIO * norm:
                out.append({"day": d.isoformat(), "orders": series[d], "usual": norm,
                            "ratio": round(series[d] / norm, 2)})
        d += timedelta(days=1)
    return out


def build_alerts(conn: sqlite3.Connection, end: date = END) -> list[dict]:
    """Усі сповіщення на дату `end`, від найсерйознішого."""
    alerts: list[dict] = []

    for item in metrics.stock_cover(conn, max_days=LOW_STOCK_DAYS, limit=50, end=end):
        if item["stock"] == 0:
            alerts.append({
                "severity": "critical", "kind": "out_of_stock",
                "title": f"Закінчився: {item['name']} ({item['sku']})",
                "detail": (f"Залишок 0, останній продаж {item['out_since']}. До того продавався по "
                           f"{item['per_day']} шт/день, втрата ~{item['lost_revenue_per_day']:.0f} грн виручки на день."),
                "value": item["lost_revenue_per_day"],
            })
        else:
            alerts.append({
                "severity": "warning", "kind": "low_stock",
                "title": f"Закінчується: {item['name']} ({item['sku']})",
                "detail": (f"Залишок {item['stock']} шт, продається {item['per_day']} шт/день, "
                           f"вистачить на {item['days_left']} дн."),
                "value": item["days_left"],
            })

    for drop in orders_drops(conn, end):
        alerts.append({
            "severity": "serious", "kind": "orders_drop",
            "title": f"Провал замовлень {drop['day']}",
            "detail": (f"{drop['orders']} замовлень при звичних {drop['usual']:g} для цього дня тижня "
                       f"({drop['ratio'] * 100:.0f}%). Варто перевірити сайт, оплату і рекламу за цей день."),
            "value": drop["ratio"],
        })

    for ch in ("google_ads", "meta_ads"):
        weeks = metrics.channel_weekly(conn, ch, weeks=4, end=end)
        last = weeks[-1]
        if last["roas"] is not None and last["roas"] < MIN_ROAS:
            first = weeks[0]
            alerts.append({
                "severity": "serious", "kind": "low_roas",
                "title": f"{metrics.CHANNEL_NAMES[ch]}: реклама не окупається",
                "detail": (f"Останній тиждень ROAS {last['roas']} (витрати {last['spend']:.0f} грн, "
                           f"виручка {last['revenue']:.0f} грн). Чотири тижні тому було {first['roas']}."),
                "value": last["roas"],
            })

    rank = {"critical": 0, "serious": 1, "warning": 2}
    alerts.sort(key=lambda a: (rank[a["severity"]], a["value"] if a["kind"] != "out_of_stock" else -a["value"]))
    return alerts
