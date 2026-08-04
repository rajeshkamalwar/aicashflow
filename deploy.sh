#!/usr/bin/env bash
set -Eeuo pipefail

RELEASES_DIR="/opt/aicashflow/releases"
CURRENT_LINK="/opt/aicashflow/current"
PREVIOUS_LINK="/opt/aicashflow/previous"
ENV_FILE="/etc/aicashflow/aicashflow.env"
ASSERTION_FILE="/etc/nginx/aicashflow/proxy-assertion.conf"
HTPASSWD_FILE="/etc/nginx/.htpasswd-aicashflow"
SERVICE="aicashflow"
PYTHON=""
READINESS_TIMEOUT_SECONDS="${AI_CASHFLOW_DEPLOY_READINESS_TIMEOUT_SECONDS:-30}"
READINESS_INTERVAL_SECONDS="${AI_CASHFLOW_DEPLOY_READINESS_INTERVAL_SECONDS:-1}"
COMMIT=""
REPOSITORY="${AI_CASHFLOW_REPOSITORY_URL:-}"

usage() {
  echo "Usage: sudo bash deploy.sh --commit <40-character-commit> --repository <private-git-url>"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --repository) REPOSITORY="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "An explicit full release commit is required." >&2; exit 2; }
[[ -n "$REPOSITORY" ]] || { echo "An approved private Git repository is required." >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "Run deployment as root." >&2; exit 1; }

check_file() {
  local path="$1" owner="$2" mode="$3"
  [[ -f "$path" && "$path" != /opt/aicashflow/* && "$path" != /var/www/* ]] || {
    echo "Protected file metadata check failed: $path" >&2; exit 1;
  }
  [[ "$(stat -c '%U:%G' "$path")" == "$owner" && "$(stat -c '%a' "$path")" == "$mode" ]] || {
    echo "Protected file owner or mode is invalid: $path" >&2; exit 1;
  }
}

check_file "$ENV_FILE" "root:aicashflow" "640"
check_file "$ASSERTION_FILE" "root:root" "600"
check_file "$HTPASSWD_FILE" "root:www-data" "640"
id -u aicashflow >/dev/null 2>&1 || { echo "The aicashflow service account is missing." >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
PYTHON="${AI_CASHFLOW_PYTHON_BIN:-/usr/bin/python3.12}"

validate_python() {
  [[ -x "$PYTHON" ]] || {
    echo "Configured Python interpreter is unavailable: $PYTHON" >&2
    return 1
  }
  "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' || {
    echo "Configured Python interpreter must be Python 3.12: $PYTHON" >&2
    return 1
  }
}

wait_for_application() {
  local deadline=$((SECONDS + READINESS_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if systemctl is-active --quiet "$SERVICE" \
      && ss -ltn | grep -qE '127\.0\.0\.1:8001[[:space:]]' \
      && [[ "$(curl -fsS --max-time 2 http://127.0.0.1:8001/health 2>/dev/null || true)" == '{"status":"ok"}' ]]; then
      return 0
    fi
    sleep "$READINESS_INTERVAL_SECONDS"
  done
  echo "Application readiness timed out after ${READINESS_TIMEOUT_SECONDS}s." >&2
  return 1
}

[[ "$READINESS_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "Deployment readiness timeout must be a positive integer." >&2; exit 1;
}
validate_python || exit 1

SOURCE_ROOT="$(mktemp -d /tmp/aicashflow-release.XXXXXX)"
SOURCE_CHECKOUT="$SOURCE_ROOT/source"
SERVICE_TEMP_DIR=""
cleanup() {
  rm -rf -- "$SOURCE_ROOT"
  [[ -z "$SERVICE_TEMP_DIR" ]] || rm -rf -- "$SERVICE_TEMP_DIR"
}
trap cleanup EXIT

git clone --quiet --no-checkout --filter=blob:none -- "$REPOSITORY" "$SOURCE_CHECKOUT"
git -C "$SOURCE_CHECKOUT" fetch --quiet --depth=1 origin "$COMMIT"
git -C "$SOURCE_CHECKOUT" cat-file -e "$COMMIT^{commit}"
git -C "$SOURCE_CHECKOUT" checkout --quiet --detach "$COMMIT"
[[ -z "$(git -C "$SOURCE_CHECKOUT" status --porcelain --untracked-files=all)" ]] || {
  echo "Release source is dirty." >&2; exit 1;
}
[[ "$(git -C "$SOURCE_CHECKOUT" rev-parse HEAD)" == "$COMMIT" ]] || {
  echo "Checked-out release does not match the requested commit." >&2; exit 1;
}

RELEASE_DIR="$RELEASES_DIR/$COMMIT"
[[ ! -e "$RELEASE_DIR" ]] || { echo "Release already exists: $COMMIT" >&2; exit 1; }
install -d -o root -g root -m 755 "$RELEASES_DIR"
install -d -o root -g root -m 755 "$RELEASE_DIR"
git -C "$SOURCE_CHECKOUT" archive "$COMMIT" | tar -x -C "$RELEASE_DIR"
printf '%s\n' "$COMMIT" > "$RELEASE_DIR/RELEASE_ID"
BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\n' "$BUILD_TIMESTAMP" > "$RELEASE_DIR/BUILD_TIMESTAMP"

"$PYTHON" -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --require-hashes -r "$RELEASE_DIR/requirements.lock"
chown -R root:root "$RELEASE_DIR"

# validate-production must pass before any symlink or service change.
runuser -u aicashflow -- bash -c "set -a; source '$ENV_FILE'; set +a; PYTHONPATH='$RELEASE_DIR/src' '$RELEASE_DIR/.venv/bin/python' -m ai_cashflow.api.preflight"

SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
SERVICE_TEMP_DIR="$(mktemp -d /tmp/aicashflow-service.XXXXXX)"
SERVICE_CANDIDATE="$SERVICE_TEMP_DIR/aicashflow.service"
cat > "$SERVICE_CANDIDATE" <<EOF
[Unit]
Description=AI Cashflow FastAPI App
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=aicashflow
WorkingDirectory=${CURRENT_LINK}/src
Environment="PATH=${CURRENT_LINK}/.venv/bin"
EnvironmentFile=${ENV_FILE}
ExecStart=${CURRENT_LINK}/.venv/bin/uvicorn ai_cashflow.api.app:app --host 127.0.0.1 --port 8001 --workers 1 --no-proxy-headers
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemd-analyze verify "$SERVICE_CANDIDATE"

OLD_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
[[ -n "$OLD_RELEASE" && -d "$OLD_RELEASE" && "$OLD_RELEASE" == "$RELEASES_DIR/"* ]] || {
  echo "A verified current release must be bootstrapped under $RELEASES_DIR before deployment." >&2
  exit 1
}
restore_previous_release() {
  if [[ -n "$OLD_RELEASE" && -d "$OLD_RELEASE" ]]; then
    ln -s "$OLD_RELEASE" "${CURRENT_LINK}.rollback"
    mv -Tf "${CURRENT_LINK}.rollback" "$CURRENT_LINK"
    systemctl restart "$SERVICE" && wait_for_application
  fi
}

install -o root -g root -m 644 "$SERVICE_CANDIDATE" "$SERVICE_FILE"
systemctl daemon-reload

ln -s "$OLD_RELEASE" "${PREVIOUS_LINK}.next"
mv -Tf "${PREVIOUS_LINK}.next" "$PREVIOUS_LINK"
ln -s "$RELEASE_DIR" "${CURRENT_LINK}.next"
mv -Tf "${CURRENT_LINK}.next" "$CURRENT_LINK"

if ! systemctl restart "$SERVICE" \
  || ! wait_for_application \
  || ! bash "$RELEASE_DIR/verify-production.sh" --post-deploy "$COMMIT"; then
  echo "Post-start checks failed; restoring the previous release." >&2
  restore_previous_release
  exit 1
fi

echo "Release activated: ${COMMIT:0:12}"
