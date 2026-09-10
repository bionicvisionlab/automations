#!/usr/bin/env bash
set -euo pipefail

EXPECTED_REPO_ROOT="/etc/bvl-automations"
VENV_DIR="/opt/bvl-automations/lab-monitor"
PYTHON_DIR="/opt/bvl-automations/python"
UV_BIN="/usr/local/bin/uv"
SERVICE_USER="labmonitor"
SERVICE_UNIT="lab-monitor.service"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

info() {
    echo "==> $*"
}

if [[ ${EUID} -ne 0 ]]; then
    fail "Run as root: sudo ./lab_monitor/install.sh"
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ "${REPO_ROOT}" != "${EXPECTED_REPO_ROOT}" ]]; then
    fail "This deployment expects the checkout at ${EXPECTED_REPO_ROOT}; found ${REPO_ROOT}"
fi

[[ -f "${SCRIPT_DIR}/pyproject.toml" ]] \
    || fail "Cannot find ${SCRIPT_DIR}/pyproject.toml"
[[ -f "${SCRIPT_DIR}/config.example.toml" ]] \
    || fail "Cannot find ${SCRIPT_DIR}/config.example.toml"
[[ -f "${SCRIPT_DIR}/lab_monitor.conf.example" ]] \
    || fail "Cannot find ${SCRIPT_DIR}/lab_monitor.conf.example"
[[ -f "${SCRIPT_DIR}/systemd/${SERVICE_UNIT}" ]] \
    || fail "Cannot find systemd/${SERVICE_UNIT}"

info "Ensuring service user exists"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

info "Ensuring uv is installed"
if [[ ! -x "${UV_BIN}" ]]; then
    command -v curl >/dev/null 2>&1 \
        || fail "curl is required to install uv"
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

"${UV_BIN}" --version

info "Ensuring managed Python 3.11 is installed"
install -d -m 0755 "${PYTHON_DIR}"
UV_PYTHON_INSTALL_DIR="${PYTHON_DIR}" \
    "${UV_BIN}" python install 3.11

venv_is_compatible=false
if [[ -x "${VENV_DIR}/bin/python" ]]; then
    if "${VENV_DIR}/bin/python" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
        venv_is_compatible=true
    fi
fi

if [[ "${venv_is_compatible}" == true ]]; then
    info "Reusing compatible environment at ${VENV_DIR}"
else
    if [[ -e "${VENV_DIR}" ]]; then
        info "Removing incompatible environment at ${VENV_DIR}"
        rm -rf "${VENV_DIR}"
    fi
    info "Creating Python 3.11 environment at ${VENV_DIR}"
    UV_PYTHON_INSTALL_DIR="${PYTHON_DIR}" \
        "${UV_BIN}" venv --python 3.11 --seed "${VENV_DIR}"
fi

info "Installing LabMonitor"
"${UV_BIN}" pip install \
    --python "${VENV_DIR}/bin/python" \
    "${SCRIPT_DIR}[slack,ble]"

info "Verifying LabMonitor import"
"${VENV_DIR}/bin/python" -c \
    'import lab_monitor; print("LabMonitor import OK")'

info "Installing live configuration templates without overwriting existing files"
if [[ ! -e "${EXPECTED_REPO_ROOT}/lab_monitor.toml" ]]; then
    install -m 0644 \
        "${SCRIPT_DIR}/config.example.toml" \
        "${EXPECTED_REPO_ROOT}/lab_monitor.toml"
    echo "    created ${EXPECTED_REPO_ROOT}/lab_monitor.toml"
else
    echo "    keeping existing ${EXPECTED_REPO_ROOT}/lab_monitor.toml"
fi

if [[ ! -e "${EXPECTED_REPO_ROOT}/.lab_monitor.conf" ]]; then
    install -m 0600 \
        "${SCRIPT_DIR}/lab_monitor.conf.example" \
        "${EXPECTED_REPO_ROOT}/.lab_monitor.conf"
    echo "    created ${EXPECTED_REPO_ROOT}/.lab_monitor.conf"
else
    chmod 0600 "${EXPECTED_REPO_ROOT}/.lab_monitor.conf"
    echo "    keeping existing ${EXPECTED_REPO_ROOT}/.lab_monitor.conf"
fi

info "Installing systemd unit"
install -m 0644 \
    "${SCRIPT_DIR}/systemd/${SERVICE_UNIT}" \
    "/etc/systemd/system/${SERVICE_UNIT}"
systemctl daemon-reload

if ! getent group bluetooth >/dev/null 2>&1; then
    echo
    echo "WARNING: group 'bluetooth' does not exist."
    echo "The shipped unit uses SupplementaryGroups=bluetooth for BLE access."
    echo "Install/configure BlueZ before starting LabMonitor with Govee sensors."
fi

echo
info "Installation complete"
echo "Python: $(${VENV_DIR}/bin/python --version 2>&1)"
echo
echo "Next steps:"
echo "  1. Follow lab_monitor/DEPLOYMENT.md to configure Netdata and the firewall."
echo "  2. Edit ${EXPECTED_REPO_ROOT}/lab_monitor.toml."
echo "  3. Edit ${EXPECTED_REPO_ROOT}/.lab_monitor.conf."
echo "  4. Run check-config and status as documented."
echo "  5. Only then: sudo systemctl enable --now lab-monitor"
