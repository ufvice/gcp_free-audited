# GCP Free 工具集（审计加固版）

本仓库是 `fatekey/gcp_free` 的私有历史副本，安全基线固定为上游提交
`f09a7316510494c59852a638a6a85af1e3fddc99`。它不会在运行时从上游
`master` 下载 shell 脚本。

## 安全加固

- 远程操作只把当前 checkout 中的 `scripts/*.sh` 通过 SSH 标准输入送入 root 私有临时文件，核对 SHA-256 后执行；不留下普通用户可替换的 `/tmp` 脚本。
- `config.dae` 默认严格校验 TLS 证书（`allow_insecure: false`）。
- 删除“向整个 VPC 开放所有入站端口”的功能。
- 新实例不再附加公开 HTTP/HTTPS 标签；防火墙使用实例专属标签和规则名，
  并在创建实例之前通过“指定来源允许 + 其他来源拒绝”的优先级规则把 SSH 限制到用户输入的 IP/CIDR；
  规则创建失败时不会继续创建实例。
- 配置防火墙时会识别并删除内容完全匹配上游实现的 `allow-all-ingress-custom`；同名但内容不同的规则不会被误删。
- 流量限制使用独立 iptables 链，不再清空系统、Docker 或用户已有规则。
  安装时会精确移除旧版生成的两个无标记 cron 命令，避免旧脚本继续运行。
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
- 远程安装流量监控脚本（iptables 监控 / 超额自动关机）
## 快速开始（推荐）

打开 https://console.cloud.google.com/
在右上角点击 Cloud Shell 
在 Cloud Shell 服务器运行
```bash
# 初次运行（私有仓库会要求 GitHub 身份验证）
git clone https://github.com/ufvice/gcp_free-audited.git && cd gcp_free-audited && bash start.sh
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
- `scripts/net_iptables.sh`: 流量监控（iptables）
- `scripts/net_shutdown.sh`: 超额自动关机

## 常见问题

- 如果 `start.sh` 报错提示未找到 venv，可删除 `.gcp_free_initialized` 后重新初始化。
