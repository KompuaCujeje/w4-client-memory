#!/bin/bash
# Ждёт, пока сайт разберёт все события, и печатает те, что не done.
U=${SITE:-https://w4-client-memory.onrender.com}
for i in $(seq 1 180); do b=$(curl -sS -m 30 $U/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['busy'] or any(e['status']=='pending' for e in d['events']))" 2>/dev/null || echo True); [ "$b" = "False" ] && break; sleep 10; done
curl -sS -m 30 $U/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print([(e['code'],e['status'],e['error'][:60]) for e in d['events'] if e['status']!='done'] or 'all done', len(d['events']))"
