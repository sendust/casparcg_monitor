#!/bin/bash
# Register CasparCG Server as a systemd service.
#
#   sudo ./install-casparcg-service.sh             install + enable (+ start if not already running)
#   sudo ./install-casparcg-service.sh --uninstall stop, disable and remove the unit
#
# Settings below can be overridden from the environment, e.g.
#   sudo CASPAR_CONFIG=/path/to/casparcg.config ./install-casparcg-service.sh
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-casparcg}"
CASPAR_USER="${CASPAR_USER:-sendust}"
CASPAR_BIN="${CASPAR_BIN:-/usr/bin/casparcg-server-2.5}"
CASPAR_CONFIG="${CASPAR_CONFIG:-/usr/share/casparcg-server-2.5/casparcg.config}"
# CEF writes its cache (cef-cache/) into the working directory unless <html><cache-path> is set
CASPAR_WORKDIR="${CASPAR_WORKDIR:-$(getent passwd "$CASPAR_USER" | cut -d: -f6)}"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    echo "Removed $UNIT_FILE"
    exit 0
fi

id "$CASPAR_USER" >/dev/null 2>&1 || die "user '$CASPAR_USER' does not exist"
[ -x "$CASPAR_BIN" ]    || die "binary not found: $CASPAR_BIN"
[ -r "$CASPAR_CONFIG" ] || die "config not found: $CASPAR_CONFIG"
[ -d "$CASPAR_WORKDIR" ] || die "working directory not found: $CASPAR_WORKDIR"

# Make sure the log directory from the config exists and is writable by the service user
LOG_DIR="$(sed -n 's:.*<log-path>\(.*\)</log-path>.*:\1:p' "$CASPAR_CONFIG" | head -1)"
if [ -n "$LOG_DIR" ]; then
    mkdir -p "$LOG_DIR"
    chown "$CASPAR_USER:$(id -gn "$CASPAR_USER")" "$LOG_DIR"
fi

# GPU access without a login session (logind only grants /dev/dri ACLs to seat users)
GROUPS_EXTRA=""
for g in video render; do
    getent group "$g" >/dev/null && GROUPS_EXTRA="$GROUPS_EXTRA $g"
done

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=CasparCG Server
After=network-online.target
Wants=network-online.target
# Give up if it crashes 5 times within 2 minutes (check: journalctl -u ${SERVICE_NAME})
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=${CASPAR_USER}
Group=$(id -gn "$CASPAR_USER")
SupplementaryGroups=${GROUPS_EXTRA# }
WorkingDirectory=${CASPAR_WORKDIR}
ExecStart=${CASPAR_BIN} ${CASPAR_CONFIG}
StandardInput=null
# Restart after any exit, including SIGTERM/SIGKILL from the monitor's Kill button.
# 'systemctl stop' still stops it for good.
Restart=always
RestartSec=3
TimeoutStopSec=20
# SIGTERM only the main process so it can shut CEF down itself; SIGKILL leftovers afterwards.
# (Default control-group mode signals the CEF helpers at the same time, and the server then
#  aborts with "GPU process isn't usable. Goodbye." and a core dump.)
KillMode=mixed
LimitNOFILE=65536
# Slightly higher CPU priority than normal processes for playout
Nice=-5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
echo "Installed and enabled $UNIT_FILE"

# Do not start a second instance: the DeckLink device and AMCP port would be in use
RUNNING="$(pgrep -u "$CASPAR_USER" -x "$(basename "$CASPAR_BIN" | cut -c1-15)" -a | grep -v -- '--type=' || true)"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Service already active. Apply unit changes with: sudo systemctl restart $SERVICE_NAME"
elif [ -n "$RUNNING" ]; then
    echo
    echo "CasparCG is already running outside systemd (on air?):"
    echo "  $RUNNING"
    echo "Stop it yourself when safe, then run:  sudo systemctl start $SERVICE_NAME"
else
    systemctl start "$SERVICE_NAME"
    sleep 3
    systemctl status "$SERVICE_NAME" --no-pager | head -12
fi
