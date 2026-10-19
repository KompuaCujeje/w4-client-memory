#!/bin/bash
# Догрузка новых событий БЕЗ сброса (stage2, sources): scripts/upload.sh файл_или_папка ...
# Повторно загруженный файл с тем же кодом считается дублем и не разбирается заново.
set -e
U=${SITE:-https://w4-client-memory.onrender.com}
args=()
for p in "$@"; do if [ -d "$p" ]; then for f in "$p"/*; do args+=(-F "files=@$f"); done; else args+=(-F "files=@$p"); fi; done
curl -sS -m 300 -o /dev/null -w "upload %{http_code}\n" -X POST $U/upload "${args[@]}"
"$(dirname "$0")/wait.sh"
