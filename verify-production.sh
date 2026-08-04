#!/usr/bin/env bash
set -uo pipefail

ENV_FILE="/etc/aicashflow/aicashflow.env"
CURRENT_LINK="/opt/aicashflow/current"
SERVICE="aicashflow"
CANONICAL="https://www.aicashflow.pro"

usage() {
  echo "Usage: sudo bash verify-production.sh [--post-deploy <40-character-commit>]"
  echo "Reports safe PASS/FAIL results only; it never prints protected values."
}

POST_COMMIT=""
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --post-deploy) POST_COMMIT="${2:-}" ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

failures=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "[PASS] $label"
  else
    echo "[FAIL] $label"
    failures=$((failures + 1))
  fi
}

metadata_ok() {
  local path="$1" owner="$2" mode="$3"
  [[ -f "$path" && "$(stat -c '%U:%G' "$path")" == "$owner" && "$(stat -c '%a' "$path")" == "$mode" ]]
}

current_release_ok() {
  local target
  target="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  [[ -n "$target" && -d "$target" && -f "$target/RELEASE_ID" ]]
}

release_id_ok() {
  local target expected actual
  target="$(readlink -f "$CURRENT_LINK")"
  expected="${POST_COMMIT:-$(basename "$target")}"
  actual="$(tr -d '\r\n' < "$target/RELEASE_ID")"
  [[ "$actual" == "$expected" ]]
}

check "protected environment owner and mode" metadata_ok "$ENV_FILE" root:aicashflow 640
check "protected assertion owner and mode" metadata_ok /etc/nginx/aicashflow/proxy-assertion.conf root:root 600
check "protected htpasswd owner and mode" metadata_ok /etc/nginx/.htpasswd-aicashflow root:www-data 640
check "current release symlink" current_release_ok
check "active release identifier" release_id_ok
check "systemd service account" bash -c "[[ \$(systemctl show '$SERVICE' -p User --value) == aicashflow ]]"
check "systemd loopback command and one worker" bash -c "systemctl show '$SERVICE' -p ExecStart --value | grep -q -- '--host 127.0.0.1.*--workers 1.*--no-proxy-headers'"
check "application service active" systemctl is-active --quiet "$SERVICE"
check "Nginx syntax" nginx -t
check "canonical redirect" bash -c "curl -sSI http://aicashflow.pro/ | grep -qi '^location: https://www.aicashflow.pro/'"
check "trusted authentication headers configured" bash -c "grep -q 'X-AI-Cashflow-Proxy-Assertion' '$CURRENT_LINK/nginx_vhost.conf'"
check "Nginx request-size limit" bash -c "grep -q 'client_max_body_size 11m;' '$CURRENT_LINK/nginx_vhost.conf'"
check "port 8001 loopback listener" bash -c "ss -ltn | grep -qE '127\\.0\\.0\\.1:8001[[:space:]]'"

if [[ -r "$ENV_FILE" && -x "$CURRENT_LINK/.venv/bin/python" ]]; then
  check "production environment and writable paths" runuser -u aicashflow -- bash -c "set -a; source '$ENV_FILE'; set +a; PYTHONPATH='$CURRENT_LINK/src' '$CURRENT_LINK/.venv/bin/python' -m ai_cashflow.api.preflight"
else
  echo "[FAIL] production environment and writable paths"
  failures=$((failures + 1))
fi

if [[ -n "$POST_COMMIT" && -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  CURL_MACHINE="$(mktemp /tmp/aicashflow-machine-curl.XXXXXX)"
  CURL_BROWSER="$(mktemp /tmp/aicashflow-browser-curl.XXXXXX)"
  chmod 600 "$CURL_MACHINE" "$CURL_BROWSER"
  cleanup() { rm -f -- "$CURL_MACHINE" "$CURL_BROWSER"; }
  trap cleanup EXIT
  printf 'silent\nshow-error\nheader = "X-API-Key: %s"\n' "$AI_CASHFLOW_API_KEY" > "$CURL_MACHINE"
  printf 'silent\nshow-error\nuser = "%s"\n' "$AI_CASHFLOW_SMOKE_BASIC_AUTH" > "$CURL_BROWSER"

  check "public health response" bash -c "[[ \$(curl -sS '$CANONICAL/health') == '{\"status\":\"ok\"}' ]]"
  check "browser endpoint requires Basic Auth" bash -c "[[ \$(curl -sS -o /dev/null -w '%{http_code}' '$CANONICAL/') == 401 ]]"
  check "readiness rejects missing credentials" bash -c "[[ \$(curl -sS -o /dev/null -w '%{http_code}' '$CANONICAL/ready') == 401 ]]"
  check "readiness accepts machine credentials" bash -c "[[ \$(curl --config '$CURL_MACHINE' '$CANONICAL/ready') == '{\"status\":\"ready\"}' ]]"
  check "HTML references active JavaScript" bash -c "curl --config '$CURL_BROWSER' '$CANONICAL/app' | grep -q '/assets/app.js?v=$POST_COMMIT'"
  printf 'url = "%s/phase0/amazon/financial-position?source_id=%s"\n' "$CANONICAL" "$AI_CASHFLOW_SMOKE_SOURCE_ID" >> "$CURL_MACHINE"
  check "financial position API authenticated" bash -c "[[ \$(curl --config '$CURL_MACHINE' -o /dev/null -w '%{http_code}') == 200 ]]"
fi

exit "$failures"
