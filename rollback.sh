#!/usr/bin/env bash
set -Eeuo pipefail

CURRENT_LINK="/opt/aicashflow/current"
PREVIOUS_LINK="/opt/aicashflow/previous"
SERVICE="aicashflow"

[[ $EUID -eq 0 ]] || { echo "Run rollback as root." >&2; exit 1; }
FAILED_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
PREVIOUS_RELEASE="$(readlink -f "$PREVIOUS_LINK" 2>/dev/null || true)"
[[ -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]] || {
  echo "No preserved previous release is available." >&2; exit 1;
}

ln -s "$PREVIOUS_RELEASE" "${CURRENT_LINK}.rollback"
mv -Tf "${CURRENT_LINK}.rollback" "$CURRENT_LINK"
if [[ -n "$FAILED_RELEASE" && -d "$FAILED_RELEASE" ]]; then
  ln -s "$FAILED_RELEASE" "${PREVIOUS_LINK}.failed"
  mv -Tf "${PREVIOUS_LINK}.failed" "$PREVIOUS_LINK"
fi
systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"
[[ "$(curl --silent --show-error --fail https://www.aicashflow.pro/health)" == '{"status":"ok"}' ]]
echo "Rollback completed. The database was not modified."
