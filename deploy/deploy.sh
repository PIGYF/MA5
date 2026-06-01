#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_DIR="${PROJECT_DIR}/deploy/state"
LOG_DIR="${PROJECT_DIR}/deploy/logs"
LAST_SUCCESS_FILE="${STATE_DIR}/last_success_commit"

REPO_DIR="${REPO_DIR:-${PROJECT_DIR}}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8764/health}"
SERVICES="${SERVICES:-ma5-web-app.service ma5-web-site.service}"
ROLLBACK_ON_FAIL="${ROLLBACK_ON_FAIL:-1}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"

mkdir -p "${STATE_DIR}" "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/deploy-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[INFO] 开始部署：$(date '+%F %T')"
echo "[INFO] 项目目录：${REPO_DIR}"
echo "[INFO] 目标分支：${DEPLOY_BRANCH}"

ROLLBACK_COMMIT=""

on_error() {
  local exit_code="$?"
  local line_no="$1"
  echo "[ERROR] 部署失败，行号=${line_no}，退出码=${exit_code}"

  if [[ "${ROLLBACK_ON_FAIL}" == "1" && -n "${ROLLBACK_COMMIT}" ]]; then
    echo "[INFO] 尝试自动回滚到：${ROLLBACK_COMMIT}"
    set +e
    "${SCRIPT_DIR}/rollback.sh" "${ROLLBACK_COMMIT}"
    local rb_code="$?"
    set -e
    if [[ "${rb_code}" -ne 0 ]]; then
      echo "[ERROR] 自动回滚失败，请人工介入检查。"
    else
      echo "[INFO] 自动回滚完成。"
    fi
  fi

  exit "${exit_code}"
}

trap 'on_error $LINENO' ERR

[[ -d "${REPO_DIR}/.git" ]]

if [[ -n "$(git -C "${REPO_DIR}" status --porcelain)" ]]; then
  echo "[ERROR] 工作区存在未提交改动，自动部署已停止。"
  exit 2
fi

CURRENT_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
if [[ -f "${LAST_SUCCESS_FILE}" ]]; then
  ROLLBACK_COMMIT="$(cat "${LAST_SUCCESS_FILE}")"
else
  ROLLBACK_COMMIT="${CURRENT_COMMIT}"
fi

echo "[INFO] 当前提交：${CURRENT_COMMIT}"
echo "[INFO] 回滚锚点：${ROLLBACK_COMMIT}"

git -C "${REPO_DIR}" fetch origin "${DEPLOY_BRANCH}"
git -C "${REPO_DIR}" checkout "${DEPLOY_BRANCH}"
git -C "${REPO_DIR}" pull --ff-only origin "${DEPLOY_BRANCH}"

NEW_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
echo "[INFO] 更新后提交：${NEW_COMMIT}"

if [[ -f "${REPO_DIR}/requirements.txt" ]]; then
  echo "[INFO] 检测到 requirements.txt，开始安装/更新依赖。"
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VENV_DIR}"
  fi
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"
  PYTHON_BIN="${VENV_DIR}/bin/python"
else
  PYTHON_BIN="python3"
fi

echo "[INFO] 开始 Python 语法检查。"
mapfile -t PY_FILES < <(find "${REPO_DIR}" -maxdepth 3 -type f -name "*.py" \
  -not -path "*/.venv/*" \
  -not -path "*/__pycache__/*")

if [[ "${#PY_FILES[@]}" -eq 0 ]]; then
  echo "[ERROR] 未找到可检查的 Python 文件。"
  exit 3
fi

"${PYTHON_BIN}" -m py_compile "${PY_FILES[@]}"

echo "[INFO] 重启服务：${SERVICES}"
for svc in ${SERVICES}; do
  systemctl restart "${svc}"
done

for svc in ${SERVICES}; do
  systemctl is-active --quiet "${svc}"
done

echo "[INFO] 健康检查：${HEALTH_URL}"
HEALTH_OK=0
for i in {1..20}; do
  if curl -fsS --max-time 5 "${HEALTH_URL}" >/dev/null; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done

if [[ "${HEALTH_OK}" -ne 1 ]]; then
  echo "[ERROR] 健康检查失败。"
  exit 4
fi

echo "${NEW_COMMIT}" > "${LAST_SUCCESS_FILE}"
echo "[INFO] 部署成功：${NEW_COMMIT}"
echo "[INFO] 日志文件：${LOG_FILE}"