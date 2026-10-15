#!/bin/zsh
# Один прогон: поднять резюме + отправить пачку автооткликов.
# Запуск: `./hh-run.sh` вручную или по расписанию (systemd/launchd).
# Аргументы передаются в apply, например: hh-run.command --dry-run
# --scheduled (первым аргументом) — запуск по расписанию: случайная пауза до 50 минут.

# Время берём у системы: EPOCHSECONDS в zsh однажды «застыл» после сна Mac
now() { date +%s }

SCHEDULED=0
if [[ "$1" == "--scheduled" ]]; then
  SCHEDULED=1
  shift
fi


DIR="${0:A:h}"
TOOL="$DIR/.venv/bin/hh-applicant-tool"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Долгосрочный токен Claude (claude setup-token): не зависит от входа в приложении
if [[ -f "$DIR/claude.env" ]]; then
  export CLAUDE_CODE_OAUTH_TOKEN="$(<"$DIR/claude.env")"
fi

# Настройки: резюме, регионы, контакт (см. examples/autopilot.env)
set -a; source "$DIR/autopilot.env"; set +a

mkdir -p "$DIR/logs"
LOG="$DIR/logs/$(date +%F_%H%M).log"
exec > >(tee -a "$LOG") 2>&1

# Не запускаться, если предыдущий прогон ещё идёт
LOCK="$DIR/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "Прогон уже идёт, выходим."
  exit 0
fi

SHIM_PID=""
cleanup() {
  [[ -n "$SHIM_PID" ]] && kill "$SHIM_PID" 2>/dev/null
  rmdir "$LOCK"
}
trap cleanup EXIT

if (( SCHEDULED )); then
  # Прогон каждый час: небольшая случайная пауза, чтобы не стучаться в HH ровно в :00
  DELAY=$(( RANDOM % 600 ))
  echo "Пауза $(( DELAY / 60 )) мин перед запуском"
  # Ждём по настенным часам: `sleep` в macOS не считает время сна,
  # и после пробуждения пауза растягивалась бы на часы
  END=$(( $(now) + DELAY ))
  while (( $(now) < END )); do sleep 20; done
fi

# Не даём Mac уснуть от простоя, пока идёт прогон (на сервере caffeinate нет и не нужен)
if command -v caffeinate >/dev/null; then caffeinate -i -w $$ & fi

echo "=== $(date '+%F %T') ==="
echo "Аргументы: $*"

# Прослойка к подписке Claude: каждый раз свежая, чтобы подхватить текущий токен
pkill -f claude_shim.py 2>/dev/null && sleep 1
python3 "$DIR/claude_shim.py" >> "$DIR/logs/shim.log" 2>&1 &
SHIM_PID=$!
for _ in {1..20}; do nc -z 127.0.0.1 8765 2>/dev/null && break; sleep 0.5; done

# После пробуждения Mac сеть поднимается не сразу: ждём HH до 10 минут
for _ in {1..30}; do
  curl -s -o /dev/null -m 10 https://api.hh.ru/ && break
  echo "Нет связи с api.hh.ru, ждём 20 сек"
  sleep 20
done

echo "--- Поднимаем резюме"
"$TOOL" update-resumes

echo "--- Проверка Claude"
# Если Claude недоступен (лимит подписки, слетел вход), ждём и пробуем снова,
# но не дольше 6 часов: к следующему прогону по расписанию уже не тянем
probe_claude() {
  curl -s -m 120 http://127.0.0.1:8765/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"x","messages":[{"role":"user","content":"Ответь одним словом: ок"}]}'
}
DEADLINE=$(( $(now) + 6 * 3600 ))
ALERTED=0
TRIES=0
PROBE=$(probe_claude)
while [[ "$PROBE" != *'"choices"'* ]]; do
  WAIT=$(python3 "$DIR/claude_wait.py" "$PROBE")
  TRIES=$(( TRIES + 1 ))
  # Предел по числу попыток — страховка, если часы опять поведут себя странно
  if (( $(now) + WAIT > DEADLINE || TRIES > 30 )); then
    echo "Claude так и не ответил, отклики не отправляем: $PROBE"
    "$DIR/.venv/bin/python" "$DIR/report.py" --alert \
      "Claude не отвечает больше 6 часов, отклики этого прогона не отправлены. Если это не лимит, запусти claude и выполни /login. Ответ: ${PROBE:0:300}"
    exit 1
  fi
  echo "Claude недоступен, ждём $(( WAIT / 60 )) мин: ${PROBE:0:200}"
  if (( ! ALERTED )); then
    "$DIR/.venv/bin/python" "$DIR/report.py" --alert \
      "Claude недоступен, прогон ждёт $(( WAIT / 60 )) мин и повторит сам. Ответ: ${PROBE:0:200}"
    ALERTED=1
  fi
  END=$(( $(now) + WAIT ))
  for _ in {1..1100}; do (( $(now) >= END )) && break; sleep 20; done
  PROBE=$(probe_claude)
done

echo "--- Доступ к вакансиям"
# Капча на карточках вакансий: откликаться бесполезно, ждём, тревога не чаще раза в 12 часов
CAPTCHA=$("$DIR/.venv/bin/python" "$DIR/captcha_check.py" 2>/dev/null)
if [[ -n "$CAPTCHA" ]]; then
  echo "HH требует капчу, отклики этого прогона пропускаем: $CAPTCHA"
  STAMP="$DIR/.captcha_alert"
  if [[ ! -f "$STAMP" ]] || (( $(now) - $(<"$STAMP") > 12 * 3600 )); then
    "$DIR/.venv/bin/python" "$DIR/report.py" --alert \
      "HH требует капчу на карточках вакансий, отклики на паузе. Обычно проходит само за несколько часов. Если держится дольше — открой ссылку в браузере и пройди капчу: $CAPTCHA"
    now > "$STAMP"
  fi
  exit 1
fi
rm -f "$DIR/.captcha_alert"

echo "--- Отклики"
# Берём всё, что опубликовано с начала прошлого успешного прогона (с запасом 30 мин).
# Уже отправленные отклики инструмент пропускает сам.
LAST_FILE="$DIR/.last_search"
RUN_STARTED=$(now)
if [[ -f "$LAST_FILE" ]]; then
  SINCE=$(( $(<"$LAST_FILE") - 1800 ))
else
  SINCE=$(( RUN_STARTED - 24 * 3600 ))
fi
# GNU date (Linux) понимает -d @epoch, BSD date (macOS) — -r epoch
DATE_FROM=$(date -d "@$SINCE" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null || date -r "$SINCE" +%Y-%m-%dT%H:%M:%S%z)
echo "Вакансии, опубликованные после $DATE_FROM"

# Живая шкала прогресса в Telegram; сама завершится вместе с прогоном
"$DIR/.venv/bin/python" "$DIR/progress.py" "$DATE_FROM" $$ >> "$DIR/logs/progress.log" 2>&1 &

# Два направления поиска в трёх регионах; progress.py повторяет те же фильтры
# Ищем по названию без фильтра роли: часть QA-вакансий заведена как «Программист, разработчик»;
# не-IT «контроль качества» отсекает ИИ (SKIP)
QA_SEARCH=(--search 'QA OR тестировщик OR тестирование OR SDET OR AQA OR tester OR "quality assurance"' --search-field name)
QA_EXCLUDED='\bjunior\b(?!\s*\+)|стаж[её]р|\bintern\b|младш|^(?!.*middle)(?=.*(\bsenior\b|\bsr\b|ведущ|старш))|\blead\b|\bлид\b|тимлид|техлид|лидер|руководител|\bhead\b|начальник|директор|архитектор|principal|\bstaff\b'
AI_SEARCH=(--search 'LLM OR "AI-инженер" OR "AI инженер" OR "AI engineer" OR "ИИ-инженер" OR "инженер ИИ" OR "prompt engineer" OR "промпт-инженер" OR "промпт инженер" OR "AI QA" OR "AI-агент" OR "AI агент" OR "AI agent" OR "ИИ-агент" OR "внедрению ИИ" OR "внедрению AI" OR "AI-аналитик" OR "AI аналитик" OR "AI analyst" OR "AI-специалист" OR "AI specialist"' --search-field name)
AI_EXCLUDED="$QA_EXCLUDED"'|продаж|маркетинг|маркетолог|data scientist|\bML\b|devops|mlops|\bлид\b|лидер|техлид|тимлид|\blead\b|руководител|начальник|\bhead\b|директор|архитектор'

# Чёрный список работодателей (скам и т.п.): «id  # название» по строке; его же читает inbox.py
EXCLUDED_EMPLOYERS=$(sed 's/#.*//' "$DIR/excluded_employers.txt" 2>/dev/null | tr -s ' \n' ' ' | xargs)

# Два резюме: основное — удалёнка и страна проживания (HOME_AREA);
# второе — офис/гибрид в городах из OFFICE_AREAS, со своим профилем (PROFILE=... перед вызовом).
# Без --resume-id инструмент откликается со всех опубликованных резюме, поэтому указываем явно

# --search включает полный поиск (без него HH отдаёт только «похожие на резюме»).
# Первый аргумент — направление (QA или AI), остальные — регион/формат
apply_pass() {
  local -a search; local excluded
  if [[ $1 == AI ]]; then search=("${AI_SEARCH[@]}"); excluded=$AI_EXCLUDED
  else search=("${QA_SEARCH[@]}"); excluded=$QA_EXCLUDED; fi
  shift
  "$TOOL" -v apply \
    "${search[@]}" \
    --experience between1And3 between3And6 moreThan6 \
    --excluded-title "$excluded" \
    ${=EXCLUDED_EMPLOYERS:+--excluded-employer-id $EXCLUDED_EMPLOYERS} \
    --date-from "$DATE_FROM" \
    --salary 150000 --currency RUR \
    --order-by publication_time \
    --use-ai -f \
    --candidate-profile "${PROFILE:-$DIR/profile.txt}" \
    --total-pages 5 --per-page 100 \
    "$@"
}

# Инструмент бросает проход на первой сетевой ошибке (HH через VPN рвёт соединения),
# поэтому повторяем проход; уже отправленные отклики он пропускает сам
apply_with_retries() {
  local rc
  for attempt in {1..5}; do
    apply_pass "$@" && return 0
    rc=$?
    echo "Проход упал (код $rc), попытка $attempt из 5, повтор через минуту"
    sleep 60
  done
  return $rc
}

# A/B-тест: по нечётным дням года удалёнка идёт со второго резюме и его профилем,
# по чётным — с основного; сравнение — stats.py --week, срез «A/B удалёнки»
if (( $(date +%j) % 2 )); then
  REMOTE_RESUME=$RESUME_OFFICE REMOTE_PROFILE=$DIR/profile_office.txt
else
  REMOTE_RESUME=$RESUME_MAIN REMOTE_PROFILE=$DIR/profile.txt
fi
echo "Удалёнка сегодня с резюме $REMOTE_RESUME"

echo "--- Удалёнка, любой регион"
PROFILE=$REMOTE_PROFILE apply_with_retries QA --resume-id $REMOTE_RESUME --work-format REMOTE "$@"
REMOTE_RC=$?
echo "--- Страна проживания, любой формат"
apply_with_retries QA --resume-id $RESUME_MAIN --area $HOME_AREA "$@"
CITY_RC=$?
echo "--- AI-направление: удалёнка, любой регион"
PROFILE=$REMOTE_PROFILE apply_with_retries AI --resume-id $REMOTE_RESUME --work-format REMOTE "$@"
AI_REMOTE_RC=$?
echo "--- AI-направление: страна проживания, любой формат"
apply_with_retries AI --resume-id $RESUME_MAIN --area $HOME_AREA "$@"
AI_CITY_RC=$?
echo "--- Офис/гибрид в выбранных городах"
PROFILE=$DIR/profile_office.txt apply_with_retries QA --resume-id $RESUME_OFFICE --area ${=OFFICE_AREAS} --work-format ON_SITE HYBRID "$@"
RU_OFFICE_RC=$?
echo "--- AI-направление: офис/гибрид в выбранных городах"
PROFILE=$DIR/profile_office.txt apply_with_retries AI --resume-id $RESUME_OFFICE --area ${=OFFICE_AREAS} --work-format ON_SITE HYBRID "$@"
AI_RU_OFFICE_RC=$?

# Время запоминаем только после успешных проходов и не в пробном режиме,
# иначе следующий прогон пропустил бы вакансии
if (( REMOTE_RC == 0 && CITY_RC == 0 && AI_REMOTE_RC == 0 && AI_CITY_RC == 0 && RU_OFFICE_RC == 0 && AI_RU_OFFICE_RC == 0 )) && [[ "$*" != *--dry-run* ]]; then
  echo "$RUN_STARTED" > "$LAST_FILE"
fi

echo "=== Готово: $(date '+%F %T'), лог: $LOG"

"$DIR/.venv/bin/python" "$DIR/report.py" "$LOG"
