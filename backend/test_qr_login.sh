#!/usr/bin/env bash
#
# End-to-end test for the QR device-login flow.
#
#   Edge calls /api/auth/device/pair -> shows QR -> phone calls
#   /api/auth/device/approve -> edge polls /api/auth/device/token for its token.
#
# Session state lives in Redis. Exercises the happy path plus every failure
# branch (bad codes, missing auth, double-approve, post-delivery poll, expiry).
# Requires the backend stack to be running:
#
#   cd backend && docker compose up -d
#   ./test_qr_login.sh

set -u

BASE="${BASE:-http://localhost:8100}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR"

PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }

# assert <description> <expected> <actual>
assert() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    echo "  $(green PASS): $desc"
    PASS=$((PASS + 1))
  else
    echo "  $(red FAIL): $desc"
    echo "        expected: $expected"
    echo "        actual:   $actual"
    FAIL=$((FAIL + 1))
  fi
}

# req <METHOD> <path> [data] [auth-header]
# Echoes "<http_status>\n<body>"; callers split with `head`/`tail`.
req() {
  local method="$1" path="$2" data="${3:-}" auth="${4:-}"
  local args=(-s -o /tmp/qr_body.$$ -w '%{http_code}' -X "$method" "$BASE$path")
  [[ -n "$data" ]] && args+=(-H 'Content-Type: application/json' -d "$data")
  [[ -n "$auth" ]] && args+=(-H "Authorization: Bearer $auth")
  local code
  code="$(curl "${args[@]}")"
  echo "$code"
  cat /tmp/qr_body.$$
  rm -f /tmp/qr_body.$$
}

rediscli() {
  ( cd "$COMPOSE_DIR" && docker compose exec -T redis redis-cli "$@" )
}

echo "=============================================="
echo " QR device-login flow test  ($BASE)"
echo "=============================================="

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[1] Edge starts a pairing session  (POST /api/auth/device/pair)"
RES="$(req POST /api/auth/device/pair)"
CODE="$(echo "$RES" | head -n1)"
BODY="$(echo "$RES" | tail -n +2)"
assert "returns 200" "200" "$CODE"
DEVICE_CODE="$(echo "$BODY" | jq -r '.device_code')"
PAIRING_CODE="$(echo "$BODY" | jq -r '.pairing_code')"
assert "device_code present" "true" "$([[ -n "$DEVICE_CODE" && "$DEVICE_CODE" != null ]] && echo true || echo false)"
assert "pairing_code present" "true" "$([[ -n "$PAIRING_CODE" && "$PAIRING_CODE" != null ]] && echo true || echo false)"
assert "expires_in is 60" "60" "$(echo "$BODY" | jq -r '.expires_in')"
assert "interval is 3" "3" "$(echo "$BODY" | jq -r '.interval')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[2] Edge polls before approval  (POST /api/auth/device/token)"
RES="$(req POST /api/auth/device/token "{\"device_code\":\"$DEVICE_CODE\"}")"
assert "returns 200" "200" "$(echo "$RES" | head -n1)"
assert "status is pending" "pending" "$(echo "$RES" | tail -n +2 | jq -r '.status')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[3] Edge polls with an unknown device_code"
RES="$(req POST /api/auth/device/token '{"device_code":"does-not-exist"}')"
assert "returns 404" "404" "$(echo "$RES" | head -n1)"
assert "detail explains" "Invalid device code" "$(echo "$RES" | tail -n +2 | jq -r '.detail')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[4] Phone approves without a token  (POST /api/auth/device/approve)"
RES="$(req POST /api/auth/device/approve "{\"pairing_code\":\"$PAIRING_CODE\"}")"
assert "returns 401" "401" "$(echo "$RES" | head -n1)"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[5] Register + log in a user (acts as the phone)"
TS="$(date +%s)"
EMAIL="qrtest+${TS}@example.com"
PHONE="0911${TS: -6}"
RES="$(req POST /api/auth/register "{\"email\":\"$EMAIL\",\"phone_number\":\"$PHONE\",\"name\":\"QR Tester\",\"password\":\"secret123\"}")"
assert "register returns 200" "200" "$(echo "$RES" | head -n1)"
RES="$(req POST /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"secret123\"}")"
assert "login returns 200" "200" "$(echo "$RES" | head -n1)"
USER_TOKEN="$(echo "$RES" | tail -n +2 | jq -r '.access_token')"
assert "user token present" "true" "$([[ -n "$USER_TOKEN" && "$USER_TOKEN" != null ]] && echo true || echo false)"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[6] Phone approves with an unknown pairing_code"
RES="$(req POST /api/auth/device/approve '{"pairing_code":"does-not-exist"}' "$USER_TOKEN")"
assert "returns 404" "404" "$(echo "$RES" | head -n1)"
assert "detail explains" "Pairing code not found" "$(echo "$RES" | tail -n +2 | jq -r '.detail')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[7] Phone approves the real pairing_code"
RES="$(req POST /api/auth/device/approve "{\"pairing_code\":\"$PAIRING_CODE\"}" "$USER_TOKEN")"
assert "returns 200" "200" "$(echo "$RES" | head -n1)"
assert "status is approved" "approved" "$(echo "$RES" | tail -n +2 | jq -r '.status')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[8] Edge polls again and receives its token"
RES="$(req POST /api/auth/device/token "{\"device_code\":\"$DEVICE_CODE\"}")"
assert "returns 200" "200" "$(echo "$RES" | head -n1)"
BODY="$(echo "$RES" | tail -n +2)"
assert "status is approved" "approved" "$(echo "$BODY" | jq -r '.status')"
EDGE_TOKEN="$(echo "$BODY" | jq -r '.access_token')"
assert "access_token present" "true" "$([[ -n "$EDGE_TOKEN" && "$EDGE_TOKEN" != null ]] && echo true || echo false)"
assert "token_type is bearer" "bearer" "$(echo "$BODY" | jq -r '.token_type')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[9] Edge token is a valid credential  (GET /api/auth/status)"
RES="$(req GET /api/auth/status '' "$EDGE_TOKEN")"
assert "returns 200" "200" "$(echo "$RES" | head -n1)"
assert "resolves to the same user" "$EMAIL" "$(echo "$RES" | tail -n +2 | jq -r '.email')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[10] Edge polls after token was already delivered (key deleted)"
RES="$(req POST /api/auth/device/token "{\"device_code\":\"$DEVICE_CODE\"}")"
assert "returns 404 (key gone)" "404" "$(echo "$RES" | head -n1)"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[11] Phone tries to approve the same pairing_code again (key was deleted on first approve)"
RES="$(req POST /api/auth/device/approve "{\"pairing_code\":\"$PAIRING_CODE\"}" "$USER_TOKEN")"
assert "returns 404 (key gone)" "404" "$(echo "$RES" | head -n1)"
assert "detail explains" "Pairing code not found" "$(echo "$RES" | tail -n +2 | jq -r '.detail')"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "[12] Pairing session expires (simulate by deleting Redis keys)"
RES="$(req POST /api/auth/device/pair)"
EXP_DEVICE_CODE="$(echo "$RES" | tail -n +2 | jq -r '.device_code')"
EXP_PAIRING_CODE="$(echo "$RES" | tail -n +2 | jq -r '.pairing_code')"
rediscli DEL "device:pair:${EXP_PAIRING_CODE}" "device:poll:${EXP_DEVICE_CODE}" >/dev/null
RES="$(req POST /api/auth/device/token "{\"device_code\":\"$EXP_DEVICE_CODE\"}")"
assert "poll returns 404 after expiry" "404" "$(echo "$RES" | head -n1)"
RES="$(req POST /api/auth/device/approve "{\"pairing_code\":\"$EXP_PAIRING_CODE\"}" "$USER_TOKEN")"
assert "approve returns 404 after expiry" "404" "$(echo "$RES" | head -n1)"

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "=============================================="
echo " Results: $(green "$PASS passed"), $([[ $FAIL -gt 0 ]] && red "$FAIL failed" || echo "$FAIL failed")"
echo "=============================================="
[[ $FAIL -eq 0 ]]
