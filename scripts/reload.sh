#!/bin/bash
# Полная перезагрузка базы на сайте: сброс + все файлы stage1 (+ доп. папки аргументами) разом.
# Ждёт, пока Render выложит текущий коммит, затем — пока разбор не закончится.
# Нужно: RESET_CODE в окружении или в ~/.config/aim-case/env; DATA — папка tasks/w4 из hackathon-lab.
# Пример: DATA=../hackathon-lab/tasks/w4 scripts/reload.sh ../hackathon-lab/tasks/w4/stage2
# Платно: ≈45 ₽ за stage1 через ProxyAPI. Не пушить в main, пока идёт разбор.
set -e
[ -f ~/.config/aim-case/env ] && source ~/.config/aim-case/env
: "${RESET_CODE:?RESET_CODE не задан}"
U=${SITE:-https://w4-client-memory.onrender.com}
D=${DATA:-../hackathon-lab/tasks/w4}/stage1
want=$(git -C "$(dirname "$0")/.." rev-parse --short=7 HEAD)
for i in $(seq 1 60); do v=$(curl -sS -m 30 $U/api/version | python3 -c "import json,sys;print(json.load(sys.stdin)['commit'])" 2>/dev/null || true); [ "$v" = "$want" ] && break; sleep 10; done
echo "version $v (want $want)"
curl -sS -m 60 -o /dev/null -w "reset %{http_code}\n" -X POST $U/reset -F "code=$RESET_CODE"
args=(); for f in "$D"/E*; do args+=(-F "files=@$f"); done
for d in "$@"; do for f in "$d"/*; do args+=(-F "files=@$f"); done; done
curl -sS -m 120 -o /dev/null -w "upload %{http_code}\n" -X POST $U/upload "${args[@]}"
"$(dirname "$0")/wait.sh"
