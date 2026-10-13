#!/usr/bin/env bash
# Pobiera access token konta bota Matrix. Hasło wpisujesz lokalnie (nie ląduje w historii ani w czacie).
# Użycie: scripts/matrix-login.sh https://matrix.example.org @bot:example.org
set -euo pipefail
HS="${1:?homeserver URL}"; USER_ID="${2:?@bot:serwer}"
read -rsp "Hasło konta ${USER_ID}: " PW; echo
BODY=$(USER_ID="$USER_ID" PW="$PW" python3 -c 'import json,os; print(json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":os.environ["USER_ID"]},"password":os.environ["PW"],"initial_device_display_name":"authentik-im-sync"}))')
RESP=$(curl -fsS -X POST "${HS%/}/_matrix/client/v3/login" -H 'Content-Type: application/json' -d "$BODY")
echo "MATRIX_TOKEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
