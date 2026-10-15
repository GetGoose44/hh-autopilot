"""Еженедельная подборка сербских вакансий с Infostud в Telegram (таймер hh-stats, по понедельникам).

На HH сербских IT-вакансий почти нет, поэтому смотрим Infostud: компании автомобильного
софта и производства плюс QA/тестирование по всей Сербии. Присылаем только новые объявления.
Запуск вручную: python serbia_digest.py [--dry]
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from report import escape, load_env, resolve_chat_id, split_message, tg

DIR = Path(__file__).parent
STATE_FILE = DIR / "serbia_state.json"
BASE = "https://poslovi.infostud.com"
# Поиск Infostud нечёткий (по «Bosch» отдаёт и водителей), поэтому фильтруем по ссылке объявления.
# Компании (запрос → часть слага работодателя): только стажировки, QA, софт, AI, данные
COMPANIES = {
    "Bosch": "bosch", "Continental": "continental", "RT-RK": "rt-rk", "HTEC": "htec", "ZF": "zf-",
    "FCA Srbija": "fca", "Stellantis": "stellantis", "Brose": "brose", "NIRA": "nira", "Elektrobit": "elektrobit",
}
ROLE = re.compile(r"intern|praks|prakt|(^|-)qa(-|$)|test|software|softver|(^|-)ai(-|$)|data|cloud|validation|embedded")
# QA и тестирование у любых работодателей
ROLE_QUERIES = ["QA engineer", "tester", "test automation", "software tester"]
QA = re.compile(r"(^|-)qa(-|$)|test(?!al)|quality-assurance")
# Не наш уровень
SENIOR = re.compile(r"senior|staff|(^|-)lead(-|$)|head|manager|director|principal")
LINK = re.compile(r'/posao/([a-z0-9-]+)/([a-z0-9-]+)/(\d+)')


def search(query: str) -> list[tuple[str, str, str]]:
    url = f"{BASE}/oglasi-za-posao?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    return list(dict.fromkeys(LINK.findall(html)))


def humanize(slug: str) -> str:
    return slug.replace("-", " ").strip().capitalize()


def main() -> None:
    dry = "--dry" in sys.argv
    try:
        seen = set(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        seen = set()
    found: dict[str, str] = {}
    for query in [*COMPANIES, *ROLE_QUERIES]:
        try:
            jobs = search(query)
        except OSError as ex:
            print(f"{query}: {ex}", file=sys.stderr)
            continue
        for slug, company, job_id in jobs:
            if query in COMPANIES:
                if COMPANIES[query] not in company or not ROLE.search(slug):
                    continue
            elif not QA.search(slug):
                continue
            if SENIOR.search(slug):
                continue
            if job_id not in seen:
                found[job_id] = (
                    f'• <a href="{BASE}/posao/{slug}/{company}/{job_id}">{escape(humanize(slug))}</a>'
                    f" — {escape(humanize(company))}"
                )
        time.sleep(2)  # не долбим Infostud
    if not dry:
        STATE_FILE.write_text(json.dumps(sorted(seen | set(found))))
    if not found:
        return
    text = f"🇷🇸 <b>Сербия: новые вакансии на Infostud ({len(found)})</b>\n\n" + "\n".join(found.values())
    if dry:
        print(text)
        return
    env = load_env()
    chat_id = resolve_chat_id(env["TG_BOT_TOKEN"], env)
    for chunk in split_message(text):
        tg(env["TG_BOT_TOKEN"], "sendMessage", chat_id=chat_id, text=chunk,
           parse_mode="HTML", disable_web_page_preview="true")


if __name__ == "__main__":
    main()
