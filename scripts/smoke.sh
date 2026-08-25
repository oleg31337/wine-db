#!/usr/bin/env bash
# Live smoke test against a running wine-db instance.
# Usage: scripts/smoke.sh [base_url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8099}"
JAR="$(mktemp)"
trap 'rm -f "$JAR" /tmp/wine-smoke-*.jpg /tmp/wine-smoke-backup.zip' EXIT

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

csrf() { grep -o 'winedb_csrf[[:space:]].*' "$JAR" | awk '{print $NF}' | tail -1; }

req() { # req METHOD PATH [curl args...]
  local method="$1" path="$2"; shift 2
  curl -sS -o /tmp/wine-smoke-body -w '%{http_code}' -b "$JAR" -c "$JAR" \
       -X "$method" "$BASE$path" -H "X-CSRF-Token: $(csrf)" "$@"
}

expect() { # expect CODE ACTUAL LABEL
  [ "$2" = "$1" ] || { echo "     got $2, body: $(head -c 300 /tmp/wine-smoke-body)"; fail "$3"; }
  pass "$3"
}

echo "== wine-db smoke test against $BASE =="

expect 200 "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/healthz")" "health check"
expect 200 "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/")" "index page served"
expect 401 "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/wines")" "API rejects anonymous"

# Seed the CSRF cookie.
curl -sS -o /dev/null -c "$JAR" "$BASE/" 

U="smoke$RANDOM"
expect 201 "$(req POST /api/auth/register -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"Smoke-Test-Pass-9!\"}")" "register user"
expect 200 "$(req GET /api/auth/me)" "session works"

WINE_ID=$(req POST /api/wines -H 'Content-Type: application/json' -d '{
  "name":"Smoke Test Barolo","maker":"Cantina Smoke","wine_type":"red",
  "country":"Italy","region":"Piedmont","vintage":2018,"grape":"Nebbiolo",
  "alcohol_pct":14.5,"sugar_g_l":1.5,"aromas":"tar, rose, cherry",
  "acidity":5,"sweetness":0,"body":5,"mouthfeel":4,"wood":3}' >/dev/null
  python3 -c "import json;print(json.load(open('/tmp/wine-smoke-body'))['id'])")
[ -n "$WINE_ID" ] && pass "create wine ($WINE_ID)" || fail "create wine"

expect 200 "$(req GET "/api/wines/$WINE_ID")" "fetch wine detail"
expect 200 "$(req PUT "/api/wines/$WINE_ID/rating" -H 'Content-Type: application/json' -d '{"stars":5}')" "rate 5 stars"

LONG=$(python3 -c "print('Excellent structure. ' * 60)")
expect 201 "$(req POST "/api/wines/$WINE_ID/comments" -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys;print(json.dumps({'body':sys.argv[1]}))" "$LONG")")" "comment >1000 chars"

python3 -c "
from PIL import Image
Image.new('RGB',(900,1200),(90,30,45)).save('/tmp/wine-smoke-label.jpg')"
expect 200 "$(req PUT "/api/wines/$WINE_ID/photo" -F "file=@/tmp/wine-smoke-label.jpg")" "upload photo"
expect 200 "$(req GET "/api/wines/$WINE_ID/photo")" "serve photo"
expect 400 "$(req PUT "/api/wines/$WINE_ID/photo" -F "file=@/etc/hostname;filename=x.jpg;type=image/jpeg")" "reject non-image"

LIST_ID=$(req POST /api/favorites -H 'Content-Type: application/json' -d '{"name":"Smoke Favorites"}' >/dev/null
  python3 -c "import json;print(json.load(open('/tmp/wine-smoke-body'))['id'])")
[ -n "$LIST_ID" ] && pass "create favorites list" || fail "create favorites list"
expect 204 "$(req PUT "/api/favorites/$LIST_ID/wines/$WINE_ID")" "add wine to list"

expect 200 "$(req GET "/api/wines?q=barolo")" "search by name"
expect 200 "$(req GET "/api/wines?wine_type=red&country=Italy&min_rating=4")" "filtered search"
expect 200 "$(req GET /api/wines/facets)" "facets"
expect 403 "$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" -X POST "$BASE/api/wines" \
  -H 'Content-Type: application/json' -d '{"name":"No CSRF"}')" "CSRF enforced"

curl -sS -b "$JAR" "$BASE/api/backup/export" -o /tmp/wine-smoke-backup.zip
python3 -c "
import zipfile,sys
z=zipfile.ZipFile('/tmp/wine-smoke-backup.zip')
names=z.namelist()
assert 'data.json' in names, names
assert any(n.startswith('photos/') for n in names), names
import json; d=json.loads(z.read('data.json'))
assert d['wines'] and d['comments'] and d['ratings'], 'backup missing rows'
assert 'password_hash' not in z.read('data.json').decode()
print('     archive:', ', '.join(names[:4]))" && pass "backup export verified" || fail "backup export"

expect 200 "$(req POST /api/backup/import -F "file=@/tmp/wine-smoke-backup.zip" -F 'mode=merge')" "backup import (merge)"
expect 204 "$(req DELETE "/api/wines/$WINE_ID")" "delete wine"
expect 204 "$(req POST /api/auth/logout)" "logout"
expect 401 "$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/api/auth/me")" "session ended"

echo
echo "All smoke checks passed."
