"""Воронка откликов по данным HH: python stats.py [YYYY-MM-DD начала автопилота]

Считает все отклики аккаунта, статусы и долю приглашений до и после даты запуска.
"""

import collections
import json
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).parent / ".venv/bin/hh-applicant-tool"
POSITIVE = {"invitation", "interview", "offer", "hired"}


def negotiations() -> list[dict]:
    items, page = [], 0
    while True:
        out = subprocess.run(
            [TOOL, "call-api", "/negotiations", f"page={page}", "per_page=100"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        data = json.loads(out)
        items += data["items"]
        page += 1
        if page >= data["pages"]:
            return items


def report(title: str, items: list[dict]) -> None:
    states = collections.Counter(n["state"]["id"] for n in items)
    positive = sum(states[s] for s in POSITIVE)
    answered = positive + states["discard"]
    days = len({n["created_at"][:10] for n in items}) or 1
    print(f"{title}: откликов {len(items)} (≈{len(items) // days} в активный день)")
    print(f"  приглашений {positive} — {100 * positive / max(len(items), 1):.1f}% от откликов, "
          f"{100 * positive / max(answered, 1):.0f}% от ответивших")
    print(f"  отказов {states['discard']}, без ответа {states['response']}")


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-09-29"
    items = negotiations()
    report(f"До {start}", [n for n in items if n["created_at"][:10] < start])
    report(f"С {start}", [n for n in items if n["created_at"][:10] >= start])
