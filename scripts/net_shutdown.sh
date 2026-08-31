#!/bin/bash
set -eu
umask 077

# ==========================================
# 流量监控自动部署脚本 (关机版)
# 功能：
# 1. 自动获取网卡，只监控出站流量 (TX)
# 2. 达到阈值后：保留统计、锁定当月状态并立即关机
# 3. 同月重开时再次关机；新月份使用 vnStat 的新月度计数
# ==========================================

# 1. 检查 Root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "错误：请使用 root 权限运行此脚本。"
    exit 1
fi

LIMIT="${GCP_FREE_TRAFFIC_LIMIT_GIB:-100}"
case "$LIMIT" in
    ''|*[!0-9]*)
        echo "错误：流量阈值必须是整数 GiB。"
        exit 1
        ;;
esac
LIMIT=$((10#$LIMIT))
if [ "$LIMIT" -lt 1 ] || [ "$LIMIT" -gt 199 ]; then
    echo "错误：流量阈值必须在 1-199 GiB 之间。"
    exit 1
fi

# 2. 自动获取默认网卡名称
INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n1)

if [ -z "$INTERFACE" ]; then
    echo "错误：无法自动检测到网卡名称，请手动修改脚本中的 INTERFACE 变量。"
    exit 1
fi
if [[ ! "$INTERFACE" =~ ^[a-zA-Z0-9_.:@-]+$ ]]; then
    echo "错误：检测到不安全的网卡名称，拒绝生成 root 脚本。"
    exit 1
fi

echo "--> 检测到当前主网卡为: $INTERFACE"

# 3. 安装依赖工具
echo "--> 正在更新软件源并安装工具..."
apt-get update -y
apt-get install vnstat util-linux cron -y

# 4. 配置并启动 vnStat
echo "--> 配置 vnStat..."
if ! vnstat --add -i "$INTERFACE" 2>/dev/null; then
    echo "    (接口可能已存在，跳过添加)"
fi

systemctl enable vnstat
systemctl restart vnstat
systemctl enable --now cron

# 等待服务启动并生成初始数据库
sleep 5
vnstat -i "$INTERFACE" > /dev/null 2>&1

# 5. 生成本工具专属监控脚本
echo "--> 生成监控脚本 /root/gcp_free_check_traffic.sh..."
CHECK_SCRIPT_TMP=$(mktemp /root/.gcp_free_check_traffic.XXXXXX)
trap 'rm -f "$CHECK_SCRIPT_TMP"' EXIT
cat > "$CHECK_SCRIPT_TMP" <<EOF
#!/bin/bash
set -eu
umask 077

# 强制使用标准区域设置
export LC_ALL=C

# 配置
LOG_FILE="/var/log/traffic_monitor.log"
INTERFACE="$INTERFACE"
LIMIT=$LIMIT
LIMIT_BYTES=\$((LIMIT * 1073741824))
STATE_DIR="/var/lib/gcp-free-audited"
MONTH_KEY=\$(date '+%Y-%m')
TRIGGER_FILE="\$STATE_DIR/traffic-limit-\$MONTH_KEY.reached"

# 日志记录函数
log() {
    # Logging is best effort and must never prevent enforcement.
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" 2>/dev/null >> "\$LOG_FILE" || true
}

# 权限检查
if [ "\$(id -u)" -ne 0 ]; then
    echo "错误：需要 root 权限"
    exit 1
fi

stop_for_limit() {
    reason="\$1"
    # Enforcement must not be skipped just because the disk is full, read-only,
    # or logging/state persistence fails.
    set +e
    state_status="未能写入本月锁定标记"
    tmp_state="\$STATE_DIR/.traffic-limit.XXXXXX"
    tmp_state=\$(mktemp "\$tmp_state")
    if [ -n "\$tmp_state" ]; then
        if printf '%s\n' "\$(date -Is) \$reason" > "\$tmp_state" &&
           chmod 0600 "\$tmp_state" &&
           mv -f "\$tmp_state" "\$TRIGGER_FILE"; then
            state_status="已写入本月锁定标记"
        fi
    fi
    log "\$reason；\$state_status，正在关机。"
    sync

    # Stop traffic immediately, then ask the init system to power off.
    ip link set dev "\$INTERFACE" down
    systemctl poweroff && exit 0
    shutdown -h now && exit 0

    log "错误：关机命令失败；已尝试关闭主网卡。"
    exit 1
}

if ! install -d -o root -g root -m 0700 "\$STATE_DIR"; then
    stop_for_limit "无法准备持久状态目录，为避免失控按失败关闭处理"
fi
if ! exec 9>/run/gcp-free-traffic-monitor.lock; then
    stop_for_limit "无法创建流量检查锁，为避免失控按失败关闭处理"
fi
# Exit code 1 is the documented nonblocking lock-conflict result. Any other
# flock failure is unsafe and must fail closed.
if flock -n 9; then
    :
else
    lock_status=\$?
    if [ "\$lock_status" -eq 1 ]; then
        exit 0
    fi
    stop_for_limit "流量检查锁异常，为避免失控按失败关闭处理"
fi

if [ -f "\$TRIGGER_FILE" ]; then
    stop_for_limit "本月流量阈值此前已触发，拒绝同月重新开机继续传输"
fi

# 获取流量数据。vnStat oneline 固定为 15 个字段，第 10 个字段为本月 TX。
# 开机时 vnstat 可能尚未就绪，先短暂重试，再按失败关闭处理。
VNSTAT_RAW=""
for attempt in 1 2 3; do
    if VNSTAT_RAW=\$(vnstat -i "\$INTERFACE" --oneline b 2>/dev/null); then
        break
    fi
    sleep 10
done
if [ -z "\$VNSTAT_RAW" ]; then
    stop_for_limit "无法读取 vnStat 月度出站计数，为避免失控按失败关闭处理"
fi
TX_BYTES=\$(printf '%s\n' "\$VNSTAT_RAW" | awk -F ';' 'NF == 15 { print \$10 }')
case "\$TX_BYTES" in
    ''|*[!0-9]*)
        stop_for_limit "vnStat 返回了无效的月度出站计数，为避免失控按失败关闭处理"
        ;;
esac

decimal_ge() {
    left="\$1"
    right="\$2"
    while [[ \${#left} -gt 1 && \$left == 0* ]]; do left=\${left#0}; done
    while [[ \${#right} -gt 1 && \$right == 0* ]]; do right=\${right#0}; done
    if [[ \${#left} -ne \${#right} ]]; then
        [[ \${#left} -gt \${#right} ]]
        return
    fi
    [[ \$left == \$right || \$left > \$right ]]
}

TX_GIB=\$(awk -v bytes="\$TX_BYTES" 'BEGIN { printf "%.2f", bytes / 1073741824 }')

# ==========================================
# 1. 终端直接输出 (显示精确数值)
# ==========================================
echo "========================================"
echo " 网卡接口    : \$INTERFACE"
echo " 当前时间    : \$(date '+%Y-%m-%d %H:%M:%S')"
echo " 精确出站(TX): \$TX_BYTES Bytes"
echo " 换算出站(TX): \$TX_GIB GiB"
echo " 触发阈值    : \$LIMIT GiB"
echo "========================================"

# ==========================================
# 2. 检查与执行策略
# ==========================================

log "当前出站流量: \$TX_GIB GiB (触发阈值: \$LIMIT GiB)"

# 检查是否超限，不用小数或 bc，避免边界舍入。
if decimal_ge "\$TX_BYTES" "\$LIMIT_BYTES"; then
    echo "状态: [警告] 流量已达到阈值，正在锁定本月状态并关机..."
    stop_for_limit "月度出站流量已达到 \$LIMIT GiB"
else
    echo "状态: [正常] 流量未超限。"
    log "流量正常。"
fi
EOF

# 6. 赋予执行权限
chown root:root "$CHECK_SCRIPT_TMP"
chmod 0700 "$CHECK_SCRIPT_TMP"
mv -f "$CHECK_SCRIPT_TMP" /root/gcp_free_check_traffic.sh
trap - EXIT

# 7. 设置定时任务
echo "--> 更新 Crontab 定时任务..."
CRON_BK="$(mktemp /tmp/gcp-free-cron.XXXXXX)"
trap 'rm -f "$CRON_BK"' EXIT
crontab -l > "$CRON_BK" 2>/dev/null || true

# 清理本工具任务，并迁移上游脚本生成的两个精确 cron 命令。不会按文件名模糊删除。
sed -i '/# gcp-free-audited$/d' "$CRON_BK"
sed -i '\|^\*/5 \* \* \* \* /root/check_traffic\.sh$|d' "$CRON_BK"
sed -i '\|^0 0 1 \* \* /root/reset_network\.sh$|d' "$CRON_BK"

# If the retired audited INPUT-only mode had already activated, remove only its
# ownership-marked chain. Never touch an unowned same-named chain.
LIMIT_CHAIN="GCP_FREE_TRAFFIC_LIMIT"
OWNER_MARK="gcp-free-audited:traffic-limit-v1"
if iptables -L "$LIMIT_CHAIN" >/dev/null 2>&1 && \
   iptables -C "$LIMIT_CHAIN" -m comment --comment "$OWNER_MARK" -j DROP >/dev/null 2>&1; then
    while iptables -C INPUT -j "$LIMIT_CHAIN" >/dev/null 2>&1; do
        iptables -D INPUT -j "$LIMIT_CHAIN"
    done
    iptables -F "$LIMIT_CHAIN"
    iptables -X "$LIMIT_CHAIN"
fi

# 添加新任务
# 每分钟检查，并在每次开机后立即检查；脚本内 flock 防止并发。
echo "* * * * * /root/gcp_free_check_traffic.sh # gcp-free-audited" >> "$CRON_BK"
echo "@reboot /root/gcp_free_check_traffic.sh # gcp-free-audited" >> "$CRON_BK"

crontab "$CRON_BK"

echo "=========================================="
echo " 安装完成！"
echo "=========================================="
echo "当前策略："
echo "1. 每分钟及每次开机时检测本月出站流量 (TX)。"
echo "2. 流量 >= $LIMIT GiB 时："
echo "   - 保留 vnStat 数据并写入本月锁定标记"
echo "   - 立即关机；同月重开会再次关机"
echo "3. 这是 VM 内本地触发器，不是 GCP 账单硬上限，统计与执行均可能延迟。"
echo "=========================================="
