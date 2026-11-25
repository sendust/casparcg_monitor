#!/bin/bash
# Stop every automatic update / background update check on this playout server.
#
#   sudo ./disable-auto-updates.sh            disable
#   sudo ./disable-auto-updates.sh --undo     restore Ubuntu defaults
#
# Manual 'sudo apt update && sudo apt upgrade' still works afterwards
# (held packages are skipped until 'apt-mark unhold').
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

TIMERS="apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer
        update-notifier-download.timer update-notifier-motd.timer motd-news.timer"
SERVICES="unattended-upgrades.service"
CONF=/etc/apt/apt.conf.d/99-no-auto-update
# Playout-critical packages: never upgrade without planning (driver/CEF/DeckLink must match)
HOLD="casparcg-server-2.5 casparcg-cef-142 desktopvideo
      nvidia-driver-615-open libnvidia-gl-615
      linux-generic linux-image-generic linux-headers-generic"

if [ "${1:-}" = "--undo" ]; then
    rm -f "$CONF"
    for u in $TIMERS $SERVICES; do systemctl unmask "$u" 2>/dev/null || true; done
    systemctl enable --now $TIMERS $SERVICES 2>/dev/null || true
    command -v tailscale >/dev/null && tailscale set --auto-update=true || true
    echo "Restored automatic updates. Held packages are kept; release with: apt-mark unhold <pkg>"
    exit 0
fi

# 1. APT periodic jobs (update lists, download, unattended-upgrade)
cat > "$CONF" <<'EOF'
// Playout server: no automatic package activity (see disable-auto-updates.sh)
APT::Periodic::Enable "0";
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF

# 2. Timers and the unattended-upgrades shutdown hook
for u in $TIMERS $SERVICES; do
    systemctl disable --now "$u" 2>/dev/null || true
    systemctl mask "$u" >/dev/null 2>&1 || true
done

# 3. Snap refresh (no snaps installed now; prevents refreshes if one is added later)
command -v snap >/dev/null && snap refresh --hold=forever >/dev/null 2>&1 || true

# 4. Tailscale self-update
command -v tailscale >/dev/null && tailscale set --auto-update=false || true

# 5. Hold playout-critical packages so a manual 'apt upgrade' does not touch them
for p in $HOLD; do
    dpkg -s "$p" >/dev/null 2>&1 && apt-mark hold "$p" >/dev/null
done

echo "== timers =="
systemctl list-timers --all --no-pager | grep -E 'apt|fwupd|update-notifier|motd' || echo "(none active)"
echo "== held =="
apt-mark showhold
