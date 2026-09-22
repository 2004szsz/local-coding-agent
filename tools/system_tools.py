# -*- coding: utf-8 -*-
"""
系统数据只读层与受控系统动作（前缀 sys_）。

哲学与 RAG / MCP 完全一致（docs/local-system-access-plan.md 3.3）：
**可选增强不得成为主链路单点故障**。psutil 缺失 → 整组只读工具不注册、
打印一行警告、对话继续。

脱敏是硬要求：进程命令行（常含 `--api-key xxx`）与环境变量最容易泄露，
**宁可少给字段，不可事后补救**——值一旦进上下文与日志就收不回。

注册入口：
    register_system_tools          只读组（sys_overview/processes/disks/network/env/
                                  battery/hardware/services/installed_apps），
                                  `system.expose` 决定注册哪些（默认安全子集）
    register_system_action_tools  动作组（sys_open_path/sys_notify/sys_launch/
                                  sys_screenshot），`system.allow_actions` 白名单决定，
                                  默认空 = 全部不注册（切片 5 审批通道前保持关闭）
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import Tool, ToolRegistry
from .fs_access import AccessBroker

__all__ = ["register_system_tools", "register_system_action_tools",
           "redact", "redact_cmdline", "psutil_available"]

try:  # psutil 是可选依赖：缺失 → 只读组不注册
    import psutil  # noqa: F401
    psutil_available = True
except ImportError:
    psutil = None
    psutil_available = False

_IS_WINDOWS = sys.platform == "win32"

#: 脱敏关键词：env 键名与命令行参数名命中任意一个即屏蔽值
_REDACT_TOKENS = ("key", "token", "secret", "password", "passwd", "credential",
                  "session", "cookie", "auth")
#: 进程命令行展示上限（字符）
_CMDLINE_LIMIT = 200
#: 只读查询的缓存秒数（psutil 调用较慢，5 秒内复用上次结果）
_CACHE_TTL = 5.0
#: 采样型 CPU 占用（首次调用返回 0.0 属已知行为，文档写明即可）
_CPU_SAMPLE_INTERVAL = 0.5


def redact(value: str) -> str:
    """命中脱敏关键词时整值屏蔽（用于环境变量值）。"""
    text = str(value or "")
    return "***" if _token_hit(text) else text


def _token_hit(key_text: str) -> bool:
    lowered = str(key_text or "").lower()
    return any(token in lowered for token in _REDACT_TOKENS)


def redact_cmdline(cmdline: List[str]) -> str:
    """
    进程命令行 → 展示串：截断 200 字符 + 屏蔽 `--xxx-key value` 形态的敏感值。

    先拼成整串再屏蔽——`--api-key` 与 `SECRETVALUE` 通常是 argv 的两个元素，
    逐元素替换会漏掉跨元素的「flag + 值」对。
    """
    joined = " ".join(str(item) for item in (cmdline or []))
    joined = _REDACT_FLAG.sub(r"\1***", joined)
    return joined[:_CMDLINE_LIMIT] + ("…" if len(joined) > _CMDLINE_LIMIT else "")


#: `--api-key=xxx` / `-password xxx` → `--api-key=***` / `-password ***`
_REDACT_FLAG = re.compile(
    r"(?i)(--?[a-z0-9_-]*(?:key|token|secret|password|passwd|credential|session|"
    r"cookie|auth)[a-z0-9_-]*(?:=|\s+))(\S+)"
)


# ======================================================================
# 缓存
# ======================================================================
def _cached(store: Dict[str, Tuple[float, Any]], key: str, factory: Callable[[], Any]) -> Any:
    hit = store.get(key)
    if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    value = factory()
    store[key] = (time.monotonic(), value)
    return value


# ======================================================================
# 只读组
# ======================================================================
def _core_psutil() -> Any:
    if psutil is None:
        raise RuntimeError("psutil 未安装，系统数据只读层不可用")
    return psutil


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _build_sys_overview() -> Tool:
    cache: Dict[str, Tuple[float, Any]] = {}

    def sys_overview(kwargs: Dict[str, Any]) -> str:
        ps = _core_psutil()

        def snapshot():
            cpu = ps.cpu_percent(interval=_CPU_SAMPLE_INTERVAL)
            vm = ps.virtual_memory()
            boot = time.time() - ps.boot_time()
            return (f"系统: {platform.platform()}\n"
                    f"CPU: 占用 {cpu}%｜核心 {ps.cpu_count(logical=False)} 物理 / "
                    f"{ps.cpu_count(logical=True)} 逻辑\n"
                    f"内存: 用 {_fmt_bytes(vm.used)} / {_fmt_bytes(vm.total)} "
                    f"（{vm.percent}%）\n"
                    f"开机时长: {int(boot // 3600)} 小时 {int(boot % 3600 // 60)} 分钟")

        return _cached(cache, "overview", snapshot)

    return Tool(
        name="sys_overview",
        description="本机概览：OS 版本、CPU 型号占用与核数、内存、开机时长。只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_overview,
    )


def _build_sys_processes() -> Tool:
    def sys_processes(kwargs: Dict[str, Any]) -> str:
        ps = _core_psutil()
        top_n = max(1, min(int(kwargs.get("top_n", 15)), 50))
        rows = []
        for proc in ps.process_iter(["pid", "name", "cpu_percent", "memory_percent",
                                     "username", "cmdline"]):
            try:
                info = proc.info
                cmdline = redact_cmdline(info.get("cmdline") or [])
                rows.append((info.get("cpu_percent") or 0.0,
                             f"pid={info.get('pid')} {info.get('name')} "
                             f"CPU={info.get('cpu_percent') or 0.0}% "
                             f"MEM={info.get('memory_percent') or 0.0}% "
                             f"user={info.get('username') or '-'} "
                             f"cmd={cmdline or '-'}"))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda r: r[0], reverse=True)
        lines = [r[1] for r in rows[:top_n]]
        return (f"CPU 占用最高的 {len(lines)} 个进程（cmdline 已脱敏并截断）:\n"
                + "\n".join(lines))

    return Tool(
        name="sys_processes",
        description="按 CPU 占用列出进程（pid/名称/内存/用户/命令行）。"
                    "命令行已脱敏并截断 200 字符。只读，无副作用。",
        parameters={
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "description": "返回前 N 个，默认 15，最大 50"},
            },
        },
        handler=sys_processes,
    )


def _build_sys_disks() -> Tool:
    cache: Dict[str, Tuple[float, Any]] = {}

    def sys_disks(kwargs: Dict[str, Any]) -> str:
        ps = _core_psutil()

        def snapshot():
            lines = []
            for part in ps.disk_partitions(all=False):
                try:
                    usage = ps.disk_usage(part.mountpoint)
                except (PermissionError, OSError):
                    continue
                lines.append(f"{part.device} ({part.mountpoint}) {part.fstype} "
                             f"共 {_fmt_bytes(usage.total)} 用 {_fmt_bytes(usage.used)} "
                             f"余 {_fmt_bytes(usage.free)}")
            return "\n".join(lines) or "(无磁盘分区信息)"

        return _cached(cache, "disks", snapshot)

    return Tool(
        name="sys_disks",
        description="磁盘分区：设备、挂载点、文件系统、总量/已用/剩余。只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_disks,
    )


def _build_sys_network() -> Tool:
    cache: Dict[str, Tuple[float, Any]] = {}

    def sys_network(kwargs: Dict[str, Any]) -> str:
        ps = _core_psutil()

        def snapshot():
            lines = []
            for name, addrs in ps.net_if_addrs().items():
                for addr in addrs:
                    if str(addr.family).startswith("AddressFamily.AF_INET"):
                        lines.append(f"{name}: {addr.address}")
            io = ps.net_io_counters()
            lines.append(f"累计: 收 {_fmt_bytes(io.bytes_recv)} / 发 {_fmt_bytes(io.bytes_sent)}")
            # 连接详情默认关：net_connections 在 Windows 上慢且敏感
            return "\n".join(lines) or "(无网络信息)"

        return _cached(cache, "network", snapshot)

    return Tool(
        name="sys_network",
        description="网卡 IPv4 地址与累计收发流量。连接详情默认不提供（慢且隐私敏感）。"
                    "只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_network,
    )


def _build_sys_env() -> Tool:
    def sys_env(kwargs: Dict[str, Any]) -> str:
        lines = []
        for key in sorted(os.environ.keys()):
            value = os.environ[key]
            shown = "***（已脱敏）" if _token_hit(key) else value
            lines.append(f"{key}={shown}")
        return "进程环境变量（密钥类已脱敏）:\n" + "\n".join(lines)

    return Tool(
        name="sys_env",
        description="列出进程环境变量。键名含 key/token/secret/password 等一律脱敏。"
                    "只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_env,
    )


def _build_sys_battery() -> Tool:
    def sys_battery(kwargs: Dict[str, Any]) -> str:
        ps = _core_psutil()
        battery = ps.sensors_battery()
        if battery is None:
            return "(无电池信息，可能是台式机)"
        return (f"电量 {round(battery.percent, 1)}%｜"
                f"{'充电中' if battery.power_plugged else '使用电池'}")

    return Tool(
        name="sys_battery",
        description="笔记本电池电量与是否充电。只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_battery,
    )


def _build_sys_hardware() -> Tool:
    def sys_hardware(kwargs: Dict[str, Any]) -> str:
        ps = _core_psutil()
        freq = ps.cpu_freq()
        freq_line = f"频率: {freq.current:.0f} MHz" if freq else "频率: 未知"
        return (f"CPU: {platform.processor() or '未知'}\n"
                f"{freq_line}\n"
                f"物理核 {ps.cpu_count(logical=False)} / 逻辑核 {ps.cpu_count(logical=True)}\n"
                f"总内存: {_fmt_bytes(ps.virtual_memory().total)}")

    return Tool(
        name="sys_hardware",
        description="CPU 型号频率、物理/逻辑核数、总内存。只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_hardware,
    )


def _build_sys_installed_apps() -> Tool:
    """注册表卸载项（Windows 内置 winreg，无需 psutil；较慢，带缓存）。"""
    cache: Dict[str, Tuple[float, Any]] = {}
    _UNINSTALL_KEYS = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                       r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")

    def sys_installed_apps(kwargs: Dict[str, Any]) -> str:
        def snapshot():
            if not _IS_WINDOWS:
                return "(仅 Windows 提供已装软件列表)"
            try:
                import winreg
            except ImportError:
                return "(winreg 不可用)"
            apps: Dict[str, str] = {}
            for uninstall_key in _UNINSTALL_KEYS:
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, uninstall_key) as root_key:
                        for i in range(winreg.QueryInfoKey(root_key)[0]):
                            try:
                                with winreg.OpenKey(root_key,
                                                    winreg.EnumKey(root_key, i)) as sub:
                                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                                    version = ""
                                    try:
                                        version = winreg.QueryValueEx(sub,
                                                                      "DisplayVersion")[0]
                                    except OSError:
                                        pass
                                    apps.setdefault(name, version)
                            except OSError:
                                continue
                except OSError:
                    continue
            lines = [f"{name} {version}".strip()
                     for name, version in sorted(apps.items(), key=lambda kv: kv[0].lower())]
            return "\n".join(lines[:200]) + (f"\n...[共 {len(lines)} 项，已截断]" if len(lines) > 200 else "")

        return _cached(cache, "apps", snapshot)

    return Tool(
        name="sys_installed_apps",
        description="已安装软件（名称+版本，来自注册表卸载项，最多 200 条）。只读，无副作用。",
        parameters={"type": "object", "properties": {}},
        handler=sys_installed_apps,
    )


# ======================================================================
# 动作组（L4 / L2，默认全部关闭）
# ======================================================================
def _build_sys_notify() -> Tool:
    def sys_notify(kwargs: Dict[str, Any]) -> str:
        title = str(kwargs.get("title") or "本地编码智能体")
        body = str(kwargs.get("body") or "")
        if not body:
            raise ValueError("缺少必填参数: body")
        if _IS_WINDOWS:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                f"$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                f"$n.BalloonTipTitle={_quote_ps(title)};"
                f"$n.BalloonTipText={_quote_ps(body)};"
                "$n.Visible=$true;$n.ShowBalloonTip(5000);"
                "Start-Sleep -Milliseconds 6000;$n.Dispose();"
            )
            cmd = ["powershell", "-NoProfile", "-Command", script]
        elif shutil.which("notify-send"):
            cmd = ["notify-send", title, body]
        else:
            raise OSError("当前系统无可用的桌面通知通道")
        subprocess.run(cmd, timeout=15, check=False,
                       capture_output=True, creationflags=_no_window())
        return "已发送桌面通知。"

    return Tool(
        name="sys_notify",
        description="发送桌面通知（标题+正文）。不打开文件、不起其他进程。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "通知标题，默认应用名"},
                "body": {"type": "string", "description": "通知正文"},
            },
            "required": ["body"],
        },
        handler=sys_notify,
    )


def _build_sys_open_path(broker: Optional[AccessBroker]) -> Tool:
    def sys_open_path(kwargs: Dict[str, Any]) -> str:
        if broker is None:
            raise PermissionError("未启用本地访问，无法打开工作区外路径")
        root = str(kwargs.get("root") or "").strip()
        rel = str(kwargs.get("path") or "").strip()
        if not rel:
            raise ValueError("缺少必填参数: path")
        p = broker.resolve(root or DEFAULT_OPEN_ROOT, rel)
        if not p.exists():
            raise FileNotFoundError(f"路径不存在: {rel}")
        if _IS_WINDOWS:
            os.startfile(str(p))  # noqa: S606 - 目标已经门闩解析，且该动作需人工确认
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)], check=False, timeout=10)
        else:
            subprocess.run(["xdg-open", str(p)], check=False, timeout=10)
        return f"已用系统默认程序打开: {rel}"

    return Tool(
        name="sys_open_path",
        description="用系统默认程序打开已授权根内的文件或目录。需要用户确认。",
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "访问根名（可选，缺省用配置根）"},
                "path": {"type": "string", "description": "相对该根的文件/目录路径"},
            },
            "required": ["path"],
        },
        handler=sys_open_path,
    )


def _build_sys_launch(allowed: Dict[str, str]) -> Tool:
    def sys_launch(kwargs: Dict[str, Any]) -> str:
        name = str(kwargs.get("app") or "").strip()
        if not name:
            raise ValueError("缺少必填参数: app")
        if name not in allowed:
            known = ", ".join(allowed) or "(空)"
            raise PermissionError(f"应用 {name} 不在启动白名单内（可启动: {known}）")
        target = allowed[name]
        if _IS_WINDOWS:
            os.startfile(target)
        else:
            subprocess.Popen([target], close_fds=True)
        return f"已启动应用: {name}"

    return Tool(
        name="sys_launch",
        description="启动白名单内的应用（不接受任意命令行）。需要用户确认。",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "白名单中的应用名"},
            },
            "required": ["app"],
        },
        handler=sys_launch,
    )


def _quote_ps(text: str) -> str:
    escaped = str(text).replace("'", "''")
    return f"'{escaped}'"


def _no_window() -> int:
    """Windows 下隐藏子进程窗口（避免每次通知闪一个控制台）。"""
    if _IS_WINDOWS:
        return subprocess.CREATE_NO_WINDOW
    return 0


def _build_sys_screenshot(workspace) -> Tool:
    """
    截图落盘到**工作区**（不落外部根：降低隐私外泄面）。

    零依赖实现：Windows 用 PowerShell 的 System.Drawing CopyFromScreen。
    本动作是 L4（默认关闭），只有 allow_actions 显式含 screenshot 才注册。
    """
    def sys_screenshot(kwargs: Dict[str, Any]) -> str:
        if workspace is None:
            raise PermissionError("当前环境没有工作区，无法保存截图")
        rel = str(kwargs.get("path") or "screenshot.png").strip()
        target = workspace.resolve(rel)
        if target.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("截图只支持 .png/.jpg 扩展名")
        if not _IS_WINDOWS:
            raise OSError("当前系统不提供零依赖截图通道")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
            f"$bmp.Save({_quote_ps(str(target))},"
            f"[System.Drawing.Imaging.ImageFormat]::{target.suffix.lower()[1:]});"
            "$g.Dispose();$bmp.Dispose();"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=25, check=False, capture_output=True,
            creationflags=_no_window())
        if not target.is_file():
            detail = ""
            try:
                detail = (result.stderr or result.stdout).decode("gbk", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise OSError(f"截图失败: {detail or '无输出'}")
        return (f"已截图保存到工作区 {workspace.relpath(target)}"
                f"（{target.stat().st_size} 字节）。")

    return Tool(
        name="sys_screenshot",
        description=("对当前屏幕截图并保存到工作区（默认 screenshot.png，仅 .png/.jpg）。"
                     "隐私极敏感，需要用户确认。"),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内的保存路径，默认 screenshot.png"},
            },
        },
        handler=sys_screenshot,
    )


DEFAULT_OPEN_ROOT = "_default"

#: 只读组全部可用成员 → 构造器。expose 配置做白名单过滤。
_READ_BUILDERS: Dict[str, Callable[[], Tool]] = {
    "overview": _build_sys_overview,
    "processes": _build_sys_processes,
    "disks": _build_sys_disks,
    "network": _build_sys_network,
    "env": _build_sys_env,
    "battery": _build_sys_battery,
    "hardware": _build_sys_hardware,
    "installed_apps": _build_sys_installed_apps,
}

#: 默认安全子集（services/clipboard/windows 需额外依赖或隐私敏感，不默认开放）
DEFAULT_EXPOSE = ("overview", "processes", "disks", "network", "env",
                  "battery", "hardware")


def register_system_tools(registry: ToolRegistry,
                          section: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    注册系统只读组。psutil 缺失或 system.enabled=false 时返回空列表并打印警告。

    返回实际注册的工具名（供调用方决定是否把 local_system 技能并入提示词）。
    """
    if not psutil_available:
        print("[系统数据] 警告: psutil 未安装，sys_* 只读工具不注册（对话继续）")
        return []
    expose = list((section or {}).get("expose") or DEFAULT_EXPOSE)
    names: List[str] = []
    for key in expose:
        builder = _READ_BUILDERS.get(str(key))
        if builder is None:
            print(f"[系统数据] 警告: 未知 expose 项 {key}，已跳过")
            continue
        tool = builder()
        registry.register(tool)
        names.append(tool.name)
    return names


def register_system_action_tools(registry: ToolRegistry,
                                 section: Optional[Dict[str, Any]] = None,
                                 broker: Optional[AccessBroker] = None,
                                 workspace: Any = None) -> List[str]:
    """
    注册受控系统动作组。`allow_actions` 白名单默认空 → 什么都不注册。

    切片 5（审批通道）完成前，装配层不应把任何动作放进白名单。
    """
    allowed_actions = [str(a).strip() for a in (section or {}).get("allow_actions") or []]
    allowed_actions = [a for a in allowed_actions if a]
    names: List[str] = []

    if "notify" in allowed_actions:
        registry.register(_build_sys_notify())
        names.append("sys_notify")
    if "open_path" in allowed_actions:
        registry.register(_build_sys_open_path(broker))
        names.append("sys_open_path")
    if "launch" in allowed_actions:
        apps = (section or {}).get("allow_apps") or {}
        registry.register(_build_sys_launch(apps))
        names.append("sys_launch")
    if "screenshot" in allowed_actions:
        registry.register(_build_sys_screenshot(workspace))
        names.append("sys_screenshot")
    return names