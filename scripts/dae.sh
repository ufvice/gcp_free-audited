#!/usr/bin/env bash
set -euo pipefail
umask 077

# Audited, immutable dependency set.  Upgrading any value requires a new audit.
DAE_VERSION="v1.0.0"
GEOIP_COMMIT="f09a7316510494c59852a638a6a85af1e3fddc99"
GEOIP_SHA256="7e0eecede617f16e02e9070df156300c88bae5b16d2885c28807a39ee0bbc378"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: this installer must run as root" >&2
  exit 1
fi

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "error: this audited installer supports x86_64 Linux only" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl unzip
else
  echo "error: this audited installer supports Debian/Ubuntu (apt-get) only" >&2
  exit 1
fi

machine="x86_64"
if grep -qw avx2 /proc/cpuinfo; then
  machine="x86_64_v3_avx2"
elif grep -qw sse /proc/cpuinfo; then
  machine="x86_64_v2_sse"
fi

case "$machine" in
  x86_64)
    dae_sha256="5adaa48546f73cd4403690bd90bf5d1ee16e430242591b323081d0401c53ee98"
    ;;
  x86_64_v2_sse)
    dae_sha256="0a973667d88af3f9a4c8bf300005507d111a4b036db1ccf5c54156e7122fe4ee"
    ;;
  x86_64_v3_avx2)
    dae_sha256="c6ebe11c69dc036d28fc1012ebbf2d6dfa6805528a369b468462d653c6b8a38e"
    ;;
esac

tmp_dir="$(mktemp -d /tmp/gcp-free-dae.XXXXXX)"
trap 'rm -rf "$tmp_dir"' EXIT
archive="$tmp_dir/dae-linux-$machine.zip"
geoip="$tmp_dir/geoip.dat"

curl --fail --show-error --location --proto '=https' --tlsv1.2 \
  "https://github.com/daeuniverse/dae/releases/download/$DAE_VERSION/dae-linux-$machine.zip" \
  --output "$archive"
printf '%s  %s\n' "$dae_sha256" "$archive" | sha256sum --check --strict -

curl --fail --show-error --location --proto '=https' --tlsv1.2 \
  "https://raw.githubusercontent.com/fatekey/gcp_free/$GEOIP_COMMIT/geoip.dat" \
  --output "$geoip"
printf '%s  %s\n' "$GEOIP_SHA256" "$geoip" | sha256sum --check --strict -

unzip -q "$archive" -d "$tmp_dir/unpacked"
binary="$tmp_dir/unpacked/dae-linux-$machine"
if [[ ! -f "$binary" ]]; then
  echo "error: verified archive does not contain the expected dae binary" >&2
  exit 1
fi

was_active=false
if systemctl is-active --quiet dae 2>/dev/null; then
  was_active=true
  systemctl stop dae
fi

install -o root -g root -m 0755 "$binary" /usr/local/bin/dae
install -d -o root -g root -m 0755 /usr/local/share/dae /usr/local/etc/dae
install -o root -g root -m 0644 "$geoip" /usr/local/share/dae/geoip.dat

# Audited from dae v1.0.0 install/dae.service; paths match this installer.
cat > /etc/systemd/system/dae.service <<'EOF'
[Unit]
Description=dae Service
Documentation=https://github.com/daeuniverse/dae
After=network-online.target docker.service systemd-sysctl.service
Wants=network-online.target

[Service]
Type=notify
User=root
LimitNPROC=512
LimitNOFILE=1048576
ExecStartPre=/usr/local/bin/dae validate -c /usr/local/etc/dae/config.dae
ExecStart=/usr/local/bin/dae run --disable-timestamp -c /usr/local/etc/dae/config.dae
ExecReload=/usr/local/bin/dae reload $MAINPID
Restart=on-abnormal
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/dae.service
systemctl daemon-reload
systemctl enable dae

if [[ "$was_active" == true && -f /usr/local/etc/dae/config.dae ]]; then
  systemctl start dae
fi

echo "dae $DAE_VERSION installed from verified, pinned artifacts ($machine)."
