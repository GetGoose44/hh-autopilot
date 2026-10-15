"""Сколько секунд ждать до сброса лимита Claude по тексту ошибки.

Пример ошибки: "You've hit your session limit · resets 2:50pm (Europe/Moscow)".
Печатает секунды до времени сброса + 3 минуты или 900, если времени в тексте нет.
"""

import re
import sys
from datetime import datetime, timedelta

text = sys.argv[1] if len(sys.argv) > 1 else ""
m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
if not m:
    print(900)
    sys.exit()
hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
now = datetime.now()
reset = now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
if reset <= now:
    reset += timedelta(days=1)
print(int((reset - now).total_seconds()) + 180)
