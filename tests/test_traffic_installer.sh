#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "This integration test must run as root (use the documented Docker command)." >&2
  exit 1
fi

REPO_DIR=${1:-/repo}
MOCK_BIN=$(mktemp -d)
trap 'rm -rf "$MOCK_BIN"' EXIT

cat >"$MOCK_BIN/ip" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == route ]]; then
  echo "default via 10.0.0.1 dev eth0"
  exit 0
fi
exit 0
EOF

cat >"$MOCK_BIN/apt-get" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"$MOCK_BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>/tmp/mock-systemctl.log
exit 0
EOF

cat >"$MOCK_BIN/vnstat" <<'EOF'
#!/usr/bin/env bash
if [[ " $* " == *" --oneline b "* ]]; then
  rx=${MOCK_RX_BYTES:-0}
  tx=${MOCK_TX_BYTES:-1024}
  printf '1;eth0;today;0;0;0;0;month;%s;%s;0;0;%s;%s;0\n' "$rx" "$tx" "$rx" "$tx"
fi
exit 0
EOF

cat >"$MOCK_BIN/crontab" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == -l ]]; then
  exit 1
fi
cp "$1" /tmp/installed-crontab
EOF

cat >"$MOCK_BIN/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"$MOCK_BIN/iptables" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF

cat >"$MOCK_BIN/flock" <<'EOF'
#!/usr/bin/env bash
if [[ -n ${MOCK_FLOCK_STATUS:-} ]]; then
  exit "$MOCK_FLOCK_STATUS"
fi
exec /usr/bin/flock "$@"
EOF

chmod +x "$MOCK_BIN"/*
export PATH="$MOCK_BIN:$PATH"

rm -f /tmp/mock-systemctl.log /tmp/installed-crontab
rm -rf /var/lib/gcp-free-audited

GCP_FREE_TRAFFIC_LIMIT_GIB=100 bash "$REPO_DIR/scripts/net_shutdown.sh"
bash -n /root/gcp_free_check_traffic.sh
grep -Fq 'LIMIT=100' /root/gcp_free_check_traffic.sh
grep -Fq '* * * * * /root/gcp_free_check_traffic.sh # gcp-free-audited' /tmp/installed-crontab
grep -Fq '@reboot /root/gcp_free_check_traffic.sh # gcp-free-audited' /tmp/installed-crontab

MOCK_RX_BYTES=107374182400 MOCK_TX_BYTES=1024 bash /root/gcp_free_check_traffic.sh
if grep -Fq 'poweroff' /tmp/mock-systemctl.log; then
  echo "RX traffic incorrectly triggered TX enforcement" >&2
  exit 1
fi
rm -f /var/log/traffic_monitor.log
mkdir /var/log/traffic_monitor.log
MOCK_TX_BYTES=107374182400 bash /root/gcp_free_check_traffic.sh
rm -rf /var/log/traffic_monitor.log
grep -Fq 'poweroff' /tmp/mock-systemctl.log

# A flock runtime error (as opposed to normal contention) must fail closed.
rm -f "/var/lib/gcp-free-audited/traffic-limit-$(date '+%Y-%m').reached"
: >/tmp/mock-systemctl.log
MOCK_FLOCK_STATUS=2 MOCK_TX_BYTES=1024 bash /root/gcp_free_check_traffic.sh
grep -Fq 'poweroff' /tmp/mock-systemctl.log

# Arbitrarily large but valid decimal counters must fail closed, not overflow.
rm -f "/var/lib/gcp-free-audited/traffic-limit-$(date '+%Y-%m').reached"
: >/tmp/mock-systemctl.log
MOCK_TX_BYTES=999999999999999999999999999999999999 bash /root/gcp_free_check_traffic.sh
grep -Fq 'poweroff' /tmp/mock-systemctl.log
test -f "/var/lib/gcp-free-audited/traffic-limit-$(date '+%Y-%m').reached"

# A same-month restart/check remains latched even if vnStat later reports less.
: >/tmp/mock-systemctl.log
MOCK_TX_BYTES=1024 bash /root/gcp_free_check_traffic.sh
grep -Fq 'poweroff' /tmp/mock-systemctl.log

echo "traffic installer integration test: PASS"
