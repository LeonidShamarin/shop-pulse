"""Точка входу Vercel: ASGI-застосунок живе в src/shoppulse/web.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from shoppulse.web import app  # noqa: E402,F401
