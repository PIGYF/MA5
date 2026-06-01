#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_DIR="${PROJECT_DIR}/deploy/state"
LAST_SUCCESS_FILE="${STATE_DIR}/last_success_commit"

REPO_DIR="${REPO_DIR:-${PROJECT_DIR}}"
SERVICES="${SERVICES:-ma5-web-app.service ma5-web-site.service}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8764/health}"

TARGET_COMMIT="${1:-}"
if [[ -z "${TARGET_COMMIT}" ]]; then
  if [[ -f "${LAST_SUCCESS_FILE}" ]]; then
    TARGET_COMMIT="$(cat "${LAST_SUCCESS_FILE}")"
  else
    echo "[ERROR] 未提供回滚目标，且不存在 last_success_commit。"
    exit 2
  fi
fi

echo "[INFO] 回滚目标：${TARGET_COMMIT}"

git -C "${REPO_DIR}" cat-file -e "${TARGET_COMMIT}^{commit}"
git -C "${REPO_DIR}" reset --hard "${TARGET_COMMIT}"

if [[ -f "${REPO_DIR}/requirements.txt" ]]; then
  VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VENV_DIR}"
  fi
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"
fi

for svc in ${SERVICES}; do
  systemctl restart "${svc}"
done

for svc in ${SERVICES}; do
  systemctl is-active --quiet "${svc}"
done

HEALTH_OK=0
for i in {1..20}; do
  if curl -fsS --max-time 5 "${HEALTH_URL}" >/dev/null; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done

if [[ "${HEALTH_OK}" -ne 1 ]]; then
  echo "[ERROR] 回滚后健康检查失败，请人工介入。"
  exit 3
fi

echo "${TARGET_COMMIT}" > "${LAST_SUCCESS_FILE}"
echo "[INFO] 回滚完成。"