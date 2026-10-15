"""Новые ответы работодателей по откликам, с разбором через Claude.

collect() возвращает HTML-блок для отчёта (или "" если ничего нового)
и запоминает просмотренные сообщения в feedback_state.json.
Запуск отдельно: python3 feedback.py [--dry] — печатает блок, --dry не сохраняет состояние.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hh_applicant_tool.operations.apply_vacancies import CANDIDATE_QUESTION_RULES

DIR = Path(__file__).parent
TOOL = DIR / ".venv/bin/hh-applicant-tool"
STATE_FILE = DIR / "feedback_state.json"
PROFILE = (DIR / "profile.txt").read_text(encoding="utf-8")
# Второе резюме (офис/гибрид) отвечает по своему профилю
PROFILE_BY_RESUME = {
    os.environ["RESUME_OFFICE"]: (DIR / "profile_office.txt").read_text(encoding="utf-8"),
} if os.environ.get("RESUME_OFFICE") else {}
CONTACT = os.environ.get("CONTACT", "")
# Сколько раз подряд отвечаем боту-анкете и сколько ждём следующий вопрос
MAX_ROUNDS = 15
NEXT_QUESTION_WAIT = 20
SHIM_URL = "http://127.0.0.1:8765/v1/chat/completions"
FIRST_RUN_DAYS = 3
# Сколько раз повторяем неудавшийся автоответ (протухла сессия, сбой HH), прежде чем отдать человеку
MAX_ATTEMPTS = 3
MAX_SEEN = 5000

CATEGORIES = {
    "invitation": "🎉 Приглашение / живой контакт",
    "questionnaire": "📝 Анкета или бот-интервью",
    "test_task": "🧪 Тестовое задание",
    "question": "💬 Вопрос от рекрутера",
}
QUIET = {"rejection", "template", "spam"}
# Всегда молчим: бот-интервью Сбера
SKIP_PATTERN = re.compile(r"гигарекрутер|giga_recruiter", re.IGNORECASE)

CLASSIFY_PROMPT = """\
Ты разбираешь сообщения работодателя кандидату на hh.ru. Верни только JSON без markdown:
{"category": "...", "summary": "...", "contact": "..."}

category — одно из:
- invitation: живой человек приглашает на собеседование/созвон, просит связаться, оставляет контакт
- questionnaire: просят ответить на вопросы анкеты или пройти интервью с ботом
- test_task: предлагают тестовое задание
- question: живой рекрутер задаёт уточняющий вопрос
- rejection: отказ
- template: автоматическое «рассмотрим резюме», напоминания, прочие шаблоны без действия
- spam: предлагают зарегистрироваться на стороннем сервисе/платформе, стать автором, \
исполнителем, партнёром — то есть не работа по вакансии
summary — одно короткое предложение по-русски: что хотят и что сделать кандидату.
contact — телефон, telegram, email, ссылка на запись/анкету, если есть в тексте, иначе "".
"""


def api(endpoint: str, *params: str) -> dict:
    out = subprocess.run(
        [TOOL, "call-api", endpoint, *params],
        capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_check": None, "seen": []}


def ask_claude(system: str, user: str) -> str:
    body = {
        "model": "claude",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        SHIM_URL, json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def classify(vacancy: str, texts: list[str], state_id: str) -> dict:
    try:
        content = ask_claude(CLASSIFY_PROMPT, (
            f"Вакансия: {vacancy}\nСтатус отклика на hh: {state_id}\n"
            "Сообщения работодателя:\n" + "\n---\n".join(texts)
        ))
        content = content.removeprefix("```json").removesuffix("```").strip()
        return json.loads(content)
    except Exception:
        # Без Claude: отличаем только приглашение по статусу
        category = "invitation" if state_id == "interview" else "question"
        return {"category": category, "summary": texts[-1][:200], "contact": ""}


RECRUITER_REPLY_RULES = f"""\
ЗАДАЧА: живой рекрутер написал кандидату в чат hh.ru. Реши, можно ли ответить \
за кандидата только по фактам из профиля выше, и если можно — напиши ответ.

Отвечай, если рекрутер:
- спрашивает о готовности к формату работы, офису, переезду, командировкам;
- спрашивает о зарплатных ожиданиях, дате выхода, причине поиска работы;
- спрашивает об опыте, навыках, языке — и ответ есть в профиле;
- спрашивает, актуален ли поиск / интересна ли вакансия, просит написать, если интересно;
- просит контакт для связи — дай {CONTACT or 'контакт из профиля'}.

Ответь одним словом HUMAN и больше ничего, если:
- предлагают конкретное время созвона или просят выбрать слот/время;
- присылают тестовое задание, ссылку, внешнюю форму или файл;
- задают вопрос, ответа на который нет в профиле, или обсуждают условия оффера;
- это отказ, шаблон или сообщение, на которое отвечать не нужно.

Формат ответа: если в подписи есть имя рекрутера — начни с «Добрый день, <Имя>!», \
иначе с «Добрый день!». Дальше 1–3 коротких предложения по делу, без подписи, \
markdown и канцелярита. Телефон не давай. Не выдумывай ничего, чего нет в профиле.
"""


def unanswered(messages: list[dict]) -> list[dict]:
    """Сообщения работодателя после последнего сообщения кандидата."""
    tail = []
    for m in reversed(messages):
        if m["author"]["participant_type"] != "employer":
            break
        if (m.get("text") or "").strip():
            tail.append(m)
    return tail[::-1]


class RepeatedQuestion(Exception):
    """Бот повторил вопрос: скорее всего ждёт нажатия кнопки, а не текст."""


def tool(*args: str) -> dict:
    out = subprocess.run(
        [TOOL, *args], capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def last_chat_message(chat_id: int) -> dict | None:
    for page in range(5):
        items = tool("chat", "list", str(page)).get("chats", {}).get("items", [])
        for chat in items:
            if chat["id"] == chat_id:
                return chat.get("lastMessage")
        if not items:
            break
    return None


def answer_questionnaire(n: dict, vacancy: str) -> list[tuple[str, str]]:
    """Отвечает боту-анкете в чате HH, пока приходят новые вопросы.

    Если у вопроса есть кнопки — отправляем текст одной из них (как нажатие).
    """
    dialog = []
    asked: set[str] = set()
    for _ in range(MAX_ROUNDS):
        messages = api(f"/negotiations/{n['id']}/messages", "per_page=100").get("items", [])
        pending = unanswered(messages)
        if not pending:
            break
        # Последний вопрос уже отвечали — бот не принял ответ, дальше не пишем
        if pending[-1]["text"].strip() in asked:
            raise RepeatedQuestion(pending[-1]["text"].strip())
        asked.update(m["text"].strip() for m in pending)
        question = "\n".join(m["text"].strip() for m in pending)

        last = last_chat_message(int(n["chat_id"])) or {}
        buttons = [
            b["text"] for b in (last.get("actions") or {}).get("text_buttons", [])
            if b.get("text")
        ]
        # Отвечаем только боту и только на вопрос: итоговые сообщения,
        # отказы и сообщения живых рекрутеров не трогаем
        is_bot = (last.get("participantDisplay") or {}).get("isBot")
        if not is_bot:
            break
        if not buttons:
            verdict = ask_claude(
                "Ответь одним словом: YES или NO.",
                "Это сообщение бота-рекрутера — вопрос или просьба, на которую "
                "кандидат должен ответить текстом? Итоговые сообщения, "
                "благодарности и отказы — NO.\n\n" + question,
            )
            if not verdict.upper().startswith("YES"):
                break
        profile = PROFILE_BY_RESUME.get((n.get("resume") or {}).get("id"), PROFILE)
        system = profile + "\n\n" + CANDIDATE_QUESTION_RULES
        if buttons:
            choice = ask_claude(system, (
                f"Вакансия: {vacancy}\nВопрос: {question}\n"
                "Варианты ответа (кнопки): " + " | ".join(buttons) + "\n"
                "Пришли только текст одного подходящего варианта, дословно."
            )).strip().strip("«»\"'. ")
            answer = next((b for b in buttons if b.lower() == choice.lower()), None)
            if answer is None:
                raise ValueError(f"Claude не выбрал кнопку: {choice!r} из {buttons}")
        else:
            answer = ask_claude(system, (
                f"Вакансия: {vacancy}\nВопрос: {question}\n\n"
                "Если это приветствие бота вроде «Начнём?», ответь коротко о готовности. "
                "Если в сообщении несколько вопросов — ответь на каждый."
            ))
        if not answer:
            break
        subprocess.run(
            [TOOL, "chat", "send", str(n["chat_id"]), answer],
            check=True, capture_output=True, timeout=60,
        )
        dialog.append((question, answer))
        time.sleep(NEXT_QUESTION_WAIT)
    return dialog


def profile_for(n: dict) -> str:
    return PROFILE_BY_RESUME.get((n.get("resume") or {}).get("id"), PROFILE)


def reply_to_recruiter(n: dict, vacancy: str, texts: list[str]) -> str | None:
    """Ответ рекрутеру по профилю; None — нужен сам кандидат."""
    answer = ask_claude(profile_for(n) + "\n\n" + RECRUITER_REPLY_RULES, (
        f"Вакансия: {vacancy}\nСообщения рекрутера:\n" + "\n---\n".join(texts)
    )).strip()
    if not answer or answer.upper().startswith("HUMAN"):
        return None
    send = subprocess.run(
        [TOOL, "chat", "send", str(n["chat_id"]), answer],
        capture_output=True, text=True, timeout=60,
    )
    if send.returncode != 0 or "[E]" in send.stdout + send.stderr:
        raise RuntimeError((send.stdout + send.stderr).strip()[-200:] or "chat send failed")
    return answer


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def fetch_updates(since: datetime, seen: set[str]) -> list[dict]:
    updates = []
    for page in range(20):
        data = api("/negotiations", "order_by=updated_at", "per_page=50", f"page={page}")
        items = data.get("items", [])
        for n in items:
            if parse_time(n["updated_at"]) < since:
                return updates
            messages = api(f"/negotiations/{n['id']}/messages", "per_page=100").get("items", [])
            new = [
                m for m in messages
                if m["author"]["participant_type"] == "employer"
                and str(m["id"]) not in seen
                and (m.get("text") or "").strip()
            ]
            if new:
                waiting = bool(messages) and messages[-1]["author"]["participant_type"] == "employer"
                updates.append({"negotiation": n, "messages": new, "waiting": waiting})
        if page >= data.get("pages", 1) - 1:
            break
    return updates


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def collect(save: bool = True) -> str:
    state = load_state()
    now = datetime.now(timezone.utc)
    since = (
        parse_time(state["last_check"]) - timedelta(minutes=10)
        if state["last_check"]
        else now - timedelta(days=FIRST_RUN_DAYS)
    )
    seen = set(state["seen"])
    questionnaires_done = set(state.get("questionnaires_done", []))
    attempts: dict[str, int] = state.get("attempts", {})
    updates = fetch_updates(since, seen)

    def retry_later(n: dict, messages: list[dict]) -> bool:
        """Автоответ не удался: вернуть сообщения в очередь, пока не кончились попытки."""
        key = str(n["id"])
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] >= MAX_ATTEMPTS:
            return False
        seen.difference_update(str(m["id"]) for m in messages)
        return True


    grouped: dict[str, list[str]] = {k: [] for k in CATEGORIES}
    answered: list[str] = []
    replied: list[str] = []
    quiet = 0
    for u in updates:
        n = u["negotiation"]
        vacancy = n["vacancy"]
        employer = (vacancy.get("employer") or {}).get("name", "")
        texts = [m["text"] for m in u["messages"]]
        if any(SKIP_PATTERN.search(t) for t in texts):
            seen.update(str(m["id"]) for m in u["messages"])
            quiet += 1
            continue
        result = classify(f"{vacancy['name']} — {employer}", texts, n["state"]["id"])
        category = result.get("category", "question")
        seen.update(str(m["id"]) for m in u["messages"])
        if category in QUIET:
            quiet += 1
            continue
        category = category if category in CATEGORIES else "question"
        chat = f"https://hh.ru/chat/{n['chat_id']}" if n.get("chat_id") else vacancy["alternate_url"]
        # Анкету прямо в чате HH заполняем сами; внешние боты и формы — в отчёт
        # В каждый чат анкеты отвечаем только один раз за всё время
        if (
            category == "questionnaire" and save
            and str(n["id"]) not in questionnaires_done
            and not any("://" in t for t in texts)
        ):
            try:
                dialog = answer_questionnaire(n, f"{vacancy['name']} — {employer}")
            except RepeatedQuestion as ex:
                dialog = []
                questionnaires_done.add(str(n["id"]))
                result["summary"] = (
                    f"Бот ждёт ответа кнопкой на вопрос «{ex}» — ответь в чате сам."
                )
            except Exception as ex:
                if retry_later(n, u["messages"]):
                    continue
                dialog = []
                result["summary"] += f" (автоответ не удался: {ex})"
            if dialog:
                questionnaires_done.add(str(n["id"]))
                qa = "\n".join(
                    f"  ❓ {escape(q[:150])}\n  ✏️ {escape(a[:300])}" for q, a in dialog
                )
                answered.append(
                    f'• <b>{escape(employer)}</b> — <a href="{chat}">{escape(vacancy["name"])}</a>\n{qa}'
                )
                continue
        # Живому рекрутеру отвечаем сами, если хватает профиля и кандидат ещё не ответил
        if (
            category in ("question", "invitation") and save
            and u.get("waiting") and n.get("chat_id")
        ):
            try:
                reply = reply_to_recruiter(n, f"{vacancy['name']} — {employer}", texts)
            except Exception as ex:
                if retry_later(n, u["messages"]):
                    continue
                reply = None
                result["summary"] += f" (автоответ не удался: {ex})"
            if reply:
                question = re.sub(r"\s+", " ", texts[-1])
                replied.append(
                    f'• <b>{escape(employer)}</b> — <a href="{chat}">{escape(vacancy["name"])}</a>\n'
                    f"  ❓ {escape(question[:200])}\n  ✏️ {escape(reply[:300])}"
                )
                continue
        line = f'• <b>{escape(employer)}</b> — <a href="{chat}">{escape(vacancy["name"])}</a>\n  {escape(result.get("summary", ""))}'
        if result.get("contact"):
            line += f"\n  📞 {escape(result['contact'])}"
        grouped[category].append(line)

    if save:
        state["last_check"] = now.strftime("%Y-%m-%dT%H:%M:%S%z")
        state["seen"] = list(seen)[-MAX_SEEN:]
        state["questionnaires_done"] = list(questionnaires_done)
        state["attempts"] = attempts
        STATE_FILE.write_text(json.dumps(state))

    blocks = [
        f"\n{title}:\n" + "\n".join(grouped[key])
        for key, title in CATEGORIES.items() if grouped[key]
    ]
    if answered:
        blocks.append("\n🤖 Ответил на анкеты в чате:\n" + "\n".join(answered))
    if replied:
        blocks.append("\n✏️ Ответил рекрутерам за тебя (проверь):\n" + "\n".join(replied))
    return "\n".join(blocks)


if __name__ == "__main__":
    print(collect(save="--dry" not in sys.argv) or "Новых ответов нет")
