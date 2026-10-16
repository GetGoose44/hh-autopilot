"""Шкала прогресса прогона: живое сообщение в Telegram или вывод в терминал.

«Сделано» — отклики, отправленные с начала прогона (по данным HH),
«осталось» — свежие вакансии по фильтрам прогона, на которые отклика ещё нет.

Запуск из прогона:  python progress.py <date_from в ISO> <pid прогона>
В терминале:        python progress.py --watch   (берёт текущий прогон из лога)
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from report import load_env, resolve_chat_id, tg

DIR = Path(__file__).parent
TOOL = DIR / ".venv/bin/hh-applicant-tool"
LOCK = DIR / ".run.lock"
# Когда прогон дошёл до откликов: до этого он может часами ждать сеть или Claude
APPLY_STARTED = DIR / ".apply_started"
INTERVAL = 180
WIDTH = 20

# Те же фильтры, что в apply_pass из hh-run.command
BASE = [
    "experience=between1And3", "experience=between3And6", "experience=moreThan6",
    "salary=150000", "currency=RUR",
]
QA_EXCLUDED = r"\bjunior\b(?!\s*\+)|стаж[её]р|\bintern\b|младш"
DIRECTIONS = [
    (['text=QA OR тестировщик OR тестирование OR SDET OR AQA OR tester OR "quality assurance"', "search_field=name"],
     re.compile(QA_EXCLUDED, re.I)),
    (['text=LLM OR "AI-инженер" OR "AI инженер" OR "AI engineer" OR "ИИ-инженер" OR "инженер ИИ" '
      'OR "prompt engineer" OR "промпт-инженер" OR "промпт инженер" OR "AI QA" OR "AI-агент" '
      'OR "AI агент" OR "AI agent" OR "ИИ-агент" OR "внедрению ИИ" OR "внедрению AI" '
      'OR "AI-аналитик" OR "AI аналитик" OR "AI analyst" OR "AI-специалист" OR "AI specialist"',
      "search_field=name"],
     re.compile(QA_EXCLUDED + r"|продаж|маркетинг|маркетолог|data scientist|\bML\b|devops|mlops", re.I)),
]
REGIONS = [
    ["work_format=REMOTE"],
    [f"area={os.environ['HOME_AREA']}"],
    [f"area={a}" for a in os.environ["OFFICE_AREAS"].split()] + ["work_format=ON_SITE", "work_format=HYBRID"],
]
PASSES = [(search + region, excluded) for search, excluded in DIRECTIONS for region in REGIONS]


def api(endpoint: str, *params: str) -> dict:
    out = subprocess.run(
        [TOOL, "call-api", endpoint, *params],
        capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def search(date_from: str) -> tuple[set[str], dict[str, bool]]:
    """Вакансии без отклика и формат (удалёнка или нет) всех найденных."""
    remaining: set[str] = set()
    remote: dict[str, bool] = {}
    for extra, excluded in PASSES:
        for page in range(5):
            data = api("/vacancies", *BASE, *extra, f"date_from={date_from}",
                       "per_page=100", f"page={page}")
            for v in data.get("items", []):
                if excluded.search(v["name"]):
                    continue
                remote[v["id"]] = "REMOTE" in {f["id"] for f in v.get("work_format") or []}
                if not v.get("relations"):
                    remaining.add(v["id"])
            if page >= data.get("pages", 1) - 1:
                break
    return remaining, remote


def applied_since(started: datetime) -> set[str]:
    ids = set()
    for page in range(10):
        data = api("/negotiations", "order_by=updated_at", "per_page=50", f"page={page}")
        for n in data.get("items", []):
            if datetime.strptime(n["created_at"], "%Y-%m-%dT%H:%M:%S%z") >= started:
                ids.add(n["vacancy"]["id"])
            elif datetime.strptime(n["updated_at"], "%Y-%m-%dT%H:%M:%S%z") < started:
                return ids
        if page >= data.get("pages", 1) - 1:
            break
    return ids


def snapshot(date_from: str, started: datetime) -> dict:
    remaining, remote = search(date_from)
    done = applied_since(started) - remaining
    total = len(done) + len(remaining)
    return {
        "done": len(done), "total": total, "left": len(remaining),
        "remote": sum(remote.get(i, False) for i in done),
        "minutes": int((datetime.now(started.tzinfo) - started).total_seconds() // 60),
    }


def bar(s: dict, width: int = WIDTH) -> str:
    filled = round(width * s["done"] / s["total"]) if s["total"] else width
    pct = round(100 * s["done"] / s["total"]) if s["total"] else 100
    return f"{'▰' * filled}{'▱' * (width - filled)} {s['done']}/{s['total']} ({pct}%)"


def render(s: dict, final: bool) -> str:
    lines = [
        "✅ <b>Прогон завершён</b>" if final else "⏳ <b>Идёт прогон</b>",
        f"<code>{bar(s, 12)}</code>",
        f"🏠 удалёнка: {s['remote']} · 🏢 офис/гибрид: {s['done'] - s['remote']}",
        f"⏱ {s['minutes']} мин",
    ]
    if final and s["left"]:
        lines.append(f"Без отклика {s['left']}: не по профилю или ошибка, подробности в отчёте")
    return "\n".join(lines)


def run_alive(pid: int | None) -> bool:
    if pid:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
    return LOCK.exists()


def current_run() -> tuple[str, datetime] | None:
    """date_from и время старта идущего прогона — из его лога."""
    if not LOCK.exists():
        return None
    log = max((DIR / "logs").glob("20*.log"), key=lambda p: p.stat().st_mtime).read_text(errors="replace")
    start = re.findall(r"^=== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ===", log, re.M)
    since = re.findall(r"Вакансии, опубликованные после (\S+)", log)
    if not start or not since:
        return None
    try:
        date_from, started = APPLY_STARTED.read_text().split()
        if date_from == since[-1]:
            return date_from, datetime.fromisoformat(started)
    except (OSError, ValueError):
        pass
    return since[-1], datetime.strptime(start[-1], "%Y-%m-%d %H:%M:%S").astimezone()


def watch() -> None:
    run = current_run()
    if not run:
        print("Сейчас прогона нет (или он ещё не дошёл до откликов).")
        return
    date_from, started = run
    while True:
        alive = run_alive(None)
        s = snapshot(date_from, started)
        mark = "✅" if not alive else "⏳"
        print(f"\r{mark} {bar(s)}  🏠{s['remote']} 🏢{s['done'] - s['remote']}  ⏱{s['minutes']} мин   ",
              end="", flush=True)
        if not alive or "--once" in sys.argv:
            print()
            return
        time.sleep(30)


def telegram(date_from: str, pid: int) -> None:
    env = load_env()
    token = env.get("TG_BOT_TOKEN")
    chat_id = token and resolve_chat_id(token, env)
    if not chat_id:
        return
    # Шкалу запускают в момент начала откликов — от него и считаем минуты
    started = datetime.now().astimezone()
    APPLY_STARTED.write_text(f"{date_from} {started.isoformat()}")
    last = render(snapshot(date_from, started), False)
    msg = tg(token, "sendMessage", chat_id=chat_id, parse_mode="HTML", text=last)["result"]["message_id"]
    snapshot_done = -1
    while True:
        time.sleep(INTERVAL)
        alive = run_alive(pid)
        try:
            s = snapshot(date_from, started)
            snapshot_done = s["done"]
            text = render(s, not alive)
            if text != last:  # одинаковый текст Telegram отвергает с ошибкой 400
                tg(token, "editMessageText", chat_id=chat_id, message_id=msg,
                   parse_mode="HTML", text=text)
                last = text
        except Exception as ex:
            # Сбой сети или HH не должен ронять шкалу — попробуем через минуту
            print(f"progress: {str(ex)[:200]}", file=sys.stderr)
        if not alive:
            # Прогоны идут каждый час: пустую шкалу (ни одного отклика) убираем из чата
            if snapshot_done == 0:
                tg(token, "deleteMessage", chat_id=chat_id, message_id=msg)
            return


if __name__ == "__main__":
    if sys.argv[1] == "--watch":
        watch()
    else:
        telegram(sys.argv[1], int(sys.argv[2]))
