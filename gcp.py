import getpass
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

try:
    from google.cloud import compute_v1
    from google.cloud import resourcemanager_v3
except ImportError:
    print("【错误】缺少必要的 Python 库。")
    print("请先在终端运行以下命令安装：")
    print("python -m pip install --require-hashes -r requirements.lock")
    sys.exit(1)

AUDITED_UPSTREAM_COMMIT = "f09a7316510494c59852a638a6a85af1e3fddc99"
LOCAL_SCRIPT_PATHS = {
    "apt": "scripts/apt.sh",
    "dae": "scripts/dae.sh",
    "net_iptables": "scripts/net_iptables.sh",
    "net_shutdown": "scripts/net_shutdown.sh",
}
MANAGED_NETWORK_TAG_PREFIX = "gcp-free-"
IAP_TCP_FORWARDING_CIDR = "35.235.240.0/20"
MAX_SSH_SOURCE_RANGES = 256
DEFAULT_TRAFFIC_LIMIT_GIB = 100
MAX_TRAFFIC_LIMIT_GIB = 199

REGION_OPTIONS = [
    {"name": "俄勒冈 (Oregon) [推荐]", "region": "us-west1", "default_zone": "us-west1-b"},
    {"name": "爱荷华 (Iowa)", "region": "us-central1", "default_zone": "us-central1-f"},
    {"name": "南卡罗来纳 (South Carolina)", "region": "us-east1", "default_zone": "us-east1-b"},
]

OS_IMAGE_OPTIONS = [
    {"name": "Debian 12 (Bookworm)", "project": "debian-cloud", "family": "debian-12"},
    {"name": "Ubuntu 22.04 LTS", "project": "ubuntu-os-cloud", "family": "ubuntu-2204-lts"},
]


def print_info(msg):
    print(f"[信息] {msg}")
    sys.stdout.flush()


def print_success(msg):
    print(f"\033[92m[成功] {msg}\033[0m")
    sys.stdout.flush()


def print_warning(msg):
    print(f"\033[93m[警告] {msg}\033[0m")
    sys.stdout.flush()


def select_from_list(items, prompt_text, label_fn):
    print(f"\n--- {prompt_text} ---")
    for i, item in enumerate(items):
        print(f"[{i+1}] {label_fn(item)}")
    while True:
        choice = input(f"请输入数字选择 (1-{len(items)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
        print("输入无效，请重试。")


def prompt_manual_project_id():
    while True:
        project_id = input("请输入项目 ID: ").strip()
        if project_id:
            return project_id
        print("输入不能为空，请重试。")


def select_gcp_project():
    print_info("正在扫描您的项目列表...")
    try:
        client = resourcemanager_v3.ProjectsClient()
        request = resourcemanager_v3.SearchProjectsRequest(query="")
        page_result = client.search_projects(request=request)

        active_projects = []
        for project in page_result:
            if project.state == resourcemanager_v3.Project.State.ACTIVE:
                active_projects.append(project)

        if not active_projects:
            print_warning("未找到活跃的项目。请手动输入项目 ID。")
            return prompt_manual_project_id()

        print("\n--- 请选择目标项目 ---")
        for i, p in enumerate(active_projects):
            print(f"[{i+1}] {p.project_id} ({p.display_name})")

        while True:
            choice = input(f"请输入数字选择 (1-{len(active_projects)}): ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(active_projects):
                    selected = active_projects[idx]
                    print_info(f"已选择项目: {selected.project_id} ({selected.display_name})")
                    return selected.project_id
            print("输入无效，请重试。")
    except Exception as e:
        print_warning(f"无法列出项目: {e}。请手动输入项目 ID。")
        return prompt_manual_project_id()


def list_zones_for_region(project_id, region):
    zones_client = compute_v1.ZonesClient()
    zones = []
    for zone in zones_client.list(project=project_id):
        if zone.status != "UP":
            continue
        zone_region = zone.region.split("/")[-1] if zone.region else ""
        if zone_region == region:
            zones.append(zone.name)
    return sorted(zones)


def select_zone(project_id):
    region_config = select_from_list(REGION_OPTIONS, "请选择部署区域", lambda r: r["name"])
    region = region_config["region"]
    default_zone = region_config["default_zone"]

    print_info(f"正在获取 {region} 的可用区列表...")
    try:
        zones = list_zones_for_region(project_id, region)
    except Exception as e:
        print_warning(f"获取可用区失败: {e}。将使用默认可用区 {default_zone}。")
        return default_zone

    if not zones:
        print_warning(f"未获取到可用区列表，使用默认可用区 {default_zone}。")
        return default_zone

    return select_from_list(zones, f"请选择可用区 ({region})", lambda z: z)


def select_os_image():
    return select_from_list(OS_IMAGE_OPTIONS, "请选择操作系统", lambda o: o["name"])


def create_instance(project_id, zone, os_config, instance_name="free-tier-vm"):
    instance_client = compute_v1.InstancesClient()
    images_client = compute_v1.ImagesClient()

    print(f"\n[开始] 正在 {project_id} 项目中准备资源...")
    print(f"可用区: {zone}")
    print(f"系统: {os_config['name']}")

    try:
        image_response = images_client.get_from_family(
            project=os_config["project"],
            family=os_config["family"],
        )
        source_disk_image = image_response.self_link

        disk = compute_v1.AttachedDisk()
        disk.boot = True
        disk.auto_delete = True
        initialize_params = compute_v1.AttachedDiskInitializeParams()
        initialize_params.source_image = source_disk_image
        initialize_params.disk_size_gb = 30
        initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-standard"
        disk.initialize_params = initialize_params

        network_interface = compute_v1.NetworkInterface()
        network_interface.name = "global/networks/default"

        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
        access_config.network_tier = compute_v1.AccessConfig.NetworkTier.STANDARD.name
        network_interface.access_configs = [access_config]

        instance = compute_v1.Instance()
        instance.name = instance_name
        instance.machine_type = f"zones/{zone}/machineTypes/e2-micro"
        instance.disks = [disk]
        instance.network_interfaces = [network_interface]

        tags = compute_v1.Tags()
        # Do not opt into the default VPC's public HTTP/HTTPS rules.  This
        # dedicated tag scopes firewall rules created by this tool.
        tags.items = [managed_network_tag(instance_name)]
        instance.tags = tags

        print("配置组装完成，正在向 Google Cloud 发送创建请求...")
        operation = instance_client.insert(
            project=project_id,
            zone=zone,
            instance_resource=instance,
        )

        print("请求已发送，正在等待操作完成... (约 30-60 秒)")
        operation_client = compute_v1.ZoneOperationsClient()
        operation = operation_client.wait(
            project=project_id,
            zone=zone,
            operation=operation.name,
        )

        if operation.error:
            print("创建失败:", operation.error)
            return False
        else:
            print_success(f"实例 '{instance_name}' 已创建！")
            try:
                inst_info = instance_client.get(project=project_id, zone=zone, instance=instance_name)
                ip = inst_info.network_interfaces[0].access_configs[0].nat_i_p
                print(f"外部 IP 地址: {ip}")
            except Exception:
                pass
            print("请前往 GCP 控制台查看详情。")
            return True

    except Exception as e:
        print(f"\n[失败] 操作中止: {e}")
        traceback.print_exc()
        return False


def list_instances(project_id):
    instance_client = compute_v1.InstancesClient()
    request = compute_v1.AggregatedListInstancesRequest(project=project_id)

    print_info(f"正在扫描项目 {project_id} 中的实例...")

    instances = []
    for zone_path, response in instance_client.aggregated_list(request=request):
        if not response.instances:
            continue
        zone_short = zone_path.split("/")[-1]
        for instance in response.instances:
            network = None
            internal_ip = "-"
            external_ip = "-"
            if instance.network_interfaces:
                network = instance.network_interfaces[0].network
                internal_ip = instance.network_interfaces[0].network_i_p
                access_configs = instance.network_interfaces[0].access_configs
                if access_configs:
                    external_ip = access_configs[0].nat_i_p or "-"
            instances.append(
                {
                    "name": instance.name,
                    "zone": zone_short,
                    "status": instance.status,
                    "cpu_platform": instance.cpu_platform or "Unknown CPU Platform",
                    "network": network or "global/networks/default",
                    "internal_ip": internal_ip,
                    "external_ip": external_ip,
                    "tags": list(instance.tags.items) if instance.tags and instance.tags.items else [],
                }
            )
    return instances


def select_instance(project_id):
    instances = list_instances(project_id)
    if not instances:
        print_warning("该项目中没有任何实例！")
        return None

    print("\n--- 请选择目标服务器 ---")
    for i, inst in enumerate(instances):
        status_color = "\033[92m" if inst["status"] == "RUNNING" else "\033[91m"
        network_short = inst["network"].split("/")[-1] if inst["network"] else "-"
        print(
            f"[{i+1}] {inst['name']:<20} | 区域: {inst['zone']:<15} | 状态: "
            f"{status_color}{inst['status']}\033[0m | 网络: {network_short} | 内网IP: "
            f"{inst['internal_ip']} | 外网IP: {inst['external_ip']} | CPU: {inst['cpu_platform']}"
        )

    while True:
        choice = input(f"请输入数字选择 (1-{len(instances)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(instances):
                return instances[idx]
        print("输入无效，请重试。")


def wait_for_operation(project_id, zone, operation_name):
    operation_client = compute_v1.ZoneOperationsClient()
    completed = operation_client.wait(project=project_id, zone=zone, operation=operation_name)
    if getattr(completed, "error", None):
        raise RuntimeError(f"GCP zonal operation failed: {completed.error}")
    return completed


def reroll_cpu_loop(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]

    instance_client = compute_v1.InstancesClient()
    attempt_counter = 1

    print_info(f"目标实例: {instance_name} ({zone})")
    print_info("目标: 只要 CPU 包含 'AMD' 即停止。")

    while True:
        print("\n" + "=" * 50)
        print_info(f"第 {attempt_counter} 次尝试...")

        current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        if current_inst.status != "RUNNING":
            print_info(f"正在启动虚拟机 {instance_name}...")
            op = instance_client.start(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
            print_info("虚拟机已通电，正在等待系统初始化...")

        current_platform = "Unknown CPU Platform"
        max_retries = 60

        for i in range(max_retries):
            current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)

            if current_inst.status != "RUNNING":
                print_warning(f"检测到虚拟机状态异常变为: {current_inst.status}。跳过本次检测。")
                current_platform = "Instability Detected"
                break

            current_platform = current_inst.cpu_platform
            if current_platform and current_platform != "Unknown CPU Platform":
                break

            if (i + 1) % 5 == 0:
                print_info(f"正在等待 CPU 元数据同步... ({i+1}/{max_retries}) - 机器正在启动中")
            time.sleep(2)

        if current_platform == "Unknown CPU Platform":
            print_warning("超时：等待 2 分钟后仍无法获取 CPU 信息。")
        else:
            print_info(f"检测到 CPU: {current_platform}")

        if "AMD" in str(current_platform).upper():
            print_success(f"恭喜！已成功刷到目标 CPU: {current_platform}")
            print_info("脚本执行完毕。")
            break

        print_warning(f"结果不满意 ({current_platform})。准备重置...")
        print_info(f"正在关停虚拟机 {instance_name}...")
        op = instance_client.stop(project=project_id, zone=zone, instance=instance_name)
        wait_for_operation(project_id, zone, op.name)
        attempt_counter += 1
        time.sleep(2)


def read_cdn_ips(filename="cdnip.txt"):
    if not os.path.exists(filename):
        print(f"【错误】找不到文件: {filename}")
        print("请在脚本同目录下创建该文件，并填入IP段。")
        return []

    ip_list = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            clean_line = line.strip()
            if clean_line:
                ip = clean_line.split()[0]
                ip_list.append(ip)

    print(f"已从 {filename} 读取到 {len(ip_list)} 个 IP 段。")
    return ip_list


def set_protocol_field(config_object, value):
    try:
        # This unusual spelling is the official google-cloud-compute field.
        config_object.I_p_protocol = value
    except (AttributeError, ValueError):
        try:
            config_object.ip_protocol = value
        except (AttributeError, ValueError):
            print(f"\n【调试信息】无法设置协议字段。对象 '{type(config_object).__name__}' 的有效属性如下:")
            print([d for d in dir(config_object) if not d.startswith("_")])
            raise


def managed_network_tag(instance_name):
    safe_name = re.sub(r"[^a-z0-9-]", "-", instance_name.lower()).strip("-")
    suffix = hashlib.sha256(instance_name.encode("utf-8")).hexdigest()[:10]
    prefix_budget = 63 - len(MANAGED_NETWORK_TAG_PREFIX) - len(suffix) - 1
    safe_name = safe_name[:prefix_budget].rstrip("-") or "vm"
    return f"{MANAGED_NETWORK_TAG_PREFIX}{safe_name}-{suffix}"


def instance_firewall_rule_names(instance_info):
    identity = f"{instance_info.get('network', '')}/{instance_info['name']}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return {
        "ssh_allow": f"gcp-free-ssh-{suffix}-allow",
        "ssh_deny": f"gcp-free-ssh-{suffix}-deny",
        "cdn_deny": f"gcp-free-cdn-{suffix}-deny",
    }


def normalize_source_cidr(value):
    try:
        network = ipaddress.ip_network(value, strict=False)
        return str(network) if network.version == 4 and network.prefixlen > 0 else None
    except ValueError:
        return None


def normalize_source_cidrs(value):
    parts = [part for part in re.split(r"[\s,;]+", str(value).strip()) if part]
    if not parts or len(parts) > MAX_SSH_SOURCE_RANGES:
        return None

    normalized = []
    for part in parts:
        cidr = normalize_source_cidr(part)
        if cidr is None:
            return None
        if cidr not in normalized:
            normalized.append(cidr)
    return normalized


def enable_iap_api(project_id):
    if shutil.which("gcloud") is None:
        print_warning("未找到 gcloud，无法启用 IAP API。")
        return False
    print_info("正在确认 Identity-Aware Proxy API 已启用...")
    result = subprocess.run(
        [
            "gcloud",
            "services",
            "enable",
            "iap.googleapis.com",
            "--project",
            project_id,
            "--quiet",
        ]
    )
    if result.returncode != 0:
        print_warning("IAP API 启用失败。请检查项目权限和结算状态。")
        return False
    return True


def select_ssh_access(project_id):
    while True:
        print("\n--- SSH 接入方式 ---")
        print("[1] Google IAP（推荐；无需维护 Cloud Shell/PC 公网 IP）")
        print("[2] 自定义一个或多个 IPv4/CIDR")
        print("[0] 取消")
        choice = input("请输入数字选择（默认 1）: ").strip() or "1"

        if choice == "1":
            if enable_iap_api(project_id):
                return [IAP_TCP_FORWARDING_CIDR], {"method": "gcloud", "use_iap": True}
            return None
        if choice == "2":
            while True:
                source = input(
                    "请输入来源 IPv4/CIDR，多个用逗号分隔"
                    "（例如 203.0.113.10/32,198.51.100.0/24）: "
                ).strip()
                source_ranges = normalize_source_cidrs(source)
                if source_ranges:
                    return source_ranges, None
                print(
                    f"输入无效：仅接受 1-{MAX_SSH_SOURCE_RANGES} 个非全开放 IPv4/CIDR，"
                    "不接受 0.0.0.0/0。"
                )
        if choice == "0":
            return None
        print("输入无效，请重试。")


def insert_firewall_rule(project_id, firewall_rule):
    firewall_client = compute_v1.FirewallsClient()
    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        completed = operation_client.wait(project=project_id, operation=operation.name)
        if getattr(completed, "error", None):
            print_warning(f"防火墙规则操作失败: {completed.error}")
            return False
        print_success(f"已添加防火墙规则: {firewall_rule.name}")
        return True
    except Exception as e:
        if "already exists" in str(e):
            print_warning(f"规则 {firewall_rule.name} 已存在，拒绝把未知旧配置当作成功。")
            return False
        else:
            print(f"【失败】{e}")
            traceback.print_exc()
            return False


def upsert_firewall_rule(project_id, firewall_rule):
    """Atomically patch an existing managed rule, or insert it when absent."""
    firewall_client = compute_v1.FirewallsClient()
    try:
        firewall_client.get(project=project_id, firewall=firewall_rule.name)
    except Exception as e:
        if is_not_found_error(e):
            return insert_firewall_rule(project_id, firewall_rule)
        print_warning(f"无法检查防火墙规则 {firewall_rule.name}: {e}")
        return False

    try:
        operation = firewall_client.patch(
            project=project_id,
            firewall=firewall_rule.name,
            firewall_resource=firewall_rule,
        )
        completed = compute_v1.GlobalOperationsClient().wait(
            project=project_id,
            operation=operation.name,
        )
        if getattr(completed, "error", None):
            print_warning(f"更新防火墙规则失败: {completed.error}")
            return False
        print_success(f"已更新防火墙规则: {firewall_rule.name}")
        return True
    except Exception as e:
        print_warning(f"更新防火墙规则失败: {firewall_rule.name} ({e})")
        return False


def add_restricted_ssh_ingress(project_id, instance_info, source_ranges):
    network = instance_info.get("network") or "global/networks/default"
    target_tag = managed_network_tag(instance_info["name"])
    rule_names = instance_firewall_rule_names(instance_info)
    allow_rule = compute_v1.Firewall()
    allow_rule.name = rule_names["ssh_allow"]
    allow_rule.direction = "INGRESS"
    allow_rule.network = network
    allow_rule.priority = 100
    allow_rule.source_ranges = list(source_ranges)
    allow_rule.target_tags = [target_tag]
    allow_config = compute_v1.Allowed()
    set_protocol_field(allow_config, "tcp")
    allow_config.ports = ["22"]
    allow_rule.allowed = [allow_config]

    deny_rule = compute_v1.Firewall()
    deny_rule.name = rule_names["ssh_deny"]
    deny_rule.direction = "INGRESS"
    deny_rule.network = network
    deny_rule.priority = 200
    deny_rule.source_ranges = ["0.0.0.0/0"]
    deny_rule.target_tags = [target_tag]
    deny_config = compute_v1.Denied()
    set_protocol_field(deny_config, "tcp")
    deny_config.ports = ["22"]
    deny_rule.denied = [deny_config]

    print(f"\n正在将标签 {target_tag} 的 SSH 限制为: {', '.join(source_ranges)} ...")
    # Install/update deny first. Existing protection remains active while an
    # allow source is changed; a failed update therefore fails closed.
    if not upsert_firewall_rule(project_id, deny_rule):
        return False
    if upsert_firewall_rule(project_id, allow_rule):
        return True

    print_warning("SSH 允许规则更新失败；拒绝规则仍保持生效，可能暂时无法登录。")
    return False


def add_deny_cdn_egress(project_id, ip_ranges, instance_info):
    if not ip_ranges:
        print("IP 列表为空，跳过创建拒绝规则。")
        return False

    network = instance_info.get("network") or "global/networks/default"
    rule_name = instance_firewall_rule_names(instance_info)["cdn_deny"]

    print(f"\n正在创建出站拒绝规则: {rule_name} ...")

    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "EGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 900
    firewall_rule.destination_ranges = ip_ranges
    firewall_rule.target_tags = [managed_network_tag(instance_info["name"])]

    deny_config = compute_v1.Denied()
    set_protocol_field(deny_config, "all")
    firewall_rule.denied = [deny_config]

    if upsert_firewall_rule(project_id, firewall_rule):
        print_success(f"已添加拒绝规则，共拦截 {len(ip_ranges)} 个 IP 段。")
        return True
    return False


def configure_firewall(project_id, instance_info):
    network = instance_info.get("network") or "global/networks/default"
    print("\n------------------------------------------------")
    print("防火墙规则管理菜单")
    print("------------------------------------------------")
    print(f"目标网络: {network}")

    target_tag = managed_network_tag(instance_info["name"])
    if not remove_legacy_allow_all_rule(project_id, network):
        print_warning("无法确认旧的全开放规则已移除，已停止防火墙配置。")
        return False, None

    if target_tag not in instance_info.get("tags", []):
        print_warning(
            f"当前实例没有安全作用域标签 {target_tag}，不会创建可能影响其他实例的入站规则。"
        )
        print_warning("请为实例添加该网络标签后重新选择实例，或使用本工具新建实例。")
        return False, None
    else:
        suggested_remote = None
        choice_in = input("\n[1/2] 是否配置受限 SSH 接入? (Y/n): ").strip().lower()
        if choice_in in ("", "y", "yes"):
            ssh_access = select_ssh_access(project_id)
            if not ssh_access:
                print_warning("已取消 SSH 接入配置。")
                return False, None
            source_ranges, suggested_remote = ssh_access
            if not add_restricted_ssh_ingress(project_id, instance_info, source_ranges):
                print_warning("SSH 限制配置失败。")
                return False, None
        else:
            print("已跳过 SSH 入站规则配置。")

    choice_out = input("\n[2/2] 是否添加【拒绝对 cdnip.txt 中 IP 的出站连接】规则? (y/n): ").strip().lower()
    if choice_out == "y":
        ips = read_cdn_ips()
        if ips:
            if len(ips) > 256:
                print(f"【警告】IP 数量 ({len(ips)}) 超过 GCP 单条规则上限 (256)。")
                print("脚本将只取前 256 个 IP。")
                ips = ips[:256]

            if not add_deny_cdn_egress(project_id, ips, instance_info):
                print_warning("出站拒绝规则配置失败。")
                return False, None
    else:
        print("已跳过出站规则配置。")

    print("\n所有操作完成。")
    return True, suggested_remote


def is_not_found_error(exc):
    msg = str(exc).lower()
    return "notfound" in msg or "not found" in msg or "404" in msg


def delete_firewall_rule(project_id, rule_name):
    firewall_client = compute_v1.FirewallsClient()
    try:
        operation = firewall_client.delete(project=project_id, firewall=rule_name)
        operation_client = compute_v1.GlobalOperationsClient()
        completed = operation_client.wait(project=project_id, operation=operation.name)
        if getattr(completed, "error", None):
            print_warning(f"删除防火墙规则失败: {rule_name} ({completed.error})")
            return False
        print_success(f"已删除防火墙规则: {rule_name}")
        return True
    except Exception as e:
        if is_not_found_error(e):
            print_info(f"防火墙规则不存在，已跳过: {rule_name}")
            return True
        print_warning(f"删除防火墙规则失败: {rule_name} ({e})")
        return False


def remove_legacy_allow_all_rule(project_id, network):
    """Delete only the exact unscoped allow-all rule created by upstream."""
    rule_name = "allow-all-ingress-custom"
    firewall_client = compute_v1.FirewallsClient()
    try:
        rule = firewall_client.get(project=project_id, firewall=rule_name)
    except Exception as e:
        if is_not_found_error(e):
            return True
        print_warning(f"无法检查旧的全开放规则: {e}")
        return False

    protocols = {getattr(item, "I_p_protocol", None) for item in rule.allowed}
    same_network = str(rule.network).split("/")[-1] == str(network).split("/")[-1]
    is_exact_legacy_rule = (
        rule.direction == "INGRESS"
        and same_network
        and list(rule.source_ranges) == ["0.0.0.0/0"]
        and not rule.target_tags
        and not rule.target_service_accounts
        and rule.priority == 1000
        and not rule.disabled
        and protocols == {"all"}
    )
    if not is_exact_legacy_rule:
        print_warning(f"发现同名规则 {rule_name}，但内容不符合已知旧规则；为避免误删已停止。")
        return False

    print_warning("检测到上游遗留的 VPC 全端口开放规则，正在删除。")
    return delete_firewall_rule(project_id, rule_name)


def delete_disks_if_needed(project_id, zone, disk_names):
    if not disk_names:
        return True
    disk_client = compute_v1.DisksClient()
    all_ok = True
    for disk_name in disk_names:
        try:
            operation = disk_client.delete(project=project_id, zone=zone, disk=disk_name)
            wait_for_operation(project_id, zone, operation.name)
            print_success(f"已删除磁盘: {disk_name}")
        except Exception as e:
            if is_not_found_error(e):
                print_info(f"磁盘不存在，已跳过: {disk_name}")
            else:
                print_warning(f"删除磁盘失败: {disk_name} ({e})")
                all_ok = False
    return all_ok


def delete_free_resources(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]

    print("\n------------------------------------------------")
    print("即将删除以下资源（可以重新创建免费资源）：")
    print(f"- 实例: {instance_name} ({zone})")
    print(f"- 相关磁盘（如仍存在）")
    managed_rules = list(instance_firewall_rule_names(instance_info).values())
    print(f"- 实例专属防火墙规则: {', '.join(managed_rules)}")
    confirm = input("请输入 DELETE 确认删除: ").strip()
    if confirm != "DELETE":
        print("已取消删除操作。")
        return False

    instance_client = compute_v1.InstancesClient()
    disk_names = []
    try:
        inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        for disk in inst.disks:
            if disk.source:
                disk_names.append(disk.source.split("/")[-1])
    except Exception as e:
        print_warning(f"读取实例信息失败，磁盘清理可能不完整: {e}")

    print_info("正在删除实例...")
    try:
        operation = instance_client.delete(project=project_id, zone=zone, instance=instance_name)
        wait_for_operation(project_id, zone, operation.name)
        print_success("实例已删除。")
    except Exception as e:
        if is_not_found_error(e):
            print_info("实例不存在，已跳过删除。")
        else:
            print_warning(f"实例删除失败: {e}")
            return False

    delete_disks_if_needed(project_id, zone, disk_names)

    print_info("正在清理防火墙规则...")
    all_ok = True
    for rule_name in managed_rules:
        all_ok = delete_firewall_rule(project_id, rule_name) and all_ok

    # These legacy names were network-wide. Only remove them through explicit,
    # content-aware migration rather than blindly when deleting one instance.
    network = instance_info.get("network") or "global/networks/default"
    all_ok = remove_legacy_allow_all_rule(project_id, network) and all_ok

    if all_ok:
        print_success("清理完成。建议到控制台确认无残留资源。")
    else:
        print_warning("资源已删除，但部分防火墙清理失败；请到控制台检查。")
    return all_ok


def pick_remote_method():
    has_gcloud = shutil.which("gcloud") is not None
    has_ssh = shutil.which("ssh") is not None

    if not has_gcloud and not has_ssh:
        print_warning("本机未发现 gcloud 或 ssh，无法执行远程脚本。")
        return None

    if has_gcloud:
        print("\n--- 远程执行方式 ---")
        print("[1] gcloud 经 Google IAP 连接（推荐）")
        print("[2] gcloud 直连实例公网 IP")
        if has_ssh:
            print("[3] 系统 ssh 直连")
        choice = input("请输入数字选择（默认 1）: ").strip() or "1"
        if choice == "1":
            return {"method": "gcloud", "use_iap": True}
        if choice == "2":
            return {"method": "gcloud", "use_iap": False}
        if choice != "3":
            print_warning("远程执行方式输入无效。")
            return None

    if not has_ssh:
        print_warning("未找到 ssh 命令，无法继续。")
        return None

    default_user = getpass.getuser()
    ssh_user = input(f"请输入 SSH 用户名 (默认 {default_user}): ").strip() or default_user
    ssh_port = input("请输入 SSH 端口 (默认 22): ").strip() or "22"
    ssh_key = input("请输入 SSH 私钥路径 (留空表示使用默认密钥): ").strip()
    return {"method": "ssh", "user": ssh_user, "port": ssh_port, "key": ssh_key}


def build_remote_exec_command(project_id, instance_info, remote_config, remote_command):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        cmd = [
            "gcloud",
            "compute",
            "ssh",
            instance_name,
            "--project",
            project_id,
            "--zone",
            zone,
        ]
        if remote_config.get("use_iap"):
            cmd.append("--tunnel-through-iap")
        cmd += ["--command", remote_command]
        return cmd
    if method == "ssh":
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["ssh"]
        port = remote_config.get("port")
        if port:
            cmd += ["-p", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [f"{remote_config.get('user')}@{host}", remote_command]
        return cmd

    print_warning("远程执行方式未设置。")
    return None


def normalize_traffic_limit_gib(value):
    text = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,2}", text):
        return None
    limit = int(text)
    if limit > MAX_TRAFFIC_LIMIT_GIB:
        return None
    return limit


def run_remote_script(
    project_id,
    instance_info,
    script_key,
    remote_config,
    traffic_limit_gib=None,
):
    relative_path = LOCAL_SCRIPT_PATHS.get(script_key)
    if not relative_path:
        print_warning("未知的脚本类型，无法执行。")
        return False

    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)
    if not os.path.isfile(local_path):
        print_warning(f"找不到已审计的本地脚本: {local_path}")
        return False

    with open(local_path, "rb") as script_file:
        script_bytes = script_file.read()
    expected_sha256 = hashlib.sha256(script_bytes).hexdigest()
    environment = ""
    if traffic_limit_gib is not None:
        normalized_limit = normalize_traffic_limit_gib(traffic_limit_gib)
        if normalized_limit is None:
            print_warning(
                f"流量阈值必须是 1-{MAX_TRAFFIC_LIMIT_GIB} 之间的整数 GiB。"
            )
            return False
        environment = f"env GCP_FREE_TRAFFIC_LIMIT_GIB={normalized_limit} "

    remote_command = (
        f"sudo -- {environment}bash -c 'set -eu; umask 077; "
        "tmp=$(mktemp /root/gcp-free-script.XXXXXX); "
        "trap \"rm -f $tmp\" EXIT; cat >\"$tmp\"; "
        "actual=$(sha256sum \"$tmp\" | cut -d \" \" -f 1); "
        f"[ \"$actual\" = \"{expected_sha256}\" ] || "
        "{ echo checksum-mismatch >&2; exit 1; }; "
        "bash \"$tmp\"'"
    )
    cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not cmd:
        return False

    print_info(f"正在远程执行本地审计脚本（SHA-256: {expected_sha256}）")
    try:
        result = subprocess.run(cmd, input=script_bytes)
        if result.returncode == 0:
            print_success("远程脚本执行完成。")
            return True
        print_warning(f"远程脚本执行失败，退出码: {result.returncode}")
        return False
    except Exception as e:
        print_warning(f"远程执行失败: {e}")
        return False


def select_traffic_monitor_script():
    print("\n--- 安装主网卡出站流量保护 ---")
    print("[1] 达到阈值后自动关机（推荐）")
    print("[0] 返回")
    while True:
        choice = input("请输入数字选择: ").strip()
        if choice == "1":
            while True:
                raw_limit = input(
                    f"请输入每月触发阈值 GiB（直接回车默认 {DEFAULT_TRAFFIC_LIMIT_GIB}，"
                    f"范围 1-{MAX_TRAFFIC_LIMIT_GIB}）: "
                ).strip()
                if not raw_limit:
                    return "net_shutdown", DEFAULT_TRAFFIC_LIMIT_GIB
                limit = normalize_traffic_limit_gib(raw_limit)
                if limit is not None:
                    return "net_shutdown", limit
                print(
                    f"输入无效：请输入 1-{MAX_TRAFFIC_LIMIT_GIB} 之间的整数。"
                )
        if choice == "0":
            return None
        print("输入无效，请重试。")


def deploy_dae_config(project_id, instance_info, remote_config):
    local_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.dae")
    if not os.path.isfile(local_config):
        print_warning(f"找不到本地配置文件: {local_config}")
        return False

    with open(local_config, "rb") as config_file:
        config_bytes = config_file.read()
    if re.search(rb"\ballow_insecure\s*:\s*true\b", config_bytes, re.IGNORECASE):
        print_warning("拒绝部署 allow_insecure: true 的 dae 配置。")
        return False
    expected_sha256 = hashlib.sha256(config_bytes).hexdigest()
    remote_command = (
        "sudo -- bash -c 'set -eu; umask 077; install -d -m 0755 /usr/local/etc/dae; "
        "tmp=$(mktemp /usr/local/etc/dae/.config.XXXXXX); "
        "trap \"rm -f $tmp\" EXIT; cat >\"$tmp\"; "
        "actual=$(sha256sum \"$tmp\" | cut -d \" \" -f 1); "
        f"[ \"$actual\" = \"{expected_sha256}\" ] || "
        "{ echo checksum-mismatch >&2; exit 1; }; "
        "chown root:root \"$tmp\"; chmod 0600 \"$tmp\"; "
        "mv -f \"$tmp\" /usr/local/etc/dae/config.dae; trap - EXIT; "
        "systemctl enable dae; systemctl restart dae'"
    )
    exec_cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not exec_cmd:
        return False

    print_info("正在应用配置并重启 dae ...")
    try:
        result = subprocess.run(exec_cmd, input=config_bytes)
        if result.returncode == 0:
            print_success("配置已更新并重启 dae。")
            return True
        print_warning(f"配置应用失败，退出码: {result.returncode}")
        return False
    except Exception as e:
        print_warning(f"配置应用失败: {e}")
        return False


def main():
    print("GCP 免费服务器多功能管理工具")
    project_id = select_gcp_project()
    current_instance = None
    remote_config = None

    while True:
        print("\n================================================")
        print(f"当前项目: {project_id}")
        if current_instance:
            print(f"当前服务器: {current_instance['name']} ({current_instance['zone']})")
        else:
            print("当前服务器: 未选择")
        print("------------------------------------------------")
        print("[1] 新建免费实例")
        print("[2] 选择服务器")
        print("[3] 刷 AMD CPU")
        print("[4] 配置防火墙规则")
        print("[5] Debian换源")
        print("[6] 安装 dae")
        print("[7] 上传 config.dae 并启用 dae")
        print("[8] 安装流量监控脚本（仅适配 Debian）")
        print("[9] 删除当前免费资源")
        print("[0] 退出")
        choice = input("请输入数字选择: ").strip()

        if choice == "1":
            zone = select_zone(project_id)
            os_config = select_os_image()
            ssh_access = select_ssh_access(project_id)
            if not ssh_access:
                print_warning("未配置 SSH 接入，实例未创建。")
                continue
            source_ranges, suggested_remote = ssh_access

            pending_instance = {
                "name": "free-tier-vm",
                "network": "global/networks/default",
            }
            if not remove_legacy_allow_all_rule(project_id, pending_instance["network"]):
                print_warning("无法确认旧的全开放规则已移除，实例未创建。")
                continue
            if not add_restricted_ssh_ingress(project_id, pending_instance, source_ranges):
                print_warning("无法预先建立 SSH 限制规则，实例未创建。")
                continue
            if create_instance(project_id, zone, os_config):
                if suggested_remote:
                    remote_config = suggested_remote
            else:
                print_warning(
                    "实例创建未确认成功；为避免误删已存在或仍在创建实例的保护，"
                    "预建防火墙规则会保留。请到控制台确认实例状态。"
                )
        elif choice == "2":
            current_instance = select_instance(project_id)
        elif choice == "3":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                reroll_cpu_loop(project_id, current_instance)
        elif choice == "4":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                configured, suggested_remote = configure_firewall(project_id, current_instance)
                if configured and suggested_remote:
                    remote_config = suggested_remote
        elif choice == "5":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "apt", remote_config)
        elif choice == "6":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "dae", remote_config)
        elif choice == "7":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    deploy_dae_config(project_id, current_instance, remote_config)
        elif choice == "8":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                traffic_selection = select_traffic_monitor_script()
                if traffic_selection:
                    script_key, traffic_limit_gib = traffic_selection
                    if not remote_config:
                        remote_config = pick_remote_method()
                    if remote_config:
                        run_remote_script(
                            project_id,
                            current_instance,
                            script_key,
                            remote_config,
                            traffic_limit_gib=traffic_limit_gib,
                        )
        elif choice == "9":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if delete_free_resources(project_id, current_instance):
                    current_instance = None
        elif choice == "0":
            print("已退出。")
            break
        else:
            print("输入无效，请重试。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[用户终止] 脚本已停止。")
    except Exception as e:
        print(f"\n[错误] 发生异常: {e}")
        traceback.print_exc()
