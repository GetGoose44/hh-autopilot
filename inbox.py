"""Чаты с работодателями: мгновенные уведомления и напоминания «ждут ответа».

Запускается таймером каждые 15 минут (inbox.sh):
- новые сообщения работодателей — сразу в Telegram (разбор feedback.collect());
- чаты, где последнее слово за работодателем и от тебя ждут действия
  (вопрос, ссылка, созвон, тест), — напоминание раз в сутки, но не дольше 3 дней.

Пока идёт прогон (.run.lock), новые сообщения не трогаем: их соберёт отчёт прогона.
Запуск вручную: .venv/bin/python inbox.py [--dry]
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedback
from report import load_env, resolve_chat_id, split_message, tg

DIR = Path(__file__).parent
STATE_FILE = DIR / "inbox_state.json"
LOCK = DIR / ".run.lock"
# Напоминания: как часто сканировать чаты, как часто напоминать про один чат, насколько старые чаты смотреть
SCAN_EVERY = timedelta(hours=1)
REMIND_EVERY = timedelta(hours=24)
MAX_AGE = timedelta(days=14)
# Не ответил работодателю дольше этого — значит, неинтересно, больше не напоминаем
REMIND_GIVE_UP = timedelta(days=3)
# Веб-сессия HH (нужна для ответов в чатах и откликов с анкетой) — проверка и тревога
SESSION_CHECK_EVERY = timedelta(hours=1)
SESSION_ALERT_EVERY = timedelta(hours=12)

# Системные тексты HH — не от людей, разбирать не нужно
SYSTEM_TEXT = re.compile(
    r"Ответьте на приглашение, даже если оно вам не интересно|"
    r"Ваши ответы отправлены работодателю",
    re.IGNORECASE,
)
# Чёрный список работодателей (общий с hh-run.command): «id  # название» по строке
EXCLUDED_FILE = DIR / "excluded_employers.txt"


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"last_scan": None, "reminded": {}}


def ignored_employers() -> set[str]:
    try:
        lines = EXCLUDED_FILE.read_text().splitlines()
        return {ln.split("#")[0].strip() for ln in lines if ln.split("#")[0].strip()}
    except OSError:
        return set()


def needs_action(n: dict, message: dict, cache: dict) -> bool:
    """Ждёт ли сообщение действия от кандидата — по разбору Claude, один раз на сообщение."""
    key = str(message["id"])
    if key not in cache:
        vacancy = n["vacancy"]
        employer = (vacancy.get("employer") or {}).get("name", "")
        result = feedback.classify(f"{vacancy['name']} — {employer}", [message["text"]], n["state"]["id"])
        cache[key] = result.get("category", "question")
    return cache[key] in feedback.CATEGORIES


def recent_negotiations() -> list[dict]:
    """Отклики, обновлённые за MAX_AGE, кроме отказов."""
    result = []
    for page in range(20):
        data = feedback.api("/negotiations", "order_by=updated_at", "per_page=100", f"page={page}")
        for n in data.get("items", []):
            if now() - feedback.parse_time(n["updated_at"]) > MAX_AGE:
                return result
            if n["state"]["id"] not in ("discard", "hired") and n.get("chat_id"):
                result.append(n)
        if page >= data.get("pages", 1) - 1:
            break
    return result


def waiting_chats(cache: dict) -> list[dict]:
    """Чаты, где последнее сообщение — от работодателя и требует действия."""
    found = []
    ignored = ignored_employers()
    for n in recent_negotiations():
        # Сообщения грузим только там, где работодатель мог чего-то ждать:
        # приглашения и отклики с непрочитанными обновлениями
        if n["state"]["id"] not in ("interview", "invitation") and not n.get("has_updates"):
            continue
        messages = feedback.api(f"/negotiations/{n['id']}/messages", "per_page=100").get("items", [])
        if not messages or messages[-1]["author"]["participant_type"] != "employer":
            continue
        text = (messages[-1].get("text") or "").strip()
        employer = (n["vacancy"].get("employer") or {}).get("name", "")
        if (
            not text
            or str((n["vacancy"].get("employer") or {}).get("id")) in ignored
            or feedback.SKIP_PATTERN.search(text)
            or SYSTEM_TEXT.search(text)
            or not needs_action(n, messages[-1], cache)
        ):
            continue
        found.append({
            "chat_id": str(n["chat_id"]),
            "employer": employer,
            "vacancy": n["vacancy"]["name"],
            "text": text,
            "since": feedback.parse_time(messages[-1]["created_at"]),
        })
    return found


def web_session_ok() -> bool:
    from hh_applicant_tool.main import HHApplicantTool

    tool = HHApplicantTool()
    for key, value in vars(tool._parser.parse_args(["test-session"])).items():
        if not hasattr(type(tool), key):
            setattr(tool, key, value)
    try:
        return bool(tool.get_redirect_config("https://hh.ru").get("account"))
    except Exception:
        return False


def session_alert(state: dict) -> str:
    last = state.get("session_check")
    if last and now() - datetime.fromisoformat(last) < SESSION_CHECK_EVERY:
        return ""
    state["session_check"] = now().isoformat()
    if web_session_ok():
        state.pop("session_alert", None)
        return ""
    alerted = state.get("session_alert")
    if alerted and now() - datetime.fromisoformat(alerted) < SESSION_ALERT_EVERY:
        return ""
    state["session_alert"] = now().isoformat()
    return (
        "🔑 <b>HH разлогинил сервер</b>\n"
        "Не работают автоответы в чатах и отклики с анкетой. "
        "Войди заново: hh-applicant-tool authorize &lt;email&gt; — код придёт на почту."
    )


def ago(dt: datetime) -> str:
    hours = int((now() - dt).total_seconds() // 3600)
    return f"{hours // 24} дн {hours % 24} ч" if hours >= 24 else f"{hours} ч"


def reminders(state: dict) -> str:
    last = state.get("last_scan")
    if last and now() - datetime.fromisoformat(last) < SCAN_EVERY:
        return ""
    state["last_scan"] = now().isoformat()
    reminded = state.setdefault("reminded", {})
    cache = state.setdefault("categories", {})
    lines = []
    for c in waiting_chats(cache):
        prev = reminded.get(c["chat_id"])
        if prev and now() - datetime.fromisoformat(prev) < REMIND_EVERY:
            continue
        if now() - c["since"] > REMIND_GIVE_UP:
            continue
        reminded[c["chat_id"]] = now().isoformat()
        excerpt = feedback.escape(re.sub(r"\s+", " ", c["text"])[:250])
        lines.append(
            f'• <b>{feedback.escape(c["employer"])}</b> — '
            f'<a href="https://hh.ru/chat/{c["chat_id"]}">{feedback.escape(c["vacancy"])}</a>\n'
            f"  ⏱ ждут {ago(c['since'])}\n  «{excerpt}»"
        )
    if not lines:
        return ""
    return f"⏳ <b>Ждут твоего ответа ({len(lines)})</b>\n\n" + "\n\n".join(lines)


def main() -> None:
    dry = "--dry" in sys.argv
    state = load_state()
    parts = []
    if not LOCK.exists():
        block = feedback.collect(save=not dry)
        if block:
            parts.append("📬 <b>Новые ответы работодателей</b>" + block)
    alert = session_alert(state)
    if alert:
        parts.append(alert)
    reminder = reminders(state)
    if reminder:
        parts.append(reminder)
    if not dry:
        STATE_FILE.write_text(json.dumps(state))
    if not parts:
        return
    text = "\n\n".join(parts)
    if dry:
        print(text)
        return
    env = load_env()
    token = env["TG_BOT_TOKEN"]
    chat_id = resolve_chat_id(token, env)
    for chunk in split_message(text):
        tg(token, "sendMessage", chat_id=chat_id, text=chunk,
           parse_mode="HTML", disable_web_page_preview="true")


if __name__ == "__main__":
    main()
