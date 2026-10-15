#!/bin/zsh
# Зарубежные вакансии вне HH (таймер hh-abroad, будни).
# Разбор новых сообщений идёт через Claude, поэтому поднимаем прослойку,
# если её нет (во время прогона она уже работает).
DIR="${0:A:h}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
[[ -f "$DIR/claude.env" ]] && export CLAUDE_CODE_OAUTH_TOKEN="$(<"$DIR/claude.env")"
cd "$DIR" || exit 1
set -a; source "$DIR/autopilot.env"; set +a

SHIM_PID=""
if ! nc -z 127.0.0.1 8765 2>/dev/null; then
  python3 "$DIR/claude_shim.py" >> "$DIR/logs/shim.log" 2>&1 &
  SHIM_PID=$!
  for _ in {1..20}; do nc -z 127.0.0.1 8765 2>/dev/null && break; sleep 0.5; done
fi
trap '[[ -n "$SHIM_PID" ]] && kill "$SHIM_PID" 2>/dev/null' EXIT

"$DIR/.venv/bin/python" "$DIR/abroad.py" "$@"
