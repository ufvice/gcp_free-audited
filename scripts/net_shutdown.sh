#!/bin/bash
set -eu
umask 077

# ==========================================
# 流量监控自动部署脚本 (关机版)
# 功能：
# 1. 自动获取网卡，只监控出站流量 (TX)
# 2. 流量超标后：重置流量统计 + 删除日志 + 立即关机
# 3. 去除独立的每月重置脚本
# ==========================================

# 1. 检查 Root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "错误：请使用 root 权限运行此脚本。"
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
LIMIT=180

# 日志记录函数
log() {
    # 如果日志文件不存在（已被删除），则不记录或重新创建，这里选择追加
    echo "\$(date '+%Y-%m-%d %H:%M:%S') - \$1" >> "\$LOG_FILE"
}

# 权限检查
if [ "\$(id -u)" -ne 0 ]; then
    echo "错误：需要 root 权限"
    exit 1
fi

# 获取流量数据 (强制使用 'b' 参数获取字节单位)
VNSTAT_RAW=\$(vnstat -i "\$INTERFACE" --oneline b 2>/dev/null)

# vnStat oneline 第 9 个字段为本月出站流量 (TX)
TX_BYTES=\$(echo "\$VNSTAT_RAW" | cut -d ';' -f 9)

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
# 2. 检查与执行策略
# ==========================================

log "当前出站流量: \$TX_GB GB (限制: \$LIMIT GB)"

# 检查是否超限 (TX_GB >= LIMIT)
if [ "\$(echo "\$TX_GB >= \$LIMIT" | bc)" -eq 1 ]; then
    echo "状态: [警告] 流量已超限！正在重置数据并关机..."
    log "警告：流量超出限制！执行重置并关机。"
    
    # --- 1. 重置 vnStat 流量统计 ---
    systemctl stop vnstat
    vnstat --remove --force -i "\$INTERFACE"
    vnstat --add -i "\$INTERFACE"
    systemctl start vnstat
    
    # --- 2. 删除监控日志 ---
    if [ -f "\$LOG_FILE" ]; then
        rm -f "\$LOG_FILE"
    fi
    
    # --- 3. 关机 ---
    # 稍微延迟1秒确保命令执行完毕
    sleep 1
    shutdown -h now
else
    echo "状态: [正常] 流量未超限。"
    log "流量正常。"
fi
EOF

# 6. 赋予执行权限
chmod +x /root/gcp_free_check_traffic.sh

# 7. 设置定时任务
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

crontab "$CRON_BK"

echo "=========================================="
echo " 安装完成！"
echo "=========================================="
echo "当前策略："
echo "1. 每 5 分钟检测一次出站流量 (TX)。"
echo "2. 流量 >= 180 GB 时："
echo "   - 重置流量统计 (归零)"
echo "   - 删除日志文件"
echo "   - 立即关机 (Shutdown)"
echo "=========================================="
