"""Зарубежные вакансии вне HH: Сербия (Infostud, LinkedIn) и Таиланд (LinkedIn).

Раз в будний день (таймер hh-abroad):
- ищет новые вакансии, Claude по профилю решает, подходит ли, и пишет выжимку на русском;
- если в объявлении есть email работодателя и подключена почта (smtp.env) — сам отправляет
  письмо с PDF-резюме и сопроводительным на английском;
- остальные присылает в Telegram: выжимка + ссылка, где откликнуться.

Запуск вручную: python abroad.py [--dry]  (--dry: ничего не отправляет и не запоминает)
smtp.env: SMTP_HOST=smtp.mail.ru SMTP_PORT=465 SMTP_USER=... SMTP_PASSWORD=<пароль приложения>
"""

import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import feedback
from report import escape, load_env, resolve_chat_id, split_message, tg

DIR = Path(__file__).parent
STATE_FILE = DIR / "abroad_state.json"
SMTP_FILE = DIR / "smtp.env"
UA = {"User-Agent": "Mozilla/5.0"}
# Не больше стольких писем работодателям за запуск — остальное в Telegram
MAX_EMAILS = 10
# Сколько новых вакансий разбирать за запуск (каждая — вызов Claude); остальные — завтра
MAX_JUDGE = int(os.environ.get("ABROAD_LIMIT", "40"))

COUNTRIES = {
    "serbia": {
        "flag": "🇷🇸", "name": "Сербия",
        "profile": DIR / "profile.txt",
        "cv": DIR / "cv" / "Nikita_Semenov_QA_Engineer_CV.pdf",
        "linkedin_location": "Serbia",
        "linkedin_queries": ["QA engineer", "software tester", "test automation", "automotive internship", "AI quality"],
        "infostud": True,
        "roles": "QA и тестирование ПО, AI/LLM-качество, стажировки и junior-позиции в IT и автомобильной "
                 "отрасли (софт, данные, качество), кроме такси, перевозок и доставки",
    },
    "thailand": {
        "flag": "🇹🇭", "name": "Таиланд",
        "profile": DIR / "profile_th.txt",
        "cv": DIR / "cv" / "Nikita_Semenov_CV_Thailand.pdf",
        "linkedin_location": "Thailand",
        "linkedin_queries": ["QA engineer", "software tester", "Russian speaking", "Russian speaker customer",
                             "AI product", "customer success"],
        "infostud": False,
        "roles": "QA и тестирование, AI и продуктовые роли, а также не-IT роли из раздела профиля «РОЛИ НЕ ИЗ IT» "
                 "(клиенты, поддержка, операционные, роли для русскоговорящих, финтех, iGaming, стажёр в продажах)",
    },
}

# Infostud: компании (запрос → часть слага работодателя) и роли; фильтр по ссылке объявления
INFOSTUD_COMPANIES = {
    "Bosch": "bosch", "Continental": "continental", "RT-RK": "rt-rk", "HTEC": "htec", "ZF": "zf-",
    "FCA Srbija": "fca", "Stellantis": "stellantis", "Brose": "brose", "NIRA": "nira", "Elektrobit": "elektrobit",
}
INFOSTUD_ROLE_QUERIES = ["QA engineer", "tester", "test automation", "software tester"]
INFOSTUD_ROLE = re.compile(r"intern|praks|prakt|(^|-)qa(-|$)|test|software|softver|(^|-)ai(-|$)|data|cloud|validation|embedded")
INFOSTUD_QA = re.compile(r"(^|-)qa(-|$)|test(?!al)|quality-assurance")
INFOSTUD_LINK = re.compile(r"/posao/([a-z0-9-]+)/([a-z0-9-]+)/(\d+)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Адреса площадок и служебные — не работодатели
NOT_EMPLOYER = re.compile(r"infostud|linkedin|hrlab|noreply|no-reply|example|sentry|wixpress|\.png$|\.jpg$", re.I)

JUDGE_RULES = """\
ЗАДАЧА: в сообщении — вакансия ({country}). Подходящие роли: {roles}.
Реши по профилю выше, стоит ли кандидату откликаться, и верни только JSON без markdown:
{{"fit": true/false, "summary_ru": "...", "subject": "...", "letter": "..."}}

- fit — false, если роль не из списка подходящих, нужен язык, которого у кандидата нет (сербский, тайский),
  или главное требование — опыт, которого в профиле нет. Стажировки для студентов — fit true, но в summary_ru
  отметь «нужен статус студента».
- summary_ru — 2–3 коротких предложения по-русски: что за роль и компания, ключевые требования,
  чего из требований у кандидата нет. Без воды.
- subject — тема письма работодателю на английском: «Application: <должность> — Nikita Semenov».
- letter — сопроводительное письмо на английском (на языке вакансии, если она не на английском и кандидат
  им владеет), 4–6 предложений: приветствие, 2–3 самых релевантных факта из профиля под требования,
  честно про недостающее одной фразой, «My CV is attached.», подпись «Nikita Semenov».
  Только факты из профиля. Без телефона и ссылок. Где кандидат живёт сейчас — не писать.
  Если fit false — letter пустая строка.
"""


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def text_of(page: str) -> str:
    page = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


def employer_emails(page: str) -> list[str]:
    found = []
    for e in EMAIL.findall(page):
        e = re.sub(r"^u003[ce]", "", e).lower()
        if not NOT_EMPLOYER.search(e) and e not in found:
            found.append(e)
    return found


def infostud_jobs() -> list[dict]:
    jobs: dict[str, dict] = {}
    for query in [*INFOSTUD_COMPANIES, *INFOSTUD_ROLE_QUERIES]:
        try:
            page = fetch(f"https://poslovi.infostud.com/oglasi-za-posao?q={urllib.parse.quote(query)}")
        except OSError as ex:
            print(f"infostud {query}: {ex}", file=sys.stderr)
            continue
        for slug, company, job_id in dict.fromkeys(INFOSTUD_LINK.findall(page)):
            if query in INFOSTUD_COMPANIES:
                if INFOSTUD_COMPANIES[query] not in company or not INFOSTUD_ROLE.search(slug):
                    continue
            elif not INFOSTUD_QA.search(slug):
                continue
            url = f"https://poslovi.infostud.com/posao/{slug}/{company}/{job_id}"
            jobs[f"infostud:{job_id}"] = {
                "url": url, "apply": f"https://poslovi.infostud.com/konkurs/{job_id}",
                "title": slug.replace("-", " ").capitalize(), "company": company.replace("-", " ").capitalize(),
            }
        time.sleep(2)
    return [{"key": k, **v} for k, v in jobs.items()]


def linkedin_jobs(location: str, queries: list[str]) -> list[dict]:
    jobs: dict[str, dict] = {}
    for query in queries:
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
               + urllib.parse.urlencode({"keywords": query, "location": location, "f_TPR": "r604800"}))
        try:
            page = fetch(url)
        except OSError as ex:
            print(f"linkedin {query}: {ex}", file=sys.stderr)
            continue
        for card in page.split("<li")[1:]:
            m = re.search(r'href="(https://[a-z]+\.linkedin\.com/jobs/view/[^"?]*?-(\d+))[?"]', card)
            if not m:
                continue
            title = re.search(r'base-search-card__title[^>]*>\s*([^<]+)', card)
            company = re.search(r'base-search-card__subtitle[^>]*>.*?>\s*([^<]+)<', card, re.S)
            jobs[f"linkedin:{m.group(2)}"] = {
                "url": m.group(1), "apply": m.group(1), "id": m.group(2),
                "title": html.unescape(title.group(1).strip()) if title else "Вакансия",
                "company": html.unescape(company.group(1).strip()) if company else "",
            }
        time.sleep(3)
    return [{"key": k, **v} for k, v in jobs.items()]


def details(job: dict) -> tuple[str, list[str]]:
    """Текст вакансии (обрезанный) и email работодателя из объявления."""
    if job["key"].startswith("linkedin:"):
        page = fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['id']}")
        m = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", page, re.S)
        body = text_of(m.group(1)) if m else text_of(page)
    else:
        page = fetch(job["url"])
        body = text_of(page)
        start = body.find(job["title"].split(" ")[0])
        body = body[max(start, 0):]
    return body[:7000], employer_emails(page)


def judge(country: dict, job: dict, body: str) -> dict:
    system = country["profile"].read_text(encoding="utf-8") + "\n\n" + JUDGE_RULES.format(
        country=country["name"], roles=country["roles"])
    raw = feedback.ask_claude(system, f"Вакансия: {job['title']} — {job['company']}\n\n{body}")
    raw = raw.removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw)


def smtp_config() -> dict | None:
    if not SMTP_FILE.exists():
        return None
    env = dict(
        line.split("=", 1) for line in SMTP_FILE.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    env = {k.strip(): v.strip() for k, v in env.items()}
    return env if env.get("SMTP_USER") and env.get("SMTP_PASSWORD") else None


def send_application(smtp: dict, to: str, subject: str, letter: str, cv: Path) -> None:
    msg = EmailMessage()
    msg["From"] = f"Nikita Semenov <{smtp['SMTP_USER']}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(letter)
    msg.add_attachment(cv.read_bytes(), maintype="application", subtype="pdf", filename=cv.name)
    with smtplib.SMTP_SSL(smtp.get("SMTP_HOST", "smtp.mail.ru"), int(smtp.get("SMTP_PORT", "465")), timeout=60) as s:
        s.login(smtp["SMTP_USER"], smtp["SMTP_PASSWORD"])
        s.send_message(msg)


def main() -> None:
    dry = "--dry" in sys.argv
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {"seen": [], "emailed": []}
    seen, emailed = set(state["seen"]), set(state["emailed"])
    smtp = smtp_config()
    sent_count = judged = 0
    blocks = []
    for code, country in COUNTRIES.items():
        jobs = linkedin_jobs(country["linkedin_location"], country["linkedin_queries"])
        if country["infostud"]:
            jobs = infostud_jobs() + jobs
        sent, manual = [], []
        for job in jobs:
            if job["key"] in seen or judged >= MAX_JUDGE:
                continue
            judged += 1
            try:
                body, emails = details(job)
                verdict = judge(country, job, body)
            except Exception as ex:
                # Сбой сети или Claude — попробуем в следующий раз
                print(f"{job['key']}: {ex}", file=sys.stderr)
                continue
            seen.add(job["key"])
            if not verdict.get("fit"):
                continue
            head = (f'• <b>{escape(job["company"])}</b> — <a href="{job["url"]}">{escape(job["title"])}</a>\n'
                    f'  {escape(verdict.get("summary_ru", ""))}')
            to = next((e for e in emails if e not in emailed), None)
            if to and smtp and verdict.get("letter") and country["cv"].exists() and sent_count < MAX_EMAILS and not dry:
                try:
                    send_application(smtp, to, verdict["subject"], verdict["letter"], country["cv"])
                    emailed.add(to)
                    sent_count += 1
                    sent.append(head + f"\n  📧 отправил резюме на {escape(to)}")
                    continue
                except Exception as ex:
                    head += f"\n  ⚠️ письмо на {escape(to)} не ушло: {escape(str(ex)[:120])}"
            elif to and not smtp:
                head += f"\n  📧 в объявлении есть {escape(to)} — подключи почту, и буду отправлять сам"
            manual.append(head + f'\n  👉 <a href="{job["apply"]}">откликнуться</a>')
        if sent or manual:
            part = [f'{country["flag"]} <b>{country["name"]}</b>']
            if sent:
                part.append("\n📧 <b>Отправил резюме сам:</b>\n" + "\n".join(sent))
            if manual:
                part.append("\n👉 <b>Откликнись сам:</b>\n" + "\n".join(manual))
            blocks.append("\n".join(part))
    if not dry:
        state["seen"], state["emailed"] = sorted(seen), sorted(emailed)
        STATE_FILE.write_text(json.dumps(state))
    if not blocks:
        return
    text = "🌍 <b>Зарубежные вакансии</b>\n\n" + "\n\n".join(blocks)
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
