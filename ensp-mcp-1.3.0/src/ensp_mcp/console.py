"""通过 eNSP 的 console 端口 telnet 进设备，跑华为 VRP 命令。

eNSP 把每台有 console 的设备映射到本机一个 TCP 端口（拓扑里的
``com_port``，默认从 2000 起），telnet 上去就是设备的 console。

VRP 和思科的差别都在这个模块里消化掉：

* 提示符是 ``<主机名>``（用户视图）和 ``[主机名]``（系统视图、接口视图等），
  不是 ``hostname#``。
* 关分页是 ``screen-length 0 temporary``，不是 ``terminal length 0``。
* 输出长了会停在 ``---- More ----`` 等空格，且用退格把这行擦掉。
* ``save`` 之类的命令会追问 ``[Y/N]``，不回答就一直挂着。
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import math
import re
import time
from dataclasses import dataclass, field

import telnetlib3

DEFAULT_HOST = "127.0.0.1"
# 连接空闲这么久就回收，免得占着 eNSP 的 console
IDLE_TIMEOUT = 300.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MORE_RE = re.compile(r"-{2,}\s*More\s*-{2,}")
# <Huawei> 用户视图 / [Huawei] 系统视图 / [Huawei-GigabitEthernet0/0/1] 接口视图
# CE 系列在有未提交改动时会写成 [~HUAWEI] 或 [*HUAWEI]
_PROMPT_RE = re.compile(r"(?:\A|[\r\n])[ \t]*([<\[])([~*]?)([^\r\n<>\[\]]{1,80})([>\]])[ \t]*\Z")
_CONFIRM_RE = re.compile(
    r"(?:\[\s*Y/N\s*\]|\(\s*y/n\s*\)\s*\[[yn]\]|\[\s*yes/no\s*\])\s*:?\s*\Z", re.I
)
_USERNAME_RE = re.compile(r"(?:^|[\r\n])\s*(?:Username|Login)\s*:\s*\Z", re.I)
_PASSWORD_RE = re.compile(r"(?:^|[\r\n])\s*(?:Please\s+)?(?:Enter\s+)?Password\s*:\s*\Z", re.I)
_PRESS_ENTER_RE = re.compile(r"Press\s+(?:ENTER|RETURN)", re.I)

# 这些命令天生就慢，等待上限单独放宽
_SLOW_COMMANDS = {
    "ping": 25.0,
    "tracert": 40.0,
    "traceroute": 40.0,
    "save": 20.0,
    "reset": 15.0,
    "display current-configuration": 20.0,
    "display saved-configuration": 20.0,
}


class ConsoleError(RuntimeError):
    """连不上或者命令跑不下去。"""

    def __init__(self, message: str, code: str = "console_error", partial_output: str = ""):
        super().__init__(message)
        self.code = code
        self.partial_output = partial_output


_AUTH_FAILURE_RE = re.compile(r"authentication fail|login (?:fail|incorrect)|invalid (?:password|username)|password (?:is )?(?:incorrect|wrong)|username or password", re.I)
_CHANGE_RE = re.compile(r"(?:change|modify|configure|set)[^\r\n]{0,80}password|password[^\r\n]{0,80}(?:change|expired|modify)", re.I)
_NEW_PASSWORD_RE = re.compile(r"(?:new|set|configure)[^\r\n]{0,80}password[^\r\n]*:\s*\Z", re.I)
_REPEAT_PASSWORD_RE = re.compile(r"(?:confirm|repeat|reenter|re-enter)[^\r\n]*password[^\r\n]*:\s*\Z", re.I)
_OLD_PASSWORD_RE = re.compile(r"(?:old|current)[^\r\n]*password[^\r\n]*:\s*\Z", re.I)
_SAVE_FILENAME_RE = re.compile(r"(?:file\s*name|filename)[^\r\n]*\[[^\r\n\]]+\]\s*:\s*\Z", re.I)
MAX_OUTPUT = 8 * 1024 * 1024


def validate_credentials(username: str = "", password: str = "", new_password: str = "") -> tuple[str, str] | None:
    for value in (username, password, new_password):
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError("登录凭据必须是单行文本，不能包含控制字符。")
    if username and not password:
        raise ValueError("给 username 时必须同时给 password。")
    return (username, password) if username or password else None


def clean_output(text: str) -> str:
    """把回显里的控制字符、分页标记、退格擦除处理干净。"""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # VRP 用退格擦掉 "---- More ----"，先把退格真正应用一遍
    if "\x08" in text:
        buffer: list[str] = []
        for char in text:
            if char == "\x08":
                if buffer and buffer[-1] != "\n":
                    buffer.pop()
            else:
                buffer.append(char)
        text = "".join(buffer)

    text = _MORE_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def strip_echo(output: str, command: str) -> str:
    """去掉设备把命令原样回显的那一行。"""
    lines = output.split("\n")
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    return "\n".join(lines).strip("\n")


def strip_prompt(output: str) -> str:
    """去掉结尾那个提示符。"""
    match = _PROMPT_RE.search(output)
    if match:
        return output[: match.start()].rstrip("\n")
    return output


@dataclass
class Prompt:
    raw: str
    hostname: str
    view: str  # user / system / sub / unknown

    @classmethod
    def parse(cls, text: str, hostname: str | None = None) -> "Prompt | None":
        match = _PROMPT_RE.search(text)
        if not match:
            return None
        opener, _flag, body, closer = match.groups()
        if (opener, closer) not in (("<", ">"), ("[", "]")) or body.lower() in ("y/n", "yes/no", "n", "y"):
            return None
        if opener == "<":
            view = "user"
            hostname = body
        elif hostname and body.startswith(hostname + "-"):
            view = "sub"
        elif hostname and body == hostname:
            view = "system"
        else:
            sub = re.search(r"-(?:GigabitEthernet|Ethernet|GE\d|Eth\d|Vlanif|vlan\d|aaa|ospf|bgp|isis|user-interface|Wlan|wlan|Eth-Trunk)", body, re.I)
            view = "sub" if sub else "system"
            hostname = body[:sub.start()] if sub else body
        return cls(raw=match.group(0).strip(), hostname=hostname, view=view)


@dataclass
class Console:
    host: str
    port: int
    reader: telnetlib3.TelnetReader | None = None
    writer: telnetlib3.TelnetWriter | None = None
    prompt: Prompt | None = None
    last_used: float = field(default_factory=time.monotonic)
    pending_changes: bool = False

    @property
    def alive(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    # -- 底层收发 -----------------------------------------------------------

    async def _drain_idle(self, idle: float = 0.35, cap: float = 3.0) -> str:
        """一直读到设备安静下来。"""
        assert self.reader is not None
        chunks: list[str] = []
        deadline = time.monotonic() + cap
        while time.monotonic() < deadline:
            try:
                data = await asyncio.wait_for(self.reader.read(4096), timeout=idle)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
        return "".join(chunks)

    def _write(self, text: str) -> None:
        assert self.writer is not None
        self.writer.write(text)

    # -- 建链 ---------------------------------------------------------------

    async def open(self, timeout: float = 8.0, credentials: tuple[str, str] | None = None, new_password: str = "") -> None:
        validate_credentials(*(credentials or ("", "")), new_password)
        if not 1 <= self.port <= 65535:
            raise ValueError("console 端口必须在 1–65535 之间。")
        try:
            self.reader, self.writer = await asyncio.wait_for(
                telnetlib3.open_connection(self.host, self.port, encoding="utf-8"),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ConsoleError(
                f"连接 {self.host}:{self.port} 超时。确认 eNSP 已启动、"
                f"该设备已开机（图标变绿）。"
            ) from exc
        except OSError as exc:
            raise ConsoleError(
                f"连不上 {self.host}:{self.port}（{exc}）。这个端口对应的设备可能没开机。"
            ) from exc

        try:
            await self._authenticate(credentials, new_password, max(timeout, 20.0))
            await self.disable_paging()
            self.last_used = time.monotonic()
        except BaseException:
            await self.close()
            raise

    async def _authenticate(self, credentials: tuple[str, str] | None, new_password: str, timeout: float) -> None:
        assert self.reader is not None and self.writer is not None
        username, password = credentials or ("", "")
        deadline = time.monotonic() + timeout
        buffer = ""
        wakeups = 0
        answers = 0
        password_sent = False
        changing = False
        while time.monotonic() < deadline:
            try:
                data = await asyncio.wait_for(self.reader.read(4096), min(0.5, max(0.001, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                if not buffer.strip() and wakeups < 3:
                    self._write("\r\n")
                    await self.writer.drain()
                    wakeups += 1
                continue
            if not data:
                raise ConsoleError("设备在登录过程中断开连接。", "disconnected")
            buffer += data
            tail = clean_output(buffer)
            if len(buffer) > MAX_OUTPUT:
                raise ConsoleError("设备登录输出过长。", "output_limit")
            if _AUTH_FAILURE_RE.search(tail):
                raise ConsoleError("设备拒绝登录，请检查凭据。", "authentication_failed")
            response = None
            if _CONFIRM_RE.search(tail):
                if not (_CHANGE_RE.search(tail) or changing):
                    raise ConsoleError("登录遇到不支持的交互提问，请在 console 中完成后重连。", "interactive_prompt")
                if not new_password:
                    raise ConsoleError("设备要求修改初始密码，请提供 new_password 或先手动完成。", "password_change_required")
                response, changing = "Y", True
            elif _REPEAT_PASSWORD_RE.search(tail) or _NEW_PASSWORD_RE.search(tail):
                if not new_password:
                    raise ConsoleError("设备要求设置新密码，请提供 new_password。", "password_change_required")
                response, changing = new_password, True
            elif _OLD_PASSWORD_RE.search(tail):
                if not password:
                    raise ConsoleError("设备要求当前密码，请提供 password。", "authentication_required")
                response = password
            elif _USERNAME_RE.search(tail):
                if not username:
                    raise ConsoleError("设备要求账号密码，请提供 username 和 password。", "authentication_required")
                if password_sent:
                    raise ConsoleError("设备重新要求登录，认证未成功。", "authentication_failed")
                response = username
            elif _PASSWORD_RE.search(tail):
                if changing or _CHANGE_RE.search(tail):
                    if not new_password:
                        raise ConsoleError("设备要求设置新密码，请提供 new_password。", "password_change_required")
                    response, changing = new_password, True
                else:
                    if not password:
                        raise ConsoleError("设备要求密码，请提供 password。", "authentication_required")
                    if password_sent:
                        raise ConsoleError("设备重复要求密码，认证未成功。", "authentication_failed")
                    response, password_sent = password, True
            elif _PRESS_ENTER_RE.search(tail):
                response = ""
            else:
                prompt = Prompt.parse(tail)
                if prompt:
                    self.prompt = prompt
                    self.pending_changes = prompt.raw.startswith("[*")
                    return
            if response is not None:
                answers += 1
                if answers > 16:
                    raise ConsoleError("设备登录交互次数过多，请手动检查。", "authentication_failed")
                self._write(response + "\r\n")
                await self.writer.drain()
                buffer = ""
        raise ConsoleError("等待设备登录提示符超时。", "login_timeout")

    async def disable_paging(self) -> None:
        """关掉分页，后面读长输出就不用一直喂空格了。"""
        await self.send("screen-length 0 temporary", timeout=6.0)

    # -- 执行命令 -----------------------------------------------------------

    async def send(
        self,
        command: str,
        timeout: float | None = None,
        confirm: str = "Y",
    ) -> str:
        """发一条命令，读到提示符为止，自动翻页和回答 [Y/N]。"""
        if not self.alive:
            raise ConsoleError("连接已经断开，需要重新连接。")
        assert self.reader is not None and self.writer is not None

        limit = timeout if timeout is not None else _timeout_for(command)
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("timeout 必须是正的有限数值。")
        if any(ord(c) < 32 for c in command) or not command.strip():
            raise ValueError("每条 command 必须是非空单行文本。")
        if confirm.upper() not in ("Y", "N", "YES", "NO"):
            raise ValueError("confirm 只能是 Y / N / YES / NO。")
        raw = ""
        hostname = self.prompt.hostname if self.prompt else None
        async def exchange():
            nonlocal raw
            self._write(command + "\r\n")
            await self.writer.drain()
            handled = 0
            while True:
                data = await self.reader.read(4096)
                if not data:
                    raise ConsoleError("设备连接断开，命令未确认完成。", "disconnected")
                raw += data
                if len(raw) > MAX_OUTPUT:
                    raise ConsoleError("命令输出超过 8 MiB，已关闭连接。", "output_limit")
                tail = clean_output(raw)
                unseen = clean_output(raw[handled:])
                if _CONFIRM_RE.search(unseen):
                    self._write(confirm + "\r\n")
                    handled = len(raw)
                elif command.strip().lower() == "save" and _SAVE_FILENAME_RE.search(unseen):
                    self._write("\r\n")
                    handled = len(raw)
                elif _USERNAME_RE.search(tail) or _PASSWORD_RE.search(tail):
                    raise ConsoleError("会话已返回登录状态，请重新连接。", "authentication_required")
                else:
                    found = Prompt.parse(tail, hostname)
                    if found:
                        self.prompt = found
                        if found.raw.startswith("[*"):
                            self.pending_changes = True
                        elif found.raw.startswith("[~"):
                            self.pending_changes = False
                        self.last_used = time.monotonic()
                        return strip_echo(strip_prompt(tail), command)
                    if _MORE_RE.search(raw[handled:]):
                        self._write(" ")
                        handled = len(raw)
                    else:
                        continue
                await self.writer.drain()
        try:
            return await asyncio.wait_for(exchange(), timeout=limit)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise ConsoleError("命令超时，执行结果未确认；已关闭连接以避免回显串入下一条命令。", "command_timeout", strip_echo(clean_output(raw), command)) from exc
        except ConsoleError as exc:
            exc.partial_output = strip_echo(clean_output(raw), command)
            await self.close()
            raise
        except OSError as exc:
            await self.close()
            raise ConsoleError("命令传输失败，连接已关闭。", "disconnected", strip_echo(clean_output(raw), command)) from exc
        except asyncio.CancelledError:
            await self.close()
            raise

    async def send_many(self, commands: list[str], confirm: str = "Y", stop_on_error: bool = False) -> list[dict]:
        results = []
        for command in commands:
            text = command.strip()
            if not text or text.startswith("#"):
                continue
            try:
                output = await self.send(text, confirm=confirm)
            except ConsoleError as exc:
                results.append({"command": text, "output": exc.partial_output, "view": "unknown", "error": str(exc), "errorCode": exc.code})
                break
            results.append(
                {
                    "command": text,
                    "output": output,
                    "view": self.prompt.view if self.prompt else "unknown",
                    "error": _error_in(output),
                }
            )
            if results[-1]["error"] and stop_on_error:
                break
        return results

    # -- 视图切换 -----------------------------------------------------------

    async def to_user_view(self) -> None:
        """退回 ``<主机名>`` 用户视图。"""
        for _ in range(6):
            if self.prompt and self.prompt.view == "user":
                return
            await self.send("return", timeout=5.0)
            if self.prompt and self.prompt.view == "user":
                return
            await self.send("quit", timeout=5.0)
        raise ConsoleError("退不回用户视图，设备可能卡在某个交互式提问上。")

    async def to_system_view(self) -> None:
        """进到 ``[主机名]`` 系统视图。"""
        if self.prompt and self.prompt.view == "system":
            return
        await self.to_user_view()
        await self.send("system-view", timeout=6.0)
        if not (self.prompt and self.prompt.view in ("system", "sub")):
            raise ConsoleError("进不了系统视图。")

    async def save_config(self) -> str:
        if self.pending_changes:
            await self.to_system_view()
            committed = await self.send("commit", timeout=25.0)
            if _error_in(committed):
                raise ConsoleError("提交配置失败：" + _error_in(committed), "commit_failed", committed)
        await self.to_user_view()
        output = await self.send("save", timeout=25.0, confirm="Y")
        if _error_in(output):
            raise ConsoleError("保存配置失败：" + _error_in(output), "save_failed", output)
        if not re.search(r"(?:saved|saving|save)[^\r\n]*(?:success|complete)|success[^\r\n]*(?:saved|saving|save)|保存成功", output, re.I):
            raise ConsoleError("设备返回提示符，但未识别到保存成功信息，请读取 saved-configuration 核对。", "save_unconfirmed", output)
        return output

    async def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = None
        self.writer = None
        self.prompt = None


def _timeout_for(command: str) -> float:
    lowered = command.strip().lower()
    for prefix, limit in _SLOW_COMMANDS.items():
        if lowered.startswith(prefix):
            return limit
    return 8.0


_ERROR_MARKERS = (
    "Error:",
    "Wrong parameter",
    "Unrecognized command",
    "Incomplete command",
    "Too many parameters",
    "% Unknown",
    "Failure:",
    "Failed",
)


def _error_in(output: str) -> str | None:
    for line in output.split("\n"):
        stripped = line.strip()
        for marker in _ERROR_MARKERS:
            if stripped.startswith(marker) or marker in stripped[:40]:
                return stripped
    return None


# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------


class ConsolePool:
    """按 host:port 复用连接，省掉每次工具调用的重连开销。"""

    def __init__(self) -> None:
        self._consoles: dict[tuple[str, int], Console] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None

    def _lock(self, key: tuple[str, int]) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def acquire(
        self,
        port: int,
        host: str = DEFAULT_HOST,
        credentials: tuple[str, str] | None = None,
        new_password: str = "",
    ) -> Console:
        key = (host, port)
        console = self._consoles.get(key)
        if console is not None:
            expired = time.monotonic() - console.last_used > IDLE_TIMEOUT
            if console.alive and not expired:
                return console
            await console.close()
            self._consoles.pop(key, None)

        console = Console(host=host, port=port)
        await console.open(credentials=credentials, new_password=new_password)
        self._consoles[key] = console
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())
        return console

    def lock_for(self, port: int, host: str = DEFAULT_HOST) -> asyncio.Lock:
        return self._lock((host, port))

    def sessions(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "host": host,
                "port": port,
                "hostname": console.prompt.hostname if console.prompt else None,
                "view": console.prompt.view if console.prompt else "unknown",
                "idleSeconds": round(now - console.last_used, 1),
                "alive": console.alive,
            }
            for (host, port), console in sorted(self._consoles.items())
        ]

    async def close(self, port: int, host: str = DEFAULT_HOST) -> bool:
        async with self._lock((host, port)):
            console = self._consoles.pop((host, port), None)
            if console is None:
                return False
            await console.close()
            return True

    async def close_all(self) -> int:
        count = 0
        for host, port in list(self._consoles):
            count += await self.close(port, host)
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        return count

    async def reap_expired(self) -> int:
        count = 0
        for key in list(self._consoles):
            lock = self._lock(key)
            if lock.locked():
                continue
            async with lock:
                device = self._consoles.get(key)
                if device and (not device.alive or time.monotonic() - device.last_used > IDLE_TIMEOUT):
                    await device.close()
                    self._consoles.pop(key, None)
                    count += 1
        return count

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.reap_expired()


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------

_PING_STATS_RE = re.compile(
    r"(\d+)\s+packet\(s\)\s+transmitted.*?(\d+)\s+packet\(s\)\s+received"
    r".*?([\d.]+)%\s*packet\s*loss",
    re.S | re.I,
)
_PING_RTT_RE = re.compile(
    r"round-trip\s+min/avg/max\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)", re.I
)


def parse_ping(output: str) -> dict:
    """把 VRP 的 ping 回显解析成结构化结果。"""
    result: dict = {"raw": output, "transmitted": None, "received": None, "lossPercent": None}
    stats = _PING_STATS_RE.search(output)
    if stats:
        result["transmitted"] = int(stats.group(1))
        result["received"] = int(stats.group(2))
        result["lossPercent"] = float(stats.group(3))
    rtt = _PING_RTT_RE.search(output)
    if rtt:
        result["rttMs"] = {
            "min": float(rtt.group(1)),
            "avg": float(rtt.group(2)),
            "max": float(rtt.group(3)),
        }
    if result["received"] is not None:
        result["reachable"] = result["received"] > 0
    else:
        result["reachable"] = True if "Reply from" in output else None
    result["parsed"] = stats is not None
    return result


def normalise_config(text: str) -> list[str]:
    """把配置文本整理成可比较的行，去掉时间戳之类每次都变的内容。"""
    skip_prefixes = (
        "#",
        "!",
        "Now current time",
    )
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if any(line.strip().startswith(p) for p in skip_prefixes):
            continue
        if line.strip() == "return":
            continue
        lines.append(line)
    return lines


def compare_configs(baseline: str, live: str) -> dict:
    before, after = normalise_config(baseline), normalise_config(live)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    missing, added = [], []
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            missing.extend(before[i:j])
        if tag in ("replace", "insert"):
            added.extend(after[k:l])
    return {"identical": before == after, "onlyInFile": missing, "onlyOnDevice": added,
            "unifiedDiff": "\n".join(difflib.unified_diff(before, after, fromfile="baseline", tofile="device", lineterm=""))}
