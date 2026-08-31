#!/bin/bash
set -eu
umask 077

# ==========================================
# 流量监控自动部署脚本
# 功能：
# 1. 自动获取网卡，只监控出站流量 (TX)
# 2. 运行 check_traffic.sh 时终端显示精确流量，日志保留简略信息
# 3. 每月重置流量并删除旧的监控日志
# ==========================================

# 1. 检查 Root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "错误：请使用 root 权限运行此脚本。"
    exit 1
fi

LIMIT="${GCP_FREE_TRAFFIC_LIMIT_GIB:-100}"
case "$LIMIT" in
    ''|*[!0-9]*) echo "错误：流量阈值必须是整数 GiB。"; exit 1 ;;
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
apt-get install vnstat bc -y

# 4. 配置并启动 vnStat
echo "--> 配置 vnStat..."
# 尝试添加接口
if ! vnstat --add -i "$INTERFACE" 2>/dev/null; then
    echo "    (接口可能已存在，跳过添加)"
fi

systemctl enable vnstat
systemctl restart vnstat

# 等待服务启动并生成初始数据库
sleep 5
vnstat -i "$INTERFACE" > /dev/null 2>&1

# 5. 生成本工具专属监控脚本
echo "--> 生成监控脚本 /root/gcp_free_check_traffic.sh..."
cat > /root/gcp_free_check_traffic.sh <<EOF
#!/bin/bash
set -eu
umask 077

# 强制使用标准区域设置
export LC_ALL=C

# 配置
LOG_FILE="/var/log/traffic_monitor.log"
INTERFACE="$INTERFACE"
LIMIT=$LIMIT

# 日志记录函数 (保持原格式)
log() {
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" >> "\$LOG_FILE"
}

# 权限检查
if [ "\$(id -u)" -ne 0 ]; then
    echo "错误：需要 root 权限"
    exit 1
fi

# 获取流量数据 (强制使用 'b' 参数获取字节单位)
# vnStat oneline: 第 10 个字段为本月 TX 字节数。
VNSTAT_RAW=\$(vnstat -i "\$INTERFACE" --oneline b 2>/dev/null)

# 提取本月出站流量 (TX)
TX_BYTES=\$(echo "\$VNSTAT_RAW" | cut -d ';' -f 10)

# 如果获取失败或为空，默认为 0
if [[ -z "\$TX_BYTES" ]]; then
    TX_BYTES=0
fi

# 将字节转换为 GB (1 GB = 1073741824 Bytes)
TX_GB=\$(echo "scale=2; \$TX_BYTES / 1073741824" | bc)

# ==========================================
# 1. 终端直接输出 (显示精确数值)
# ==========================================
echo "========================================"
echo " 网卡接口    : \$INTERFACE"
echo " 当前时间    : \$(date '+%Y-%m-%d %H:%M:%S')"
echo " 精确出站(TX): \$TX_BYTES Bytes"
echo " 换算出站(TX): \$TX_GB GB"
echo " 流量上限    : \$LIMIT GB"
echo "========================================"

# ==========================================
# 2. 日志记录与限制逻辑 (保持简洁)
# ==========================================

log "当前出站流量: \$TX_GB GB (限制: \$LIMIT GB)"

# 检查是否超限
if [ "\$(echo "\$TX_GB >= \$LIMIT" | bc)" -eq 1 ]; then
    echo "状态: [警告] 流量已超限，正在应用防火墙规则..."
    log "警告：流量超出限制！正在执行封禁策略..."
    
    # 使用专用链实施限制，不清空系统、Docker 或用户已有的规则。
    LIMIT_CHAIN="GCP_FREE_TRAFFIC_LIMIT"
    OWNER_MARK="gcp-free-audited:traffic-limit-v1"
    apply_limit() {
        if iptables -L "\$LIMIT_CHAIN" >/dev/null 2>&1; then
            # Refuse to touch a same-named chain unless our ownership marker exists.
            iptables -C "\$LIMIT_CHAIN" -m comment --comment "\$OWNER_MARK" -j DROP 2>/dev/null || return 1
            iptables -F "\$LIMIT_CHAIN" || return 1
        else
            iptables -N "\$LIMIT_CHAIN" || return 1
        fi
        iptables -A "\$LIMIT_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT || return 1
        iptables -A "\$LIMIT_CHAIN" -i lo -j ACCEPT || return 1
        iptables -A "\$LIMIT_CHAIN" -p tcp --dport 22 -j ACCEPT || return 1
        iptables -A "\$LIMIT_CHAIN" -m comment --comment "\$OWNER_MARK" -j DROP || return 1
        iptables -C INPUT -j "\$LIMIT_CHAIN" 2>/dev/null || iptables -I INPUT 1 -j "\$LIMIT_CHAIN"
    }
    if apply_limit; then
        log "网络已限制 (仅保留 SSH；仅处理 IPv4)。"
    else
        log "错误：流量限制规则未完整应用。"
        exit 1
    fi
else
    echo "状态: [正常] 流量未超限。"
    log "流量正常。"
fi
EOF

# 6. 生成本工具专属重置脚本
echo "--> 生成重置脚本 /root/gcp_free_reset_network.sh..."
cat > /root/gcp_free_reset_network.sh <<EOF
#!/bin/bash
set -eu
umask 077

RESET_LOG="/var/log/network_reset.log"
TRAFFIC_LOG="/var/log/traffic_monitor.log"
INTERFACE="$INTERFACE"

log() {
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" >> "\$RESET_LOG"
}

log "开始执行每月网络重置..."

# 1. 删除旧的流量监控日志 (新增功能)
if [ -f "\$TRAFFIC_LOG" ]; then
    rm -f "\$TRAFFIC_LOG"
    log "已删除旧的流量监控日志: \$TRAFFIC_LOG"
else
    log "流量监控日志不存在，无需删除。"
fi

# 2. 只移除本工具创建的专用链，保留系统、Docker 和用户规则
LIMIT_CHAIN="GCP_FREE_TRAFFIC_LIMIT"
OWNER_MARK="gcp-free-audited:traffic-limit-v1"
while iptables -C INPUT -j "\$LIMIT_CHAIN" 2>/dev/null; do
    if ! iptables -C "\$LIMIT_CHAIN" -m comment --comment "\$OWNER_MARK" -j DROP 2>/dev/null; then
        log "错误：发现同名但不属于本工具的链，拒绝修改。"
        exit 1
    fi
    if ! iptables -D INPUT -j "\$LIMIT_CHAIN"; then
        log "错误：无法移除 INPUT 中的流量限制跳转。"
        exit 1
    fi
done
if iptables -L "\$LIMIT_CHAIN" >/dev/null 2>&1; then
    if ! iptables -C "\$LIMIT_CHAIN" -m comment --comment "\$OWNER_MARK" -j DROP 2>/dev/null; then
        log "错误：发现同名但不属于本工具的链，拒绝删除。"
        exit 1
    fi
    iptables -F "\$LIMIT_CHAIN"
    iptables -X "\$LIMIT_CHAIN"
fi
log "流量限制专用链已移除，其他防火墙规则保持不变。"

# 3. 重置 vnStat 数据库
systemctl stop vnstat
vnstat --remove --force -i "\$INTERFACE"
vnstat --add -i "\$INTERFACE"
systemctl start vnstat

# 强制刷新一次数据以确保数据库建立
sleep 3
vnstat -i "\$INTERFACE" > /dev/null 2>&1

log "vnStat 数据库已重置 (接口: \$INTERFACE)。"
EOF

# 7. 赋予执行权限
chmod +x /root/gcp_free_check_traffic.sh
chmod +x /root/gcp_free_reset_network.sh

# 8. 设置定时任务
echo "--> 更新 Crontab 定时任务..."
CRON_BK="$(mktemp /tmp/gcp-free-cron.XXXXXX)"
trap 'rm -f "$CRON_BK"' EXIT
crontab -l > "$CRON_BK" 2>/dev/null || true

# 清理本工具任务，并迁移上游脚本生成的两个精确 cron 命令。不会按文件名模糊删除。
sed -i '/# gcp-free-audited$/d' "$CRON_BK"
sed -i '\|^\*/5 \* \* \* \* /root/check_traffic\.sh$|d' "$CRON_BK"
sed -i '\|^0 0 1 \* \* /root/reset_network\.sh$|d' "$CRON_BK"

# 添加新任务
# 每5分钟检查一次流量
echo "*/5 * * * * /root/gcp_free_check_traffic.sh # gcp-free-audited" >> "$CRON_BK"
# 每月1号 00:00 重置网络和日志
echo "0 0 1 * * /root/gcp_free_reset_network.sh # gcp-free-audited" >> "$CRON_BK"

crontab "$CRON_BK"

echo "=========================================="
echo " 安装完成！"
echo "=========================================="
echo "您可以手动运行以下命令查看精确流量："
echo "  bash /root/gcp_free_check_traffic.sh"
echo ""
echo "监控日志位置："
echo "  /var/log/traffic_monitor.log"
echo "=========================================="
