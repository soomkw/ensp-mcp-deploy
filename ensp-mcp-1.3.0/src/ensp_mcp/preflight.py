"""只读检查 eNSP、VirtualBox、模板机和外部设备镜像。"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from . import catalog


AR_VBOX_MODELS = {"AR201", "AR1220", "AR2220", "AR2240", "AR3260"}
SVRP_MODELS = {"NE40E", "CE6800", "CE12800"}
WLAN_BASES = {
    "AC6005": "WLAN_AC_Base",
    "AC6605": "WLAN_AC_Base",
    "AD9430": "WLAN_AD_Base",
    "R250D": "WLAN_SAP_Base",
}
LOCAL_EXECUTABLES = {
    "S5700": ("vboxserver/devices/LSW/s5700/eNSP_Switch.exe",),
    "S3700": ("vboxserver/devices/LSW/s3700/eNSP_Switch.exe",),
    "USG5500": (
        "vboxserver/devices/FW/USG5500/eNSP_Firewall.exe",
        "vboxserver/devices/FW/USG5500/flash.dat",
        "vboxserver/devices/FW/USG5500/web.bin",
    ),
}


def runtime_kind(model: str) -> str:
    try:
        model = catalog.get_model(model).name
    except (KeyError, ValueError):
        return "unknown"
    if model in AR_VBOX_MODELS:
        return "virtualbox_ar"
    if model in SVRP_MODELS:
        return "svrp_image"
    if model == "USG6000V":
        return "ngfw_image"
    if model in LOCAL_EXECUTABLES:
        return "local_simulator"
    if model.startswith("AP"):
        return "virtualbox_wlan_ap"
    if model in WLAN_BASES:
        return "virtualbox_wlan"
    if model == "Router":
        return "legacy_unknown"
    return "built_in"


def _path_record(path: Path | None, source: str = "", required_file: str | Path = "") -> dict:
    found = bool(path and path.is_dir())
    if found and required_file:
        found = (path / required_file).is_file()
    return {
        "found": found,
        "path": str(path.resolve()) if path and path.exists() else (str(path) if path else None),
        "source": source or None,
    }


def _registry_ensp() -> tuple[Path | None, str, str]:
    if os.name != "nt":
        return None, "", ""
    try:
        import winreg
    except ImportError:
        return None, "", ""
    roots = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for root_name in roots:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_name) as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        sub_name = winreg.EnumKey(root, index)
                        with winreg.OpenKey(root, sub_name) as sub:
                            display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            if not display.lower().startswith("ensp"):
                                continue
                            location = str(winreg.QueryValueEx(sub, "InstallLocation")[0]).strip()
                            try:
                                version = str(winreg.QueryValueEx(sub, "DisplayVersion")[0])
                            except OSError:
                                version = re.sub(r"^eNSP\s*V?", "", display, flags=re.I)
                            if location:
                                return Path(location), version, "registry"
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue
    return None, "", ""


def find_ensp(explicit: str = "") -> dict:
    marker = Path("res") / "items.xml"
    candidates: list[tuple[Path, str, str]] = []
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir() and (path / marker).is_file():
            record = _path_record(path, "argument", marker)
            record["version"] = _version_from_files(path)
            record["expectedVirtualBox"] = _expected_vbox(path)
            return record
        record = _path_record(path, "argument", marker)
        record.update({"version": "", "expectedVirtualBox": "5.2.x"})
        return record
    env_home = os.environ.get("ENSP_HOME", "").strip()
    if env_home:
        candidates.append((Path(env_home).expanduser(), "", "ENSP_HOME"))
    registry_path, registry_version, registry_source = _registry_ensp()
    if registry_path:
        candidates.append((registry_path, registry_version, registry_source))
    if os.name == "nt":
        candidates.extend(
            (Path(value), "", "common_path")
            for value in (
                r"C:\Program Files\Huawei\eNSP",
                r"C:\Program Files (x86)\Huawei\eNSP",
                r"D:\Application\eNSP",
            )
        )
    for path, version, source in candidates:
        if path.is_dir() and (path / marker).is_file():
            record = _path_record(path, source, marker)
            record["version"] = version or _version_from_files(path)
            record["expectedVirtualBox"] = _expected_vbox(path)
            return record
    record = _path_record(candidates[0][0], candidates[0][2], marker) if candidates else _path_record(None)
    record.update({"version": "", "expectedVirtualBox": "5.2.x"})
    return record


def _version_from_files(home: Path) -> str:
    for path in (home / "whatsnew.txt", home / "release_notes.txt"):
        if not path.is_file():
            continue
        try:
            match = re.search(r"V?1\.3\.00\.\d+T", path.read_text(encoding="utf-8", errors="ignore"))
            if match:
                return match.group(0).lstrip("V")
        except OSError:
            pass
    return ""


def _expected_vbox(home: Path) -> str:
    tools = home / "tools"
    if tools.is_dir():
        for path in tools.glob("VirtualBox-*-Win.exe"):
            match = re.search(r"VirtualBox-(\d+\.\d+)", path.name, re.I)
            if match:
                return match.group(1) + ".x"
    return "5.2.x"


def _registry_vbox() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg
        for name in (r"SOFTWARE\Oracle\VirtualBox", r"SOFTWARE\WOW6432Node\Oracle\VirtualBox"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, name) as key:
                    return Path(str(winreg.QueryValueEx(key, "InstallDir")[0]))
            except OSError:
                continue
    except ImportError:
        pass
    return None


def find_vbox() -> dict:
    manager_name = "VBoxManage.exe" if os.name == "nt" else "VBoxManage"
    candidates: list[tuple[Path, str]] = []
    env_home = os.environ.get("VBOX_MSI_INSTALL_PATH", "").strip()
    if env_home:
        candidates.append((Path(env_home), "VBOX_MSI_INSTALL_PATH"))
    registry_path = _registry_vbox()
    if registry_path:
        candidates.append((registry_path, "registry"))
    executable = shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
    if executable:
        candidates.append((Path(executable).parent, "PATH"))
    if os.name == "nt":
        candidates.append((Path(r"C:\Program Files\Oracle\VirtualBox"), "common_path"))
    for home, source in candidates:
        manager = home / manager_name
        if manager.is_file():
            record = _path_record(home, source, manager_name)
            record["executable"] = str(manager.resolve())
            return record
    record = _path_record(candidates[0][0], candidates[0][1], manager_name) if candidates else _path_record(None)
    record["executable"] = None
    return record


def _run(command: list[str], timeout: float = 5.0) -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
            check=False,
        )
        return {"ok": completed.returncode == 0, "returnCode": completed.returncode, "output": completed.stdout}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returnCode": None, "output": str(exc)}


def _parse_boolean(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in {"yes", "true", "1", "enabled", "running"}:
        return True
    if lowered in {"no", "false", "0", "disabled", "not supported"}:
        return False
    return None


def _host_info(manager: str) -> dict:
    result = _run([manager, "list", "hostinfo"])
    parsed = {"commandOk": result["ok"], "hardwareVirtualization": None, "nestedPaging": None}
    for line in result["output"].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normal = key.strip().lower()
        if "supports hw virtualization" in normal:
            parsed["hardwareVirtualization"] = _parse_boolean(value)
        elif "supports nested paging" in normal:
            parsed["nestedPaging"] = _parse_boolean(value)
    return parsed


def _registered_vms(manager: str) -> tuple[bool, list[dict]]:
    result = _run([manager, "list", "vms"])
    vms = []
    for line in result["output"].splitlines():
        match = re.match(r'^"(.*)"\s+\{([^}]+)\}\s*$', line.strip())
        if match:
            vms.append({"name": match.group(1), "uuid": match.group(2)})
    return bool(result["ok"]), vms


def _host_only(manager: str) -> tuple[bool, list[dict]]:
    result = _run([manager, "list", "hostonlyifs"])
    adapters: list[dict] = []
    current: dict[str, str] = {}
    for line in result["output"].splitlines() + [""]:
        if not line.strip():
            if current:
                adapters.append(
                    {
                        "name": current.get("Name"),
                        "status": current.get("Status"),
                        "ipAddress": current.get("IPAddress"),
                        "networkMask": current.get("NetworkMask"),
                    }
                )
                current = {}
            continue
        key, separator, value = line.partition(":")
        if separator:
            current[key.strip()] = value.strip()
    return bool(result["ok"]), adapters


def _inaccessible_media(manager: str) -> list[dict]:
    result = _run([manager, "list", "hdds"])
    if not result["ok"]:
        return []
    media: list[dict] = []
    current: dict[str, str] = {}
    for line in result["output"].splitlines() + [""]:
        if not line.strip():
            if current and current.get("State", "").lower() == "inaccessible":
                media.append({"uuid": current.get("UUID"), "location": current.get("Location")})
            current = {}
            continue
        key, separator, value = line.partition(":")
        if separator:
            current[key.strip()] = value.strip()
    return media


def _windows_virtualization() -> dict:
    result = {"hypervisorPresent": None, "vbsStatus": None}
    if os.name != "nt":
        return result
    command = (
        "$c=Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue;"
        "$v=Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard "
        "-ClassName Win32_DeviceGuard -ErrorAction SilentlyContinue;"
        "[pscustomobject]@{hypervisorPresent=$c.HypervisorPresent;"
        "vbsStatus=$v.VirtualizationBasedSecurityStatus}|ConvertTo-Json -Compress"
    )
    completed = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], timeout=6.0)
    if completed["ok"]:
        try:
            data = json.loads(completed["output"].strip())
            result["hypervisorPresent"] = data.get("hypervisorPresent")
            raw_status = data.get("vbsStatus")
            result["vbsStatus"] = int(raw_status) if raw_status is not None else None
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return result


def _recent_hyperv_error(ensp_home: Path | None) -> bool:
    if ensp_home is None:
        return False
    path = ensp_home / "vboxserver" / "log" / "VBoxManage.log"
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 512_000))
            text = handle.read().decode("utf-8", errors="ignore")
        return "VERR_SUPDRV_NO_RAW_MODE_HYPER_V_ROOT" in text
    except OSError:
        return False


def _ini_fields(path: Path) -> dict:
    fields = {"ImgFile": "", "RootKey": "", "SupportKey": ""}
    if not path.is_file():
        return fields
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        lookup = {key.casefold(): key for key in fields}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            canonical = lookup.get(key.strip().casefold())
            if separator and canonical:
                fields[canonical] = value.strip()
    except OSError:
        pass
    return fields


def _file_checks(home: Path | None, relatives: Iterable[str]) -> list[dict]:
    checks = []
    for relative in relatives:
        path = home / relative if home else Path(relative)
        present = bool(home and path.is_file() and path.stat().st_size > 0)
        checks.append({"path": str(path), "present": present})
    return checks


def _base_check(
    home: Path | None,
    registered: set[str],
    base: str,
    manager: str | None,
    active_adapter_names: set[str],
) -> dict:
    vdi = home / "vboxserver" / base / f"{base}.vdi" if home else Path(f"{base}.vdi")
    snapshots = list((vdi.parent / "Snapshots").glob("*.vdi")) if vdi.parent.is_dir() else []
    info = (
        _run([manager, "showvminfo", base, "--machinereadable"])
        if manager and base in registered
        else {"ok": False, "output": ""}
    )
    attached_adapters = re.findall(r'^hostonlyadapter\d+="(.*)"$', info["output"], re.MULTILINE)
    snapshot_match = re.search(r'^CurrentSnapshotName="(.*)"$', info["output"], re.MULTILINE)
    snapshot_name = snapshot_match.group(1) if snapshot_match else None
    network_ready = bool(attached_adapters) and all(name in active_adapter_names for name in attached_adapters)
    return {
        "baseVm": base,
        "registered": base in registered,
        "configurationReadable": bool(info["ok"]),
        "disk": str(vdi),
        "diskPresent": bool(vdi.is_file() and vdi.stat().st_size > 0),
        "snapshotCount": len(snapshots),
        "currentSnapshot": snapshot_name,
        "hostOnlyAdapters": attached_adapters,
        "networkReady": network_ready,
        "ready": bool(
            base in registered
            and info["ok"]
            and vdi.is_file()
            and snapshots
            and snapshot_name
            and snapshot_name.endswith("_Link")
            and network_ready
        ),
    }


def _model_check(
    model: str,
    ensp_home: Path | None,
    vbox_core_ready: bool,
    registered: set[str],
    manager: str | None = None,
    active_adapter_names: set[str] | None = None,
) -> dict:
    try:
        model = catalog.get_model(model).name
    except (KeyError, ValueError):
        pass
    runtime = runtime_kind(model)
    result: dict = {"model": model, "runtime": runtime, "ready": True, "blockers": [], "warnings": []}
    active_adapter_names = active_adapter_names or set()
    if runtime == "virtualbox_ar":
        base = _base_check(ensp_home, registered, "AR_Base", manager, active_adapter_names)
        result["base"] = base
        result["ready"] = vbox_core_ready and base["ready"]
        if not vbox_core_ready:
            result["blockers"].append("VirtualBox 运行环境不可用")
        if not base["ready"]:
            result["blockers"].append("AR_Base 未完整注册，或基础磁盘/快照缺失")
    elif runtime in {"virtualbox_wlan", "virtualbox_wlan_ap"}:
        base_name = "WLAN_AP_Base" if runtime == "virtualbox_wlan_ap" else WLAN_BASES[model]
        base = _base_check(ensp_home, registered, base_name, manager, active_adapter_names)
        result["base"] = base
        result["ready"] = vbox_core_ready and base["ready"]
        if not vbox_core_ready:
            result["blockers"].append("VirtualBox 运行环境不可用")
        if not base["ready"]:
            result["blockers"].append(f"{base_name} 未完整注册，或基础磁盘/快照缺失")
    elif runtime == "ngfw_image":
        image = ensp_home / "plugin" / "ngfw" / "Database" / "vfw_usg.vdi" if ensp_home else Path("vfw_usg.vdi")
        fields = _ini_fields(ensp_home / "plugin" / "ngfw" / "System" / "system.ini") if ensp_home else _ini_fields(Path(""))
        registered_image = all(fields.values())
        image_ready = bool(image.is_file() and image.stat().st_size > 0)
        image_recognized = bool(
            manager and image_ready and _run([manager, "showmediuminfo", "disk", str(image)])["ok"]
        )
        result.update({
            "image": str(image),
            "imagePresent": image_ready,
            "imageRecognizedByVirtualBox": image_recognized,
            "registrationFieldsPresent": registered_image,
        })
        result["ready"] = vbox_core_ready and image_ready and image_recognized and registered_image
        if not vbox_core_ready:
            result["blockers"].append("VirtualBox 运行环境不可用")
        if not image_ready:
            result["blockers"].append("USG6000V 镜像 vfw_usg.vdi 未安装")
        elif not image_recognized:
            result["blockers"].append("VirtualBox 无法识别 USG6000V 镜像")
        if not registered_image:
            result["blockers"].append("USG6000V 镜像注册信息不完整")
        result["warnings"].append("只有实际启动后才能最终确认镜像与当前版本兼容")
    elif runtime == "svrp_image":
        image = ensp_home / "plugin" / "svrp" / "Database" / "svrpbox.img" if ensp_home else Path("svrpbox.img")
        fields = _ini_fields(ensp_home / "plugin" / "svrp" / "System" / "system.ini") if ensp_home else _ini_fields(Path(""))
        registered_image = all(fields.values())
        image_ready = bool(image.is_file() and image.stat().st_size > 0)
        image_recognized = bool(
            manager and image_ready and _run([manager, "showmediuminfo", "disk", str(image)])["ok"]
        )
        result.update({
            "image": str(image),
            "imagePresent": image_ready,
            "imageRecognizedByVirtualBox": image_recognized,
            "registrationFieldsPresent": registered_image,
        })
        result["ready"] = vbox_core_ready and image_ready and image_recognized and registered_image
        if not vbox_core_ready:
            result["blockers"].append("VirtualBox 运行环境不可用")
        if not image_ready:
            result["blockers"].append("SVRP 镜像 svrpbox.img 未安装")
        elif not image_recognized:
            result["blockers"].append("VirtualBox 无法识别 SVRP 镜像")
        if not registered_image:
            result["blockers"].append("SVRP 镜像注册信息不完整")
    elif runtime == "local_simulator":
        files = _file_checks(ensp_home, LOCAL_EXECUTABLES[model])
        result["files"] = files
        result["ready"] = all(item["present"] for item in files)
        if not result["ready"]:
            result["blockers"].append("本地模拟器或固件文件缺失")
    elif runtime == "legacy_unknown":
        result["ready"] = False
        result["blockers"].append("旧 Router 型号已停止维护，运行依赖无法可靠确认")
        result["warnings"].append("优先使用 AR 系列，并先检查 VirtualBox")
    elif runtime == "unknown":
        result["ready"] = False
        result["blockers"].append("内置目录中没有该型号，无法判断启动依赖")
    elif ensp_home is None:
        result["ready"] = False
        result["blockers"].append("未找到 eNSP 安装目录")
    return result


def inspect_runtime(required_models: Iterable[str] = (), ensp_home: str = "") -> dict:
    """执行只读预检。不会启动虚拟机、修改 Windows 功能或注册镜像。"""
    required = []
    for value in required_models:
        raw = str(value)
        try:
            raw = catalog.get_model(raw).name
        except (KeyError, ValueError):
            pass
        if raw not in required:
            required.append(raw)
    ensp = find_ensp(ensp_home)
    ensp_path = Path(ensp["path"]) if ensp["found"] and ensp["path"] else None
    vbox = find_vbox()
    manager = vbox.get("executable")
    version = ""
    host = {"commandOk": False, "hardwareVirtualization": None, "nestedPaging": None}
    vm_command_ok = False
    registered_vms: list[dict] = []
    adapter_command_ok = False
    adapters: list[dict] = []
    inaccessible: list[dict] = []
    if manager:
        version_result = _run([manager, "--version"])
        if version_result["ok"]:
            version = version_result["output"].strip().splitlines()[0]
        host = _host_info(manager)
        vm_command_ok, registered_vms = _registered_vms(manager)
        adapter_command_ok, adapters = _host_only(manager)
        inaccessible = _inaccessible_media(manager)
    windows = _windows_virtualization()
    log_conflict = _recent_hyperv_error(ensp_path)
    recommended_version = version.startswith("5.2.") or version.startswith("5.2r")
    hypervisor_conflict = bool(
        version.startswith("5.2")
        and (windows["hypervisorPresent"] is True or windows["vbsStatus"] == 2 or log_conflict)
    )
    active_adapters = [
        adapter for adapter in adapters if str(adapter.get("status", "")).lower() in {"up", "running"}
    ]
    vbox_core_ready = bool(
        manager
        and version
        and recommended_version
        and host["commandOk"]
        and host["hardwareVirtualization"] is True
        and not hypervisor_conflict
        and vm_command_ok
        and adapter_command_ok
        and active_adapters
    )
    vbox.update(
        {
            "version": version or None,
            "recommendedVersion": ensp.get("expectedVirtualBox") or "5.2.x",
            "versionRecommended": recommended_version if version else False,
            "host": host,
            "registeredVms": registered_vms,
            "hostOnlyAdapters": adapters,
            "inaccessibleMedia": inaccessible,
            "windowsHypervisor": windows,
            "recentHyperVRawModeError": log_conflict,
            "coreReady": vbox_core_ready,
        }
    )

    registered_names = {vm["name"] for vm in registered_vms}
    active_adapter_names = {str(adapter.get("name")) for adapter in active_adapters if adapter.get("name")}
    model_checks = [
        _model_check(model, ensp_path, vbox_core_ready, registered_names, manager, active_adapter_names)
        for model in required
    ]
    topology_ready = all(check["ready"] for check in model_checks) if required else None

    issues: list[dict] = []

    def issue(severity: str, code: str, message: str, remediation: str = "", affects: list[str] | None = None) -> None:
        issues.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "remediation": remediation or None,
                "affects": affects or [],
            }
        )

    if not ensp["found"]:
        issue("error", "ENSP_NOT_FOUND", "未找到有效的 eNSP 安装目录。", "传入 ensp_home 或设置 ENSP_HOME。", required)
    if not vbox["found"]:
        issue("error", "VBOX_NOT_FOUND", "未找到 VirtualBox/VBoxManage。", "安装与 eNSP 版本匹配的 VirtualBox。", required)
    elif version and not recommended_version:
        issue(
            "error" if any(runtime_kind(model).startswith(("virtualbox", "svrp", "ngfw")) for model in required) else "warning",
            "VBOX_VERSION_RISK",
            f"VirtualBox {version} 不是 eNSP 200T 推荐的 5.2.x。",
            "使用 eNSP 随附或经实际验证的 5.2.x，并重新运行预检。",
            required,
        )
    if hypervisor_conflict:
        issue(
            "error",
            "VBOX_HYPERV_CONFLICT",
            "Windows Hyper-V/VBS 正在占用硬件虚拟化，VirtualBox 5.2 的 eNSP 虚拟设备会启动失败。",
            "在确认系统用途后关闭占用虚拟化的 Hyper-V/VBS 功能并重启；本工具不会自动修改系统设置。",
            ["AR 系列", "WLAN", "NE40E/CE", "USG6000V"],
        )
    elif manager and host["hardwareVirtualization"] is False:
        issue(
            "error",
            "VBOX_HW_VIRTUALIZATION_UNAVAILABLE",
            "VirtualBox 当前拿不到硬件虚拟化能力。",
            "检查 BIOS 虚拟化与 Windows hypervisor 占用后重启，再确认 VBoxManage list hostinfo。",
            ["AR 系列", "WLAN", "NE40E/CE", "USG6000V"],
        )
    if manager and not active_adapters:
        issue("error", "VBOX_HOST_ONLY_UNAVAILABLE", "没有处于 Up 状态的 VirtualBox Host-Only 网卡。", "修复 VirtualBox Host-Only 网络后再启动 eNSP 设备。", required)
    if inaccessible:
        issue("warning", "VBOX_INACCESSIBLE_MEDIA", f"VirtualBox 记录了 {len(inaccessible)} 个不可访问磁盘。", "清理已删除实验遗留的失效磁盘注册项前，先确认它们不属于现有有用的拓扑。")

    for check in model_checks:
        if check["ready"] or check["model"] == "USG6000V" or check["model"] in SVRP_MODELS:
            continue
        issue(
            "error",
            "MODEL_RUNTIME_NOT_READY",
            f"{check['model']}：{'；'.join(check['blockers'])}",
            "按 modelChecks 中的基础机、磁盘或本地文件检查结果修复后重新预检。",
            [check["model"]],
        )

    # 即使当前拓扑没使用，也报告两类常见的外部镜像状态；此时只作为提示。
    for optional_model, code in (("USG6000V", "USG6000V_NOT_READY"), ("NE40E", "SVRP_NOT_READY")):
        check = _model_check(
            optional_model, ensp_path, vbox_core_ready, registered_names, manager, active_adapter_names
        )
        if not check["ready"]:
            svrp_required = [model for model in required if model in SVRP_MODELS]
            in_topology = optional_model in required or (optional_model == "NE40E" and bool(svrp_required))
            message = "；".join(check["blockers"])
            remediation = "没有硬性要求时优先使用 USG5500。" if optional_model == "USG6000V" else "安装并注册匹配版本的 SVRP 镜像。"
            affects = [optional_model] if optional_model == "USG6000V" else (svrp_required or [optional_model])
            issue("error" if in_topology else "warning", code, message, remediation, affects)

    return {
        "platform": {"system": platform.system(), "release": platform.release()},
        "ensp": ensp,
        "virtualBox": vbox,
        "modelChecks": model_checks,
        "readyForTopology": topology_ready,
        "virtualDeviceRuntimeReady": vbox_core_ready,
        "issues": issues,
        "errorCount": sum(item["severity"] == "error" for item in issues),
        "warningCount": sum(item["severity"] == "warning" for item in issues),
        "readOnly": True,
    }
