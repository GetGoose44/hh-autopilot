"""Отчёт о прогоне в Telegram.

Использование: python3 report.py <лог прогона>
               python3 report.py --alert "текст" — короткая тревога
Настройки в telegram.env рядом: TG_BOT_TOKEN=..., TG_CHAT_ID=...
Если TG_CHAT_ID не задан, берётся из последнего сообщения боту и сохраняется.
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import feedback

DIR = Path(__file__).parent
ENV_FILE = DIR / "telegram.env"
TOOL = DIR / ".venv/bin/hh-applicant-tool"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def tg(token: str, method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode() if params else None
    # Telegram из РФ иногда не отвечает: несколько попыток с растущей паузой
    for attempt in range(4):
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/{method}", data, timeout=30
            ) as r:
                return json.load(r)
        except OSError:
            if attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))


def resolve_chat_id(token: str, env: dict[str, str]) -> str | None:
    if env.get("TG_CHAT_ID"):
        return env["TG_CHAT_ID"]
    updates = tg(token, "getUpdates").get("result", [])
    chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
    if not chats:
        return None
    chat_id = str(chats[-1])
    with ENV_FILE.open("a") as f:
        f.write(f"\nTG_CHAT_ID={chat_id}\n")
    return chat_id


def vacancy_info(vacancy_id: str) -> tuple[str, bool | None, str]:
    """Название, допускает ли удалёнку и город вакансии."""
    try:
        out = subprocess.run(
            [TOOL, "call-api", f"/vacancies/{vacancy_id}"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        v = json.loads(out)
        formats = {f["id"] for f in v.get("work_format") or []}
        remote = "REMOTE" in formats or (v.get("schedule") or {}).get("id") == "remote"
        return f"{v['name']} — {v['employer']['name']}", remote, v["area"]["name"]
    except Exception:
        return f"вакансия {vacancy_id}", None, ""


def build_report(log: str) -> str:
    sent = re.findall(r"Отправили отклик на вакансию(?: с тестом)? (\S+)", log)
    skipped_ai = len(re.findall(r"Пропускаем вакансию, AI не написал письмо", log))
    resume_ok = "✅ Обновлено" in log
    errors = [
        line.strip() for line in log.splitlines()
        if re.search(r"\[E\]|Traceback|Error:", line)
    ]
    dry = bool(re.search(r"^Аргументы:.*--dry-run", log, re.M))

    lines = ["🤖 <b>HH автоотклики</b>" + (" (пробный прогон)" if dry else "")]
    lines.append("📄 Резюме: " + ("поднято" if resume_ok else "не поднято (рано или ошибка)"))
    remote, office, unknown = [], [], []
    for url in dict.fromkeys(sent):
        vacancy_id = url.rstrip("/").rsplit("/", 1)[-1]
        title, is_remote, city = vacancy_info(vacancy_id)
        if is_remote is None:
            unknown.append(f'• <a href="{url}">{escape(title)}</a>')
        elif is_remote:
            remote.append(f'• <a href="{url}">{escape(title)}</a>')
        else:
            office.append(f'• <a href="{url}">{escape(title)}</a> ({escape(city)})')
    lines.append(f"📨 Отправлено откликов: <b>{len(remote) + len(office) + len(unknown)}</b>")
    lines.append(f"\n🏠 Удалёнка: <b>{len(remote)}</b>")
    lines += remote
    lines.append(f"\n🏢 Офис / гибрид: <b>{len(office)}</b>")
    lines += office
    if unknown:
        lines.append(f"\n❔ Формат не определён (вакансия уже закрыта): {len(unknown)}")
        lines += unknown
    if skipped_ai:
        lines.append(f"⏭ Пропущено как нерелевантные: {skipped_ai}")
    try:
        feedback_block = feedback.collect()
    except Exception as ex:
        feedback_block = f"\n⚠️ Не удалось проверить ответы работодателей: {escape(str(ex)[:200])}"
    lines.append(feedback_block or "\n📭 Новых ответов работодателей нет")
    if errors:
        lines.append(f"⚠️ Ошибки ({len(errors)}):")
        lines += [f"<code>{escape(e[:200])}</code>" for e in errors[:5]]
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    return chunks + [current] if current.strip() else chunks


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> None:
    env = load_env()
    token = env.get("TG_BOT_TOKEN")
    if not token:
        print("telegram.env без TG_BOT_TOKEN, отчёт не отправлен")
        return
    chat_id = resolve_chat_id(token, env)
    if not chat_id:
        print("Не нашёл chat_id: напиши боту любое сообщение")
        return
    if sys.argv[1] == "--alert":
        text = "🚨 <b>HH автоотклики</b>\n" + escape(sys.argv[2])
    else:
        log = ANSI.sub("", Path(sys.argv[1]).read_text(errors="replace"))
        text = build_report(log)
    for chunk in split_message(text):
        tg(token, "sendMessage", chat_id=chat_id, text=chunk,
           parse_mode="HTML", disable_web_page_preview="true")
    print("Отчёт отправлен в Telegram")


if __name__ == "__main__":
    main()
