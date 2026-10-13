#!/usr/bin/env bash
# Pobiera access token konta bota Matrix. Hasło wpisujesz lokalnie (nie ląduje w historii ani w czacie).
# Użycie: scripts/matrix-login.sh https://matrix.example.org @bot:example.org
# Uwaga: działa tylko dla kont Z HASŁEM. Konto założone przez SSO (Authentik) hasła nie ma —
# wtedy weź token z Element: Ustawienia → Pomoc i informacje → Zaawansowane → Token dostępu.
set -uo pipefail
HS="${1:?homeserver URL, np. https://matrix.example.org}"; HS="${HS%/}"
USER_ID="${2:?@bot:serwer}"
API="$HS/_matrix/client/v3"

fail() { echo "BŁĄD: $*" >&2; exit 1; }

# 1. Czy pod tym adresem w ogóle jest API Matrixa i czy wolno logować hasłem?
FLOWS=$(curl -sS -m 15 "$API/login") || fail "nie mogę połączyć się z $API/login"
printf '%s' "$FLOWS" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(f["type"]=="m.login.password" for f in d["flows"]) else 2)' 2>/dev/null
case $? in
  0) ;;
  2) fail "ten homeserver nie pozwala na logowanie hasłem (tylko SSO?) — weź token z Element (patrz nagłówek skryptu)" ;;
  *) fail "to nie jest API Matrixa. Odpowiedź z $API/login: $(printf '%s' "$FLOWS" | head -c 200)
       Podaj adres homeservera (base_url z https://<domena>/.well-known/matrix/client), nie adres strony WWW." ;;
esac

# 2. Logowanie
read -rsp "Hasło konta ${USER_ID}: " PW; echo
BODY=$(USER_ID="$USER_ID" PW="$PW" python3 -c 'import json,os; print(json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":os.environ["USER_ID"]},"password":os.environ["PW"],"initial_device_display_name":"authentik-im-sync"}))')
OUT=$(curl -sS -m 20 -w '\n%{http_code}' -X POST "$API/login" -H 'Content-Type: application/json' -d "$BODY") || fail "połączenie nie powiodło się"
CODE="${OUT##*$'\n'}"; RESP="${OUT%$'\n'*}"
[ "$CODE" = "200" ] || fail "logowanie odrzucone (HTTP $CODE): $(printf '%s' "$RESP" | head -c 200)"
TOKEN=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])') || fail "odpowiedź bez access_token"
echo "MATRIX_TOKEN=$TOKEN"
