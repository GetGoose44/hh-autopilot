#!/bin/zsh
# Ставит (или переставляет) расписание автооткликов: будни, 9/14/19 + случайная пауза до 50 мин.
# Удалить расписание: install-schedule.sh --remove

LABEL="ru.hh-autoapply.run"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$PLIST"

if [[ "$1" == "--remove" ]]; then
  echo "Расписание удалено"
  exit 0
fi

INTERVALS=""
for d in 1 2 3 4 5; do
  for h in 9 14 19; do
    INTERVALS+="    <dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$h</integer><key>Minute</key><integer>0</integer></dict>
"
  done
done

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$HOME/hh-autoapply/hh-run.command</string>
    <string>--scheduled</string>
  </array>
  <key>StartCalendarInterval</key><array>
$INTERVALS  </array>
  <key>StandardOutPath</key><string>$HOME/hh-autoapply/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/hh-autoapply/logs/launchd.log</string>
</dict></plist>
EOF

plutil -lint "$PLIST" >/dev/null || { echo "Ошибка в plist"; exit 1; }
launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "Расписание установлено: будни, 9:00/14:00/19:00 + до 50 мин"
