# GCP Free 工具集（审计加固版）

本仓库是 `fatekey/gcp_free` 的公开历史副本，安全基线固定为上游提交
`f09a7316510494c59852a638a6a85af1e3fddc99`。它不会在运行时从上游
`master` 下载 shell 脚本。

## 安全加固

- 远程操作只把当前 checkout 中的 `scripts/*.sh` 通过 SSH 标准输入送入 root 私有临时文件，核对 SHA-256 后执行；不留下普通用户可替换的 `/tmp` 脚本。
- `config.dae` 默认严格校验 TLS 证书（`allow_insecure: false`）。
- 删除“向整个 VPC 开放所有入站端口”的功能。
- 新实例不再附加公开 HTTP/HTTPS 标签；防火墙使用实例专属标签和规则名，
  并在创建实例之前通过“指定来源允许 + 其他来源拒绝”的优先级规则限制 SSH；
  默认仅允许 Google IAP TCP 转发网段，也支持逗号分隔的多个自定义 IPv4/CIDR；
  规则创建失败时不会继续创建实例。
- 配置防火墙时会识别并删除内容完全匹配上游实现的 `allow-all-ingress-custom`；同名但内容不同的规则不会被误删。
- 保留的旧版入站限制只使用独立 iptables 链，不再清空系统、Docker 或用户已有规则；
  新版关机保护安装时会精确移除旧 cron 和本工具拥有的旧链，避免两套策略同时运行。
- 菜单中的主网卡出站流量保护默认以 `100 GiB` 为触发阈值（可输入 `1-199`），
  每分钟及开机时检查；达到后保留计数、记录本月锁定标记并自动关机。
- `scripts/dae.sh` 固定安装 dae `v1.0.0`，程序与 GeoIP 文件都使用仓库内置的可信 SHA-256 校验；不使用 latest、CDN 或 Worker 镜像。
- Python 依赖固定在 `requirements.lock`，安装时强制核对包哈希。

> 注意：依赖固定并不等于永久安全。更新任何脚本、dae 版本、GeoIP 或 Python 依赖前，
> 都应重新审计并通过正常 commit 合入；不要恢复跟随分支或 latest 的运行时下载。

这是一个用于管理 GCP 免费实例的脚本集合，提供创建实例、刷 AMD CPU、配置防火墙、换源、安装 dae，以及远程安装流量监控脚本等功能。

创建免费实例需要绑定结算账号，也就是说目前应该处于试用赠金或者付费账号状态。

## 功能概览

- 创建/选择 GCP 免费实例
- 刷 AMD CPU
- 配置防火墙规则
- 换源、安装 dae、上传 `config.dae`
- 远程安装主网卡出站流量保护（默认 100 GiB，超额自动关机）
## 快速开始（推荐）

打开 https://console.cloud.google.com/
在右上角点击 Cloud Shell 
在 Cloud Shell 服务器运行
```bash
# 初次运行；固定到已审计标签，不跟随 main 后续变化
git clone --branch audited-f09a731-v4 --depth 1 https://github.com/ufvice/gcp_free-audited.git
cd gcp_free-audited
bash start.sh
# 再次运行
cd ~/gcp_free-audited && bash start.sh
```

## 环境要求

- 已安装 Google Cloud SDK（`gcloud`）
- 已登录并具备对应项目权限（建议先 `gcloud auth login`）
- Python 3

## 本地运行

### 环境要求

- 已安装 Google Cloud SDK（`gcloud`）
- 已登录并具备对应项目权限（建议先 `gcloud auth application-default login`）
- Python 3
### 运行脚本

使用 `start.sh` 自动初始化环境：

```bash
bash start.sh
```

首次运行会：

1. 启用所需 GCP API
2. 创建并进入 venv
3. 安装依赖
4. 执行 `gcp.py`

再次运行只会进入 venv 并执行 `gcp.py`。

## 手动运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
python gcp.py
```

## 脚本说明

- `gcp.py`: 主控制脚本
- `config.dae`: dae 配置模板
- `scripts/apt.sh`: 换源脚本
- `scripts/dae.sh`: 安装 dae
- `scripts/net_iptables.sh`: 旧的“限制业务入站”模式；它不是出站账单保护，菜单不再提供
- `scripts/net_shutdown.sh`: 主网卡出站达到阈值后锁定当月状态并自动关机

## 推荐部署流程（100 GiB 主网卡出站保护）

1. 在 GCP 控制台创建或选择已绑定结算账号的项目，并设置 Cloud Billing Budget 告警。
   Budget 只是延迟告警，不是消费硬上限。
2. 打开 Cloud Shell，先执行 `gcloud config set project 你的项目ID`，再按上面的固定标签命令克隆并运行工具。
3. 选择 `[1] 新建免费实例`，区域选择 Oregon/Iowa/South Carolina 之一，系统推荐 Debian 12。
4. SSH 接入选择 `[1] Google IAP`。工具会启用 IAP API、只放行 Google IAP TCP
   转发网段 `35.235.240.0/20`，并让后续 `gcloud compute ssh` 自动使用
   `--tunnel-through-iap`；Cloud Shell 和 PC 无需分别维护公网 IP。
5. 实例创建后选择 `[2] 选择服务器`，再选择 `[8] 安装流量监控脚本`。
6. 选择“达到阈值后自动关机”，阈值直接回车即为 `100 GiB`。
7. 不需要 CDN 分流时，不执行 `[6] 安装 dae` 和 `[7] 上传 config.dae`；配置防火墙时也不要启用 CDN IP 拒绝规则。
8. 登录 VM 后可执行 `sudo vnstat -i "$(ip route | awk '/default/ {print $5; exit}')" -m`
   查看月度计数；不要把强制检查脚本当作只读命令，它在达到阈值时会立即断网关机。

如果不能使用 IAP，可选择“自定义一个或多个 IPv4/CIDR”，例如：

```text
34.81.192.51/32,203.0.113.8/32,198.51.100.0/24
```

工具会逐项规范化、去重，并拒绝 IPv6、无效输入和 `0.0.0.0/0`。

> 该保护是 VM 内基于 vnStat 的本地触发器，不是 Google Cloud 的账单硬上限。
> 监控、vnStat 和关机均可能有延迟；root 入侵者也能停用它。请同时保持最小入站端口、
> 使用预算告警，并将支付方式风险控制在你可以接受的范围内。
> 当前实现支持本工具创建的单主网卡 GCP 实例；多网卡或默认路由会变化的机器不在支持范围内。
> 全新 vnStat 数据库需要数分钟才会产生首批月度数据。v4 会在安装后的 15 分钟内仅对
> `Not enough data available yet.` 这一明确初始化状态等待重试；窗口结束后仍失败则关机。

## 常见问题

- 如果 `start.sh` 报错提示未找到 venv，可删除 `.gcp_free_initialized` 后重新初始化。
