"""Тести без мережі: дані, показники, сповіщення, консультант з фейковою моделлю, HTTP."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from shoppulse import alerts, assistant, metrics, web, world


@pytest.fixture(scope="module")
def conn():
    return world.build()


# --- дані -----------------------------------------------------------------------

def test_world_is_deterministic():
    a, b = world.build(), world.build()
    q = "SELECT COUNT(*), ROUND(SUM(total),2) FROM oc_order"
    assert a.execute(q).fetchone() == b.execute(q).fetchone()


def test_world_size_is_realistic(conn):
    orders, products = conn.execute("SELECT (SELECT COUNT(*) FROM oc_order), (SELECT COUNT(*) FROM oc_product)").fetchone()
    assert 6000 < orders < 11000
    assert 60 < products < 120


def test_every_order_has_lines_and_total_matches(conn):
    bad = conn.execute("""SELECT COUNT(*) FROM oc_order o WHERE ABS(o.total -
        (SELECT SUM(quantity*price) FROM oc_order_product op WHERE op.order_id = o.order_id)) > 0.01""").fetchone()[0]
    assert bad == 0


def test_stockout_product_stops_selling(conn):
    n = conn.execute("""SELECT COUNT(*) FROM oc_order_product op JOIN oc_order o USING(order_id)
        JOIN oc_product p USING(product_id) WHERE p.sku = ? AND o.date_added >= ?""",
                     (world.STOCKOUT_SKU, world.STOCKOUT_FROM.isoformat())).fetchone()[0]
    assert n == 0


# --- показники -------------------------------------------------------------------

def test_kpis_compare_equal_periods(conn):
    k = metrics.kpis(conn, 30)
    assert k["period"] == {"from": "2026-09-05", "to": "2026-10-04", "days": 30}
    assert k["previous_period"] == {"from": "2026-08-06", "to": "2026-09-04"}
    c = k["current"]
    assert c["aov"] == pytest.approx(c["revenue"] / c["orders"], abs=0.01)


def test_revenue_excludes_canceled_and_refunded(conn):
    k = metrics.kpis(conn, 180)["current"]
    paid = conn.execute("SELECT ROUND(SUM(total),2) FROM oc_order WHERE order_status NOT IN ('canceled','refunded')").fetchone()[0]
    everything = conn.execute("SELECT ROUND(SUM(total),2) FROM oc_order").fetchone()[0]
    assert k["revenue"] == pytest.approx(paid)
    assert k["revenue"] < everything


def test_daily_has_no_gaps(conn):
    rows = metrics.daily(conn, 60)
    assert len(rows) == 60
    assert rows[0]["day"] == "2026-08-06" and rows[-1]["day"] == "2026-10-04"


def test_daily_sum_equals_kpi_revenue(conn):
    assert sum(r["revenue"] for r in metrics.daily(conn, 30)) == pytest.approx(metrics.kpis(conn, 30)["current"]["revenue"])


def test_period_rejects_out_of_range():
    with pytest.raises(ValueError):
        metrics.period(0)
    with pytest.raises(ValueError):
        metrics.period(181)


def test_stock_cover_reports_stockout_with_lost_revenue(conn):
    rows = metrics.stock_cover(conn, max_days=7)
    first = rows[0]
    assert first["sku"] == world.STOCKOUT_SKU and first["stock"] == 0
    assert first["lost_revenue_per_day"] > 0
    assert all(r["days_left"] < 7 for r in rows)


def test_channels_roas_is_revenue_over_spend(conn):
    for ch in metrics.channels(conn, 30):
        if ch["spend"]:
            assert ch["roas"] == pytest.approx(ch["revenue"] / ch["spend"], abs=0.01)
        else:
            assert ch["roas"] is None


# --- сповіщення: три закладені проблеми знаходяться, зайвих серйозних немає ------

def test_planted_incidents_are_found(conn):
    found = alerts.build_alerts(conn)
    kinds = {(a["kind"], a["title"]) for a in found}
    assert any(k == "out_of_stock" and world.STOCKOUT_SKU in t for k, t in kinds)
    assert any(k == "orders_drop" and world.SITE_DOWN_DAY.isoformat() in t for k, t in kinds)
    assert any(k == "low_roas" and "Meta" in t for k, t in kinds)


def test_no_false_serious_alerts(conn):
    found = alerts.build_alerts(conn)
    serious = [a for a in found if a["severity"] in ("critical", "serious")]
    assert len(serious) == 3, [a["title"] for a in serious]
    assert found[0]["severity"] == "critical"


def test_orders_drop_uses_same_weekday_median(conn):
    drops = alerts.orders_drops(conn, end=date(2026, 9, 20), lookback=14)
    assert [d["day"] for d in drops] == ["2026-09-12"]
    assert drops[0]["ratio"] < 0.4


# --- консультант ------------------------------------------------------------------

class FakeChat:
    """Сценарій: список відповідей моделі по черзі. Записує, що їй надіслали."""

    def __init__(self, script):
        self.script, self.seen = list(script), []

    def chat(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.script.pop(0)


def call(name, args, cid="c1"):
    return {"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
        "usage": {"prompt_tokens": 500, "completion_tokens": 40}}


def final(text):
    return {"message": {"role": "assistant", "content": text}, "usage": {"prompt_tokens": 900, "completion_tokens": 80}}


def test_ask_answer_with_numbers_from_tools_is_verified(conn):
    k = metrics.kpis(conn, 30)["current"]
    text = f"За 30 днів виручка {k['revenue']:,.0f} грн, замовлень {k['orders']}.".replace(",", " ")
    ans = assistant.ask(conn, "Яка виручка за місяць?", FakeChat([call("get_kpis", {"days": 30}), final(text)]))
    assert ans.status == "ok"
    assert ans.unverified == [] and ans.checked_numbers == 3   # 30 днів, виручка, замовлення
    assert ans.steps[0]["tool"] == "get_kpis"
    assert ans.prompt_tokens == 1400 and ans.cost_usd > 0


def test_ask_flags_invented_number(conn):
    ans = assistant.ask(conn, "Яка виручка?", FakeChat([call("get_kpis", {"days": 30}),
                                                        final("Виручка 1 234 567 грн, зростання 42.5%.")]))
    assert "1 234 567" in ans.unverified and "42.5" in ans.unverified


def test_ask_flags_invented_date(conn):
    ans = assistant.ask(conn, "Коли був провал?", FakeChat([call("get_alerts", {}),
                                                            final("Провал був 2026-09-12 і 2026-08-01.")]))
    assert ans.unverified == ["2026-08-01"]


def test_ask_bad_tool_arguments_go_back_to_model(conn):
    chat = FakeChat([call("get_kpis", {"days": 999}), call("get_kpis", {"days": 30}, "c2"), final("Готово.")])
    ans = assistant.ask(conn, "Виручка?", chat)
    assert ans.steps[0]["error"].startswith("invalid arguments")
    tool_msg = chat.seen[1][-1]
    assert tool_msg["role"] == "tool" and "invalid arguments" in tool_msg["content"]
    assert ans.status == "ok"


def test_ask_unknown_tool_is_reported_not_executed(conn):
    ans = assistant.ask(conn, "?", FakeChat([call("drop_table", {}), final("Не можу.")]))
    assert ans.steps[0]["error"] == "unknown tool 'drop_table'"


def test_ask_stops_at_step_limit(conn):
    chat = FakeChat([call("get_alerts", {}, f"c{i}") for i in range(10)])
    ans = assistant.ask(conn, "?", chat, max_steps=3)
    assert ans.status == "step_limit" and len(chat.seen) == 3


def test_ask_more_than_four_calls_are_trimmed_consistently(conn):
    many = {"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": "get_alerts", "arguments": "{}"}} for i in range(6)]},
        "usage": {}}
    chat = FakeChat([many, final("Ок.")])
    assistant.ask(conn, "?", chat)
    sent = chat.seen[1]
    asked = [c["id"] for c in sent[2]["tool_calls"]]
    answered = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
    assert asked == answered == ["c0", "c1", "c2", "c3"]


def test_ask_model_down_returns_error_status(conn):
    class Down:
        def chat(self, messages, tools):
            raise assistant.TransientError("HTTP 503")
    ans = assistant.ask(conn, "?", Down())
    assert ans.status == "error" and "недоступна" in ans.text


def test_groq_retries_are_bounded():
    import httpx

    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={})
    client = assistant.GroqChat("k", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    with pytest.raises(assistant.TransientError):
        client.chat([], [])
    assert len(calls) == assistant.MAX_RETRIES


def test_tool_specs_have_no_titles_and_match_registry():
    specs = assistant.tool_specs()
    assert {s["function"]["name"] for s in specs} == set(assistant.TOOLS)
    assert "title" not in json.dumps(specs)


def test_numbers_checker_ignores_small_ints_and_question_numbers():
    res = assistant.check_numbers("Топ 3 товари за 45 днів: 100 грн", [{"x": 100}], question="за 45 днів")
    assert res["unverified"] == [] and res["checked"] == 1


# --- HTTP -------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "_hits", {})
    monkeypatch.setattr(web, "_day_count", {"day": "", "n": 0})
    return TestClient(web.app)


def test_dashboard_endpoint(client):
    r = client.get("/api/dashboard?days=30")
    assert r.status_code == 200
    d = r.json()
    assert d["as_of"] == "2026-10-04" and len(d["daily"]) == 30 and d["alerts"]


def test_dashboard_rejects_odd_period(client):
    assert client.get("/api/dashboard?days=13").status_code == 400


def test_index_and_health(client):
    assert client.get("/health").json() == {"ok": True}
    assert "Shop Pulse" in client.get("/").text


def test_ask_without_key_is_503(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert client.post("/api/ask", json={"question": "Виручка?"}).status_code == 503


def test_ask_rate_limit_per_ip(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setattr(web.assistant, "GroqChat", lambda key: FakeChat([final("Ок.")] * 20))
    codes = [client.post("/api/ask", json={"question": "Виручка?"}).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_global_daily_cap():
    web._day_count.update(day="", n=0)
    web._hits.clear()
    now = 1_790_000_000.0
    results = [web._allow(f"ip{i}", now) for i in range(web.GLOBAL_PER_DAY + 1)]
    assert results[:-1] == [None] * web.GLOBAL_PER_DAY
    assert "Денний ліміт" in results[-1]


def test_normalized_typography_is_checked(conn):
    k = metrics.kpis(conn, 30)["current"]
    rev = f"{k['revenue']:,.0f}".replace(",", " ")
    ans = assistant.ask(conn, "?", FakeChat([call("get_alerts", {}), call("get_kpis", {"days": 30}, "c2"),
                                             final(f"Виручка {rev} грн, провал 2026‑09‑12.")]))
    assert ans.unverified == []
    assert "2026-09-12" in ans.text


@pytest.mark.parametrize("written", ["12.09.2026", "12.09", "12 09 2026", "12 09", "12 вересня", "12 вересня 2026 року"])
def test_human_dates_are_checked_against_iso(written):
    data = [{"day": "2026-09-12", "orders": 8}]
    assert assistant.check_numbers(f"Провал {written}: 8 замовлень.", data)["unverified"] == []


def test_wrong_human_date_is_flagged():
    data = [{"day": "2026-09-12"}]
    assert assistant.check_numbers("Провал 13 вересня.", data)["unverified"] == ["2026-09-13"]


def test_decimals_are_not_mistaken_for_dates():
    data = [{"days_left": 4.4, "roas": 1.32}]
    assert assistant.check_numbers("Вистачить на 4.4 дн, ROAS 1.32.", data)["unverified"] == []
    assert assistant.check_numbers("Вистачить на 4.7 дн.", data)["unverified"] == ["4.7"]


def test_customer_index_keeps_kpis_fast(conn):
    import time
    t = time.perf_counter()
    metrics.kpis(conn, 90)
    assert time.perf_counter() - t < 0.5
