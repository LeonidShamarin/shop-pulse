"""Оцінка консультанта на справжній моделі: 15 питань з відомою відповіддю.

Що міряється по кожному питанню:
* tool_ok  – чи викликано інструмент, без якого відповісти не можна;
* fact_ok  – чи є у відповіді факт із закладених у дані (назва товару, дата, канал);
* clean    – чи всі числа відповіді знайшлися в результатах інструментів;
* latency, токени, вартість.

Запуск (ключ лише з оточення):  PYTHONPATH=src python eval/run_eval.py
Результат: eval/results.json і рядок підсумку.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

from shoppulse import assistant, world

STOCKOUT_NAME = None  # заповнюється з бази


def cases(stockout_name: str) -> list[dict]:
    return [
        {"q": "Яка виручка і скільки замовлень за останні 30 днів?", "tool": "get_kpis", "facts": ["2 294 900|2294900"]},
        {"q": "Як змінилась виручка порівняно з попереднім місяцем?", "tool": "get_kpis", "facts": ["%"]},
        {"q": "Який середній чек за тиждень?", "tool": "get_kpis", "facts": ["грн"]},
        {"q": "Що сталося 12 вересня?", "tool": "get_alerts|get_daily", "facts": ["12"]},
        {"q": "Які зараз є проблеми в магазині?", "tool": "get_alerts", "facts": [stockout_name, "Meta"]},
        {"q": "Які товари треба дозамовити першими?", "tool": "get_stock_cover|get_alerts", "facts": [stockout_name]},
        {"q": "Скільки виручки ми втрачаємо через товари, яких немає?", "tool": "get_stock_cover|get_alerts", "facts": [stockout_name]},
        {"q": "Чи окупається реклама в Meta?", "tool": "get_channel_weekly|get_alerts", "facts": ["1.32"]},
        {"q": "Який канал приносить найбільше виручки?", "tool": "get_channels", "facts": ["Google"]},
        {"q": "Скільки ми витратили на рекламу за 30 днів?", "tool": "get_kpis|get_channels", "facts": ["грн"]},
        {"q": "Назви три найприбутковіші товари за місяць", "tool": "get_top_products", "facts": ["KT-|грн"]},
        {"q": "Яка частка повторних покупців?", "tool": "get_kpis", "facts": ["%"]},
        {"q": "Яка маржа магазину за 90 днів?", "tool": "get_kpis", "facts": ["%|грн"]},
        {"q": "Скільки скасованих замовлень?", "tool": "get_kpis", "facts": ["%"]},
        {"q": "Яка погода буде завтра в Києві?", "tool": "", "facts": ["немає|не можу|не маю|недоступн|не стосу"]},
    ]


def main() -> int:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        print("GROQ_API_KEY is not set")
        return 2
    conn = world.build()
    name = conn.execute("SELECT name FROM oc_product WHERE sku = ?", (world.STOCKOUT_SKU,)).fetchone()[0]
    client = assistant.GroqChat(key)
    rows = []
    for c in cases(name):
        t0 = time.perf_counter()
        ans = assistant.ask(conn, c["q"], client)
        dt = time.perf_counter() - t0
        tools = [s.get("tool") for s in ans.steps if s.get("tool")]
        tool_ok = (not tools) if not c["tool"] else any(t in c["tool"].split("|") for t in tools)
        low = assistant.normalize(ans.text).lower()
        fact_ok = all(any(alt.lower() in low for alt in f.split("|")) for f in c["facts"])
        rows.append({"question": c["q"], "answer": ans.text, "tools": tools, "status": ans.status,
                     "tool_ok": tool_ok, "fact_ok": fact_ok, "unverified": ans.unverified,
                     "checked_numbers": ans.checked_numbers, "latency_s": round(dt, 2),
                     "cost_usd": ans.cost_usd})
        time.sleep(2)  # безкоштовний ліміт Groq на хвилину
    n = len(rows)
    lat = sorted(r["latency_s"] for r in rows)
    summary = {
        "model": assistant.MODEL, "questions": n,
        "tool_ok": sum(r["tool_ok"] for r in rows), "fact_ok": sum(r["fact_ok"] for r in rows),
        "all_numbers_verified": sum(not r["unverified"] for r in rows),
        "numbers_checked": sum(r["checked_numbers"] for r in rows),
        "numbers_unverified": sum(len(r["unverified"]) for r in rows),
        "latency_median_s": statistics.median(lat), "latency_p95_s": lat[min(n - 1, int(n * 0.95))],
        "cost_per_1000_usd": round(sum(r["cost_usd"] for r in rows) / n * 1000, 3),
    }
    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
