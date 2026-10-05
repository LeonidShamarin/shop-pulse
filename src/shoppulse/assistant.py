"""ШІ-консультант: відповідає на питання про магазин лише через інструменти.

Модель не пише SQL і не бачить базу. Вона може викликати ті самі функції, що
малюють дашборд (`metrics`, `alerts`), з аргументами, які перевіряє Pydantic.
Після відповіді код шукає в тексті кожне число й дату і звіряє з тим, що
повернули інструменти. Числа, яких там немає, показуються користувачу як
неперевірені: так видно, де модель порахувала сама або вигадала.

Обмеження з першого дня: не більше MAX_STEPS викликів моделі на питання,
таймаут на запит, retry лише на 429/5xx/мережу і лише MAX_RETRIES разів.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from shoppulse import alerts as alerts_mod
from shoppulse import metrics

MAX_STEPS = 4
MAX_RETRIES = 3
MAX_QUESTION_CHARS = 400
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
# USD за 1M токенів (вхід, вихід), console.groq.com/docs/models, перевірено 2026-09-23
PRICE_PER_M = (0.15, 0.60)

SYSTEM = """Ти аналітик інтернет-магазину. Відповідаєш українською, коротко (до 120 слів).
Дані бери ТІЛЬКИ з інструментів. Не вигадуй чисел: кожне число у відповіді має бути
в результатах інструментів. Не перераховуй і не округлюй по-своєму, пиши числа як є.
Не рахуй сам похідних чисел (частки, середні, різниці): бери готові поля
(aov, change_pct, ratio, roas). Якщо інструменти не дають відповіді, так і скажи.
Гроші в гривнях, пиши "грн". Простий текст без markdown, без зірочок і заголовків.
На питання "що сталося", "які проблеми", "що не так" спершу виклич get_alerts.
Про залишки і що дозамовити виклич get_stock_cover.
Про окупність реклами дивись і get_channel_weekly: середнє за місяць ховає свіжий спад.
Остання дата даних 2026-10-04. "Цей місяць" = останні 30 днів, "тиждень" = 7 днів.
Наприкінці можеш дати одну практичну пораду, що перевірити."""


# --- аргументи інструментів --------------------------------------------------

class DaysArgs(BaseModel):
    days: int = Field(30, ge=1, le=180, description="Скільки останніх днів брати")


class TopArgs(DaysArgs):
    limit: int = Field(10, ge=1, le=20)


class StockArgs(BaseModel):
    max_days: float = Field(14, gt=0, le=60, description="Показати товари, яких вистачить менше ніж на стільки днів")


class ChannelWeeklyArgs(BaseModel):
    channel: Literal["google_ads", "meta_ads", "organic", "email", "direct"]
    weeks: int = Field(8, ge=1, le=25)


class NoArgs(BaseModel):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[sqlite3.Connection, Any], Any]


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_kpis", "Виручка, замовлення, середній чек, маржа, скасування, повторні покупці, витрати "
                     "на рекламу за період і зміна до попереднього такого ж періоду.",
         DaysArgs, lambda c, a: metrics.kpis(c, a.days)),
    Tool("get_daily", "Виручка і кількість замовлень по днях.", DaysArgs,
         lambda c, a: metrics.daily(c, a.days)),
    Tool("get_top_products", "Товари з найбільшою виручкою за період, з маржею і залишком.", TopArgs,
         lambda c, a: metrics.top_products(c, a.days, a.limit)),
    Tool("get_stock_cover", "Товари, що закінчились або скоро закінчаться, з темпом продажів.", StockArgs,
         lambda c, a: metrics.stock_cover(c, a.max_days)),
    Tool("get_channels", "Замовлення, виручка, витрати, ROAS і вартість замовлення по рекламних каналах.",
         DaysArgs, lambda c, a: metrics.channels(c, a.days)),
    Tool("get_channel_weekly", "ROAS і витрати одного каналу по тижнях.", ChannelWeeklyArgs,
         lambda c, a: metrics.channel_weekly(c, a.channel, a.weeks)),
    Tool("get_alerts", "Поточні сповіщення: закінчився товар, провал замовлень, реклама не окупається.",
         NoArgs, lambda c, a: alerts_mod.build_alerts(c)),
]}


def tool_specs() -> list[dict]:
    specs = []
    for t in TOOLS.values():
        schema = t.args.model_json_schema()
        schema.pop("title", None)
        for p in schema.get("properties", {}).values():
            p.pop("title", None)
        specs.append({"type": "function", "function": {"name": t.name, "description": t.description,
                                                       "parameters": schema}})
    return specs


def run_tool(conn: sqlite3.Connection, name: str, raw_args: str) -> tuple[Any, str | None]:
    """Виконати інструмент. Помилка повертається моделі текстом, а не валить відповідь."""
    tool = TOOLS.get(name)
    if tool is None:
        return None, f"unknown tool {name!r}"
    try:
        args = tool.args.model_validate(json.loads(raw_args or "{}"))
    except (json.JSONDecodeError, ValidationError) as e:
        return None, f"invalid arguments: {str(e)[:300]}"
    return tool.run(conn, args), None


# --- перевірка чисел ----------------------------------------------------------

_DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
_MONTHS = {"січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6, "липня": 7,
           "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12}
# 12.09.2026, 12.09, 12 09 2026, 12 09, 12 вересня (2026 року): моделі переписують ISO-дати по-людськи
_HUMAN_DATE = re.compile(r"\b(\d{1,2})(?:\.(\d{1,2})|\s(\d{2})\b|\s(" + "|".join(_MONTHS) + r"))"
                         r"(?:[.\s](20\d\d))?(?:\s?(?:року|р\.))?")


def _iso_dates(text: str, blob: str, year: int = 2026) -> tuple[str, list[str]]:
    """Знайти людські дати, повернути текст без них і список ISO-дат.

    «4.4» чи «14.10» можуть бути і числом, і датою. Цифрова форма вважається датою
    лише тоді, коли така дата є в даних; інакше лишається в тексті і перевіряється
    як число. Форма з назвою місяця («12 вересня») завжди дата.
    """
    found: list[str] = []

    def sub(m: re.Match) -> str:
        day = int(m.group(1))
        month = int(m.group(2) or m.group(3) or 0) or _MONTHS[m.group(4)]
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return m.group(0)
        iso = f"{int(m.group(5) or year):04d}-{month:02d}-{day:02d}"
        if not m.group(4) and iso not in blob:
            return m.group(0)
        found.append(iso)
        return " "
    return _HUMAN_DATE.sub(sub, text), found
_NUM = re.compile(r"(?<![\w.])-?\d[\d\s  ]*(?:[.,]\d+)?")


def _numbers_in(obj: Any, out: list[float], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out, depth + 1)
    elif isinstance(obj, str):
        for m in _NUM.finditer(_DATE.sub(" ", obj)):
            v = _parse(m.group())
            if v is not None:
                out.append(v)


def _parse(token: str) -> float | None:
    t = re.sub(r"[\s  ]", "", token).replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def check_numbers(answer: str, tool_results: list[Any], question: str = "",
                  tool_args: list[dict] | None = None) -> dict:
    """Які числа й дати з відповіді є в результатах інструментів.

    Число вважається підтвердженим, якщо збігається з числом із даних з точністю до
    округлення (до цілого, або 0.5% для великих сум). Не перевіряються: маленькі цілі
    до 10 (нумерація, «3 товари»), роки 2020-2030, числа з питання і з аргументів
    інструментів («за 30 днів»).
    """
    known: list[float] = []
    _numbers_in(tool_results, known)
    _numbers_in(tool_args or [], known)
    blob = json.dumps(tool_results, ensure_ascii=False)
    q_nums = {_parse(m.group()) for m in _NUM.finditer(question)}

    dates = _DATE.findall(answer)
    text, human = _iso_dates(_DATE.sub(" ", answer), blob)
    dates += human
    bad_dates = [d for d in dates if d not in blob]
    checked, unverified = 0, []
    for m in _NUM.finditer(text):
        v = _parse(m.group().strip())
        if v is None or (v.is_integer() and (0 <= v <= 10 or 2020 <= v <= 2030)) or v in q_nums:
            continue
        checked += 1
        # Допуск = половина останнього написаного розряду: «4.4» приймає 4.36..4.45,
        # «14367» приймає 14366.79. Для великих сум ще 0.5% («~14 тис» не пишемо, але
        # «2 295 000» замість 2 294 900 це округлення, не вигадка).
        frac = re.search(r"[.,](\d+)", m.group())
        tol = 0.5 * 10 ** -len(frac.group(1)) if frac else 0.5
        ok = any(abs(v - k) <= max(tol + 1e-9, abs(k) * 0.005 if abs(k) >= 1000 else 0) for k in known)
        if not ok:
            unverified.append(m.group().strip())
    return {"checked": checked + len(dates), "unverified": unverified + bad_dates}


_TYPO = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-",
                       " ": " ", " ": " ", " ": " "})


def normalize(text: str) -> str:
    """Моделі пишуть нерозривні пробіли й дефіси (2 294 900, 2026‑09‑26).
    Для читача це те саме, а регулярні вирази без нормалізації їх не бачать."""
    return text.translate(_TYPO)


# --- клієнт моделі -------------------------------------------------------------

class ChatClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """Повертає {'message': {...}, 'usage': {...}} у форматі OpenAI."""


class TransientError(Exception):
    pass


class GroqChat:
    def __init__(self, api_key: str, *, model: str = MODEL, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key, self.model = api_key, model
        self.http = http or httpx.Client(timeout=30.0)
        self.sleep = sleep

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto",
                "temperature": 0.0, "max_completion_tokens": 1200, "reasoning_effort": "low"}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.http.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = TransientError(f"HTTP {r.status_code}")
                else:
                    r.raise_for_status()
                    data = r.json()
                    return {"message": data["choices"][0]["message"], "usage": data.get("usage", {})}
            self.sleep(min(2 ** attempt, 8))
        raise TransientError(str(last))


# --- цикл ----------------------------------------------------------------------

@dataclass
class Answer:
    text: str
    steps: list[dict] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    checked_numbers: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    status: str = "ok"          # ok | step_limit | error

    @property
    def cost_usd(self) -> float:
        return round((self.prompt_tokens * PRICE_PER_M[0] + self.completion_tokens * PRICE_PER_M[1]) / 1e6, 6)

    def as_dict(self) -> dict:
        return {"answer": self.text, "steps": self.steps, "unverified": self.unverified,
                "checked_numbers": self.checked_numbers, "status": self.status,
                "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
                "cost_usd": self.cost_usd}


def ask(conn: sqlite3.Connection, question: str, client: ChatClient, max_steps: int = MAX_STEPS) -> Answer:
    question = question.strip()[:MAX_QUESTION_CHARS]
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    specs = tool_specs()
    ans = Answer(text="")
    results: list[Any] = []
    used_args: list[dict] = []
    for _ in range(max_steps):
        try:
            out = client.chat(messages, specs)
        except (TransientError, httpx.HTTPError) as e:
            ans.text, ans.status = "Модель зараз недоступна, спробуйте за хвилину.", "error"
            ans.steps.append({"error": str(e)[:200]})
            return ans
        usage = out.get("usage") or {}
        ans.prompt_tokens += int(usage.get("prompt_tokens", 0))
        ans.completion_tokens += int(usage.get("completion_tokens", 0))
        msg = out["message"]
        # Не більше 4 викликів за крок; у повідомленні асистента лишаються лише ті,
        # на які є відповідь, інакше API відхилить наступний запит.
        calls = (msg.get("tool_calls") or [])[:4]
        if not calls:
            ans.text = normalize(msg.get("content") or "").strip()
            check = check_numbers(ans.text, results, question, used_args)
            ans.unverified, ans.checked_numbers = check["unverified"], check["checked"]
            return ans
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            result, err = run_tool(conn, fn.get("name", ""), fn.get("arguments", "{}"))
            if err is None:
                results.append(result)
                used_args.append(json.loads(fn.get("arguments") or "{}"))
            ans.steps.append({"tool": fn.get("name"), "args": fn.get("arguments"), "error": err})
            payload = {"error": err} if err else result
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": json.dumps(payload, ensure_ascii=False)[:6000]})
    ans.text, ans.status = "Не вдалося відповісти за відведену кількість кроків.", "step_limit"
    return ans
