"""Воронка откликов по данным HH.

python stats.py [YYYY-MM-DD]  — до/после даты запуска автопилота (по умолчанию 2026-09-29)
python stats.py --week         — срезы за последние 7 и 28 дней в Telegram (таймер hh-stats)

Срезы: резюме, удалёнка/офис, уровень вакансии, направление (QA/AI).
Срез «резюме × удалёнка» — это A/B-тест: удалёнка по нечётным дням года идёт с
резюме RESUME_OFFICE, по чётным — с RESUME_MAIN (см. hh-run.sh).
"""

import collections
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR = Path(__file__).parent
TOOL = DIR / ".venv/bin/hh-applicant-tool"
POSITIVE = {"invitation", "interview", "offer", "hired"}
AI_TITLE = re.compile(r"\bAI\b|ИИ|LLM|промпт|prompt", re.I)


def api(endpoint: str, *params: str) -> dict:
    out = subprocess.run(
        [TOOL, "call-api", endpoint, *params],
        capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def negotiations() -> list[dict]:
    items, page = [], 0
    while True:
        data = api("/negotiations", f"page={page}", "per_page=100")
        items += data["items"]
        page += 1
        if page >= data["pages"]:
            return items


def level(name: str) -> str:
    if re.search(r"middle", name, re.I):
        return "middle"
    if re.search(r"senior|\bsr\b|ведущ|старш|lead|\bлид|руковод|head", name, re.I):
        return "senior/lead"
    return "без уровня"


def is_remote(vacancy_id: str, cache: dict) -> bool | None:
    if vacancy_id not in cache:
        try:
            v = api(f"/vacancies/{vacancy_id}")
            formats = {f["id"] for f in v.get("work_format") or []}
            cache[vacancy_id] = "REMOTE" in formats or (v.get("schedule") or {}).get("id") == "remote"
        except Exception:
            cache[vacancy_id] = None
    return cache[vacancy_id]


def line(items: list[dict]) -> str:
    states = collections.Counter(n["state"]["id"] for n in items)
    positive = sum(states[s] for s in POSITIVE)
    n = len(items)
    return f"{n} откл → {positive} пригл ({100 * positive / max(n, 1):.1f}%), отказов {states['discard']}"


def segments(items: list[dict], resumes: dict[str, str], remote_cache: dict) -> list[str]:
    out = []
    groups = {
        "Резюме": lambda n: resumes.get((n.get("resume") or {}).get("id"), "другое"),
        "Формат": lambda n: {True: "удалёнка", False: "офис/гибрид", None: "закрыта"}[
            is_remote(n["vacancy"]["id"], remote_cache)],
        "A/B удалёнки": lambda n: (
            resumes.get((n.get("resume") or {}).get("id"), "другое")
            if is_remote(n["vacancy"]["id"], remote_cache) else None),
        "Уровень": lambda n: level(n["vacancy"]["name"]),
        "Направление": lambda n: "AI" if AI_TITLE.search(n["vacancy"]["name"]) else "QA",
    }
    for title, key in groups.items():
        buckets = collections.defaultdict(list)
        for n in items:
            k = key(n)
            if k is not None:
                buckets[k].append(n)
        out.append(f"<b>{title}</b>")
        for k, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            out.append(f"  {k}: {line(group)}")
    return out


def week_report() -> str:
    items = negotiations()
    resumes = {r["id"]: r["title"][:30] for r in api("/resumes/mine")["items"]}
    now = datetime.now(timezone.utc)
    cache: dict = {}
    parts = ["📊 <b>Воронка откликов за неделю</b>"]
    for days in (7, 28):
        since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        window = [n for n in items if n["created_at"][:10] >= since]
        parts.append(f"\n<b>— {days} дней:</b> {line(window)}")
        parts += segments(window, resumes, cache)
    parts.append("\nСвежие отклики ещё ждут ответа — проценты за 7 дней занижены.")
    return "\n".join(parts)


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
    if "--week" in sys.argv:
        from report import load_env, resolve_chat_id, split_message, tg

        text = week_report()
        if "--dry" in sys.argv:
            print(text)
            sys.exit()
        env = load_env()
        chat_id = resolve_chat_id(env["TG_BOT_TOKEN"], env)
        for chunk in split_message(text):
            tg(env["TG_BOT_TOKEN"], "sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML")
        sys.exit()
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-09-29"
    items = negotiations()
    report(f"До {start}", [n for n in items if n["created_at"][:10] < start])
    report(f"С {start}", [n for n in items if n["created_at"][:10] >= start])
