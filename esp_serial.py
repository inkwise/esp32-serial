#!/usr/bin/env python3
"""esp_serial.py — Termux 上的 ESP32 / USB 串口工具。

定位：**桥接 App（TCPUART 等）的 Termux 侧客户端**，同时内置 socat 的替代品，
所以一条 ``python esp_serial.py pty`` 就能给 esptool / mpremote / minicom
提供稳定的 ``/dev/pts/N`` 设备节点，不用再装 socat。

子命令一览
----------
list       列出 termux-usb 设备 + 扫本地桥接端口
scan       扫本地 TCP 端口，找 TCPUART 之类的桥接服务
doctor     环境体检（termux-api / libusb / pyserial / esptool / 端口）
mon        只读监听，带时间戳 / hexdump / 落盘
repl       交互式终端（Ctrl-] 退出，可本地回显）
send       发送文本 / 文件 / 十六进制
exec       发一条命令，等回显，按退出码判断（脚本友好）
pty        把 TCP 桥接端点变成 /dev/pts/N（稳定符号链接），socat 替代
flash      用 esptool 直连 socket://host:port 烧写（自动补 no-reset）
usb        usb list / usb identify：读 USB 描述符（VID:PID、芯片型号、端点）

设计要点
--------
* 只依赖 CPython 标准库；pyserial / esptool / libusb 都是可选的。
* 波特率、数据位、流控在手机侧的桥接 App 里设置，Termux 这侧只搬字节。
* TCPUART 这类 App 提供的 socket 不能改 DTR/RTS，所以烧写必须手动进下载模式，
  ``flash`` 子命令会自动加 ``--before no-reset --after no-reset``。
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import termios
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VERSION = "1.0.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080  # TCPUART 的默认端口；社区教程普遍用 8080
CONNECT_TIMEOUT = 5.0

# 常见的桥接端口（TCPUART / Serial Bridge / ser2net 默认值都收进来）
CANDIDATE_PORTS = (8080, 1234, 4567, 8000, 8888, 5000, 9000, 7000, 3333, 12345, 23)

ENTER_SEQ = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n", "none": b""}

EXAMPLES = """\
常用示例
--------
  # 1) 设备连上手机 + TCPUART 点了 Start（波特率 115200，端口 8080）后：
  python esp_serial.py list                     # 确认 USB 设备 + 8080 在听
  python esp_serial.py mon --stamp              # 看 ESP32 启动日志（Ctrl-C 退出）
  python esp_serial.py mon --hex --log esp.log  # 十六进制看，同时落盘

  # 2) 交互：micropython REPL / AT 指令
  python esp_serial.py repl                     # Ctrl-C 或 Ctrl-] 退出
  python esp_serial.py repl --forward-ctrl-c    # 让 Ctrl-C 转发给板子（中断 micropython）

  # 3) 发一条命令并抓回显（脚本里用退出码判断）
  python esp_serial.py exec 'print(1+1)' --until '>>>'

  # 4) 给 esptool / mpremote / minicom 一个稳定的串口节点（不需要 socat）
  python esp_serial.py pty --link-file ~/esp32 &
  esptool --port ~/esp32 --before no-reset --after no-reset write-flash -z 0x1000 fw.bin
  mpremote connect port:$HOME/esp32 repl

  # 5) 直接让本工具调 esptool（自动补 --before/--after no-reset）
  python esp_serial.py flash -- write-flash -z 0x1000 fw.bin

  # 6) 看板子到底是哪颗 USB 芯片（会弹一次授权框）
  python esp_serial.py usb identify

  # 说明：没指定端点时，若 8080 没有桥接服务但插着 USB 设备，
  #       会自动回退成 --usb 直连，所以上面所有命令裸着敲也行。

  # 7) 板子是 ESP32-S2/S3/C3 原生 USB（VID:PID 303a:xxxx）时，绕过桥接 App 直连：
  python esp_serial.py mon --usb --stamp           # 直接读，不用装 TCPUART
  python esp_serial.py repl --usb                  # 直接交互
  python esp_serial.py pty --usb --link-file ~/esp32 &   # 给 esptool/mpremote 用
"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

class LinkClosed(Exception):
    """链路已关闭。"""


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, flush=True, **kwargs)


def ts_str() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KiB"
    return f"{n / 1024 / 1024:.1f}MiB"


def disp_width(text: str) -> int:
    """终端显示宽度：CJK/全角字符占两列。"""
    width = 0
    for ch in text:
        code = ord(ch)
        wide = (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
        )
        width += 2 if wide else 1
    return width


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


def hexdump_lines(data: bytes, base_offset: int = 0, width: int = 16):
    """生成经典 hexdump 文本行。"""
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        hexpart = f"{hexpart:<{width * 3 - 1}}"
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        yield f"{base_offset + i:08x}  {hexpart}  |{text}|"


# --------------------------------------------------------------------------- #
# 链路抽象：TCP / 已有 fd / 字符设备
# --------------------------------------------------------------------------- #

def make_raw(fd: int) -> None:
    """把 tty 设成 8N1 原始模式（不处理回车、不回显、不做流控）。"""
    try:
        attrs = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
        | termios.IXOFF
        | termios.IXANY
    )
    oflag &= ~termios.OPOST
    lflag &= ~(
        termios.ECHO
        | termios.ECHONL
        | termios.ICANON
        | termios.ISIG
        | termios.IEXTEN
    )
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])


class Link:
    """统一读写接口。

    ``read(timeout)`` 语义：
      * 返回 ``bytes``：有数据
      * 返回 ``b""``：超时，暂时没数据
      * 返回 ``None``：对端关闭 / 连接断开
    """

    kind = "link"

    def can_select(self) -> bool:
        """该链路有没有可 select 的 fd（USB 直连没有）。"""
        return True

    def read(self, timeout: float = 0.2) -> bytes | None:
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError

    def fileno(self) -> int:
        raise NotImplementedError

    def describe(self) -> str:
        return self.kind

    def close(self) -> None:
        pass


class TcpLink(Link):
    kind = "tcp"

    def __init__(self, host: str, port: int, timeout: float = CONNECT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def open(self) -> "TcpLink":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = sock
        return self

    def read(self, timeout: float = 0.2) -> bytes | None:
        if self.sock is None:
            return None
        try:
            ready, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return b""
        try:
            data = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            return None
        return data or None

    def write(self, data: bytes) -> int:
        if self.sock is None:
            raise LinkClosed("TCP 连接未建立")
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise LinkClosed(f"写入失败：{exc}") from exc
        return len(data)

    def fileno(self) -> int:
        if self.sock is None:
            raise LinkClosed("TCP 连接未建立")
        return self.sock.fileno()

    def describe(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class FdLink(Link):
    """包装一个已有 fd（termux-usb 给的 fd、pty master 等）或一个字符设备路径。"""

    kind = "fd"

    def __init__(self, fd: int, name: str | None = None, close_fd: bool = True):
        self.fd = fd
        self.name = name or f"fd:{fd}"
        self._close_fd = close_fd
        try:
            self.is_tty = os.isatty(fd)
        except OSError:
            self.is_tty = False
        if self.is_tty:
            make_raw(fd)

    @classmethod
    def open_path(cls, path: str) -> "FdLink":
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            raise LinkClosed(f"打不开 {path}：{exc}") from exc
        return cls(fd, name=path)

    def read(self, timeout: float = 0.2) -> bytes | None:
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                return None
            raise
        return data or None

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            try:
                sent += os.write(self.fd, view[sent:])
            except (BlockingIOError, InterruptedError):
                select.select([], [self.fd], [], 1.0)
                continue
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    raise LinkClosed(f"设备已关闭（{self.name}）") from exc
                raise
        return sent

    def fileno(self) -> int:
        return self.fd

    def describe(self) -> str:
        return self.name

    def close(self) -> None:
        if self._close_fd:
            try:
                os.close(self.fd)
            except OSError:
                pass


def connect_tcp(host: str, port: int, retries: int = 0, delay: float = 1.0, verbose: bool = False) -> TcpLink:
    attempts = max(1, retries + 1)
    last: Exception | None = None
    for i in range(attempts):
        try:
            return TcpLink(host, port).open()
        except OSError as exc:
            last = exc
            if i + 1 < attempts:
                if verbose:
                    eprint(f"[esp_serial] 第 {i + 1}/{attempts} 次连接失败：{exc}，{delay:.1f}s 后重试")
                time.sleep(delay)
    raise LinkClosed(
        f"连不上 {host}:{port}（{last}）。检查：TCPUART 是否已点 Start / 端口是否 {DEFAULT_PORT} / "
        f"手机是否锁屏把 App 挂了（可 termux-wake-lock）"
    )


# 命令带 --usb 时会被 termux-usb 包一层重新执行，子进程靠这个全局开关
# 让 open_endpoint() 返回 USB 直连链路而不是 TCP 链路。
_USB_LINK_ACTIVE = False
USB_BAUDRATE = 115200

# termux-usb 会把 -e 子进程的 stdout 换成管道、并且等子进程退出才转发，
# 所以实时输出（mon/repl/pty）要另开一条命名管道接回来。
OUT_FIFO_ENV = "ESP_SERIAL_OUT_FIFO"

# 这些子命令在没有 TCP 桥接时会自动回退到 USB 直连
USB_AUTO_COMMANDS = {"mon", "repl", "send", "exec", "pty", "bridge", "boot", "reset"}


def open_output_stream():
    """子进程取输出流：有 FIFO 就写 FIFO（实时），否则退回 stdout。"""
    path = os.environ.get(OUT_FIFO_ENV)
    if not path:
        return sys.stdout.buffer
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return sys.stdout.buffer
    return os.fdopen(fd, "wb", buffering=0)


def rebind_stdout():
    """把 sys.stdout 指到实时输出流上（print 与 sys.stdout.buffer 都跟着走）。"""
    import io

    stream = open_output_stream()
    if stream is not sys.stdout.buffer:
        sys.stdout = io.TextIOWrapper(
            stream, encoding="utf-8", errors="replace", write_through=True
        )
    return stream


def open_usb_link() -> object:
    """按 TERMUX_USB_FD 建立 USB-CDC 直连链路。"""
    import usb_cdc

    return usb_cdc.from_env(baudrate=USB_BAUDRATE)


def open_endpoint(
    spec: str | None,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    retries: int = 0,
    delay: float = 1.0,
    verbose: bool = False,
):
    """把命令行里的端点写法规整成 Link。

    支持：``8080`` / ``host:8080`` / ``tcp://host:8080`` / ``socket://host:8080``
          / ``fd://3`` / ``/dev/pts/5`` 或任意 ``已存在的路径``；
    处于 ``--usb`` 模式时直接返回 USB-CDC 链路。
    """
    if _USB_LINK_ACTIVE:
        return open_usb_link()
    host = host or DEFAULT_HOST
    if spec:
        spec = spec.strip()
        if spec.startswith("fd://"):
            return FdLink(int(spec[5:]), name=spec, close_fd=False)
        if spec.startswith(("tcp://", "socket://")):
            parsed = urlparse(spec)
            return connect_tcp(
                parsed.hostname or host, parsed.port or port or DEFAULT_PORT, retries, delay, verbose
            )
        if spec.isdigit():
            return connect_tcp(host, int(spec), retries, delay, verbose)
        if spec.startswith("/") or os.path.exists(spec):
            return FdLink.open_path(spec)
        if ":" in spec:
            head, _, tail = spec.rpartition(":")
            if tail.isdigit():
                return connect_tcp(head or host, int(tail), retries, delay, verbose)
        raise SystemExit(
            f"看不懂的端点：{spec}\n"
            "可用形式：8080 / host:8080 / tcp://host:8080 / fd://3 / /dev/pts/5"
        )
    return connect_tcp(host, port or DEFAULT_PORT, retries, delay, verbose)


# --------------------------------------------------------------------------- #
# 输出器：直通 / 时间戳 / hexdump，可 tee 日志
# --------------------------------------------------------------------------- #

class Emitter:
    def __init__(self, out=None, log_path: str | None = None, stamp: bool = False, hex_mode: bool = False):
        self.out = out or sys.stdout.buffer
        self.log = open(os.path.expanduser(log_path), "ab") if log_path else None
        self.stamp = stamp
        self.hex_mode = hex_mode
        self.bytes_in = 0
        self._pending = bytearray()
        self._offset = 0

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_in += len(data)
        if self.log is not None:
            self.log.write(data)
            self.log.flush()
        if self.hex_mode:
            prefix = f"[{ts_str()}] " if self.stamp else ""
            for line in hexdump_lines(data, self._offset):
                self._write((prefix + line + "\n").encode())
            self._offset += len(data)
            return
        if self.stamp:
            self._pending.extend(data)
            while True:
                idx = self._pending.find(b"\n")
                if idx < 0:
                    break
                line = bytes(self._pending[: idx + 1])
                del self._pending[: idx + 1]
                self._emit_line(line)
            return
        self._write(data)

    def _emit_line(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        self._write(f"[{ts_str()}] {text}\n".encode())

    def _write(self, data: bytes) -> None:
        try:
            self.out.write(data)
            self.out.flush()
        except BrokenPipeError:
            raise
        except (ValueError, OSError):
            pass

    def flush_tail(self) -> None:
        if self._pending:
            line = bytes(self._pending)
            self._pending.clear()
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            self._write(f"[{ts_str()}] {text}\n".encode())

    def close(self) -> None:
        self.flush_tail()
        if self.log is not None:
            try:
                self.log.close()
            except OSError:
                pass
            self.log = None


# --------------------------------------------------------------------------- #
# termux-usb 相关
# --------------------------------------------------------------------------- #

def termux_usb_devices(timeout: float = 20.0):
    """``termux-usb -l`` 的设备列表；没有 termux-usb 时返回 None。"""
    exe = shutil.which("termux-usb")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-l"], capture_output=True, timeout=timeout, text=True)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def usb_device_paths(devices) -> list:
    return [d for d in (devices or []) if isinstance(d, str) and d.startswith("/dev/bus/usb/")]


# --------------------------------------------------------------------------- #
# 端口扫描
# --------------------------------------------------------------------------- #

def parse_port_spec(ports_arg: str | None, range_arg: str | None) -> list:
    ports: list = list(CANDIDATE_PORTS)
    if ports_arg:
        ports = []
        for item in ports_arg.split(","):
            item = item.strip()
            if item.isdigit():
                ports.append(int(item))
    elif range_arg:
        ports = []  # 只给了 --range 就只扫这个范围，别混进默认候选
    if range_arg:
        lo, _, hi = range_arg.partition("-")
        if lo.isdigit() and (hi or lo).isdigit():
            start = int(lo)
            end = int(hi) if hi else start
            ports.extend(range(start, end + 1))
    seen, ordered = set(), []
    for p in ports:
        if 0 < p < 65536 and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered or list(CANDIDATE_PORTS)


def probe_port(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_ports(host: str, ports, timeout: float = 0.3, workers: int = 16) -> list:
    if not ports:
        return []
    workers = max(1, min(workers, len(ports)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: (p, probe_port(host, p, timeout)), ports))
    return [p for p, ok in results if ok]


def probe_bridge_data(host: str, port: int, timeout: float = 0.8) -> bytes:
    """连上去看看对面会不会主动吐数据（ESP32 复位时会送启动日志）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                return sock.recv(256)
            except (socket.timeout, OSError):
                return b""
    except OSError:
        return b""


# --------------------------------------------------------------------------- #
# list / scan / doctor
# --------------------------------------------------------------------------- #

def cmd_list(args) -> int:
    devices = termux_usb_devices()
    ports = [] if args.no_scan else scan_ports(
        args.host, parse_port_spec(args.ports, args.range), timeout=args.timeout / 1000.0
    )

    if args.json:
        payload = {
            "termux_usb_available": devices is not None,
            "termux_usb_devices": usb_device_paths(devices),
            "open_ports": ports,
            "host": args.host,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("== termux-usb 设备 ==")
    if devices is None:
        print("  未安装 termux-api？请 pkg install termux-api，并安装 Termux:API App")
    elif not usb_device_paths(devices):
        print("  没有 USB 设备（检查 OTG 转接头 / 数据线是否支持数据传输）")
    else:
        for idx, dev in enumerate(usb_device_paths(devices)):
            print(f"  [{idx}] {dev}")
        print("  提示：VID:PID / 芯片型号要看描述符 → esp_serial.py usb identify（会弹授权框）")

    print()
    print(f"== 本地桥接端口（{args.host}） ==")
    if ports:
        for port in ports:
            tag = "TCPUART 默认端口" if port == DEFAULT_PORT else ""
            print(f"  {args.host}:{port}  在听 {tag}".rstrip())
        port = ports[0]
        print()
        print(f"下一步：python esp_serial.py mon --port {port} --stamp")
    else:
        print("  没发现监听端口。请看 README 的 TCPUART 配置：连接 → 授权 → Server → 端口 8080 → Start")
        print("  也试试：python esp_serial.py scan --range 8000-8100")
        print("  如果板子是 ESP32-S2/S3/C3 的原生 USB（303a:xxxx），不需要桥接 App：")
        print("    python esp_serial.py mon --usb --stamp")
    return 0


def cmd_scan(args) -> int:
    ports = parse_port_spec(args.ports, args.range)
    found = scan_ports(args.host, ports, timeout=args.timeout / 1000.0)

    results = []
    for port in found:
        sample = probe_bridge_data(args.host, port) if args.probe else b""
        results.append({"port": port, "sample": sample})

    if args.json:
        print(
            json.dumps(
                {
                    "host": args.host,
                    "scanned": len(ports),
                    "open": [r["port"] for r in results],
                    "samples": {str(r["port"]): r["sample"].hex() for r in results if args.probe},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"扫描 {args.host} 的 {len(ports)} 个端口 ...")
    if not results:
        print("没找到监听端口。确认桥接 App 已 Start，且是 TCP Server 模式。")
        return 1
    for item in results:
        line = f"  {args.host}:{item['port']}  在听"
        if args.probe:
            sample = item["sample"]
            if sample:
                preview = sample[:60].decode("utf-8", "replace").replace("\r", "\\r").replace("\n", "\\n")
                line += f"  首批数据 {len(sample)}B: {preview}"
            else:
                line += "  （暂时没有主动数据，正常——等板子复位）"
        print(line)
    print()
    print(f"建议：python esp_serial.py mon --port {results[0]['port']} --stamp")
    return 0


def _which(cmd: str):
    return shutil.which(cmd)


def doctor_checks():
    checks = []

    def add(name, ok, detail, fix="", optional=False):
        checks.append(
            {"name": name, "ok": bool(ok), "detail": detail, "fix": fix, "optional": optional}
        )

    add("Python", sys.version_info >= (3, 8), sys.version.split()[0], "pkg install python")

    termux = bool(os.environ.get("PREFIX")) and "com.termux" in (os.environ.get("PREFIX") or "")
    add("Termux 环境", termux, os.environ.get("PREFIX", "未知"), "这个工具是为 Termux 写的")

    usb_exe = _which("termux-usb")
    add("termux-usb", bool(usb_exe), usb_exe or "未找到", "pkg install termux-api && 装 Termux:API App")

    devices = termux_usb_devices()
    if devices is None:
        add("USB 设备枚举", False, "termux-usb -l 跑不起来", "确认 Termux:API App 已安装并给了 USB 权限")
    else:
        paths = usb_device_paths(devices)
        add("USB 设备枚举", bool(paths), f"{len(paths)} 个：{', '.join(paths) or '无'}",
            "插好 OTG + 数据线；换一根支持数据的线")

    try:
        import libusb_probe  # noqa: F401

        lib_path = libusb_probe.available()
    except Exception as exc:  # pragma: no cover
        lib_path = None
        add("libusb_probe 模块", False, str(exc), "确保 libusb_probe.py 与本文件同目录")
    add("libusb", bool(lib_path), lib_path or "未安装", "pkg install libusb",
        optional=True)  # 只影响 usb identify

    try:
        import serial  # type: ignore

        add("pyserial", True, getattr(serial, "__version__", "?"), "pip install pyserial")
    except Exception:
        add("pyserial", False, "未安装（esptool 需要）", "pkg install esptool")

    esptool = _which("esptool") or _which("esptool.py")
    add("esptool", bool(esptool), esptool or "未找到", "pkg install esptool")

    mpremote = _which("mpremote")
    add("mpremote", bool(mpremote), mpremote or "未找到", "pip install mpremote", optional=True)

    socat = _which("socat")
    add("socat", True, socat or "未安装（本工具不需要）", "", optional=True)

    ports = scan_ports(DEFAULT_HOST, list(CANDIDATE_PORTS), timeout=0.25)
    add("本地桥接端口", bool(ports), f"在听：{ports}" if ports else "没发现",
        f"TCPUART: 连接→授权→TCP Server→端口 {DEFAULT_PORT}→Start")

    wakelock = _which("termux-wake-lock")
    add("termux-wake-lock", bool(wakelock), wakelock or "未找到",
        "pkg install termux-api；长任务先 termux-wake-lock")

    return checks


def cmd_doctor(args) -> int:
    checks = doctor_checks()
    if args.json:
        print(json.dumps({"checks": checks, "ok": all(c["ok"] for c in checks)}, ensure_ascii=False, indent=2))
        return 0
    bad = 0
    print("== esp_serial.py 环境体检 ==\n")
    for item in checks:
        if item["ok"]:
            mark = "✓"
        else:
            mark = "·" if item["optional"] else "✗"
        if not item["ok"] and not item["optional"]:
            bad += 1
        name = pad_display(item["name"], 20)
        suffix = "（可选）" if item["optional"] else ""
        print(f"  {mark} {name} {item['detail']}{suffix}")
        if not item["ok"] and item["fix"]:
            print(f"      → {item['fix']}")
    print()
    if bad:
        print(f"有 {bad} 项必需组件待处理（· 是可选项）")
    else:
        print("必需组件齐全，可以开跑：python esp_serial.py mon --stamp")
    return 0


# --------------------------------------------------------------------------- #
# mon
# --------------------------------------------------------------------------- #

def cmd_mon(args) -> int:
    link = open_endpoint(
        args.endpoint,
        args.host,
        args.port,
        retries=args.reconnect_retries,
        verbose=True,
    )
    emitter = Emitter(log_path=args.log, stamp=args.stamp, hex_mode=args.hex)
    pattern = re.compile(args.exit_on.encode()) if args.exit_on else None
    acc = bytearray()
    started = time.monotonic()
    deadline = started + args.duration if args.duration else None
    status = 0

    eprint(f"[esp_serial] 监听 {link.describe()}"
           + (f" → 日志 {args.log}" if args.log else "")
           + ("（Ctrl-C 结束）" if sys.stderr.isatty() else ""))
    try:
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            timeout = 0.2 if deadline is None else max(0.01, min(0.2, deadline - now))
            data = link.read(timeout)
            if data is None:
                if args.reconnect:
                    eprint("[esp_serial] 连接断开，重连中 ...")
                    link.close()
                    try:
                        link = open_endpoint(
                            args.endpoint, args.host, args.port, retries=args.reconnect_retries, verbose=True
                        )
                    except LinkClosed as exc:
                        eprint(f"[esp_serial] {exc}")
                        status = 1
                        break
                    continue
                eprint("[esp_serial] 连接已关闭")
                break
            if data:
                emitter.feed(data)
                if pattern is not None:
                    acc.extend(data)
                    if len(acc) > 65536:
                        del acc[: len(acc) - 65536]
                    if pattern.search(bytes(acc)):
                        eprint(f"[esp_serial] 命中退出条件：{args.exit_on}")
                        break
    except KeyboardInterrupt:
        eprint("")
    finally:
        emitter.close()
        link.close()

    elapsed = time.monotonic() - started
    eprint(f"[esp_serial] 结束：收到 {human_bytes(emitter.bytes_in)}，用时 {elapsed:.1f}s")
    return status


# --------------------------------------------------------------------------- #
# repl
# --------------------------------------------------------------------------- #

def translate_input(data: bytes, enter: bytes, carry: bytes = b"") -> tuple:
    """把本地按键/行尾统一成设备侧的行尾序列，返回 (要发送的字节, 新的 carry)。

    CR、LF、CRLF 一律折算成一个 ``enter``；不缓存结尾的 CR，
    否则单按一次回车（raw 模式下只有一个 \\r 字节）会被吞掉。
    """
    if carry:
        data = carry + data
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if enter == b"":
        return normalized, b""
    return normalized.replace(b"\n", enter), b""


def echo_local(data: bytes, out) -> None:
    buf = bytearray()
    for byte in data:
        if byte in (0x7F, 0x08):
            buf.extend(b"\b \b")
        elif byte in (0x0D, 0x0A):
            buf.extend(b"\r\n")
        elif 32 <= byte < 127:
            buf.append(byte)
    if buf:
        out.write(bytes(buf))
        out.flush()


def _repl_pipe(link: Link, enter: bytes) -> int:
    """stdin 不是 tty 时的退化模式：逐行发送。"""
    carry = b""
    for raw in sys.stdin.buffer:
        payload, carry = translate_input(raw, enter, carry)
        if payload:
            link.write(payload)
        deadline = time.monotonic() + 0.15
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            data = link.read(left)
            if data is None:
                return 0
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                deadline = time.monotonic() + 0.15
    if carry:
        link.write(carry)
    return 0


def cmd_repl(args) -> int:
    link = open_endpoint(args.endpoint, args.host, args.port, retries=args.reconnect_retries, verbose=True)
    enter = ENTER_SEQ[args.enter]
    if not sys.stdin.isatty():
        eprint("[esp_serial] stdin 不是终端，进入逐行发送模式")
        try:
            return _repl_pipe(link, enter)
        finally:
            link.close()

    in_fd = sys.stdin.fileno()
    out = sys.stdout.buffer
    old_attrs = termios.tcgetattr(in_fd)
    carry = b""
    status = 0
    eprint(f"[esp_serial] 已连接 {link.describe()}")
    if args.forward_ctrl_c:
        eprint("[esp_serial] 退出：Ctrl-]  |  Ctrl-C 已设为转发给板子（--forward-ctrl-c）")
    else:
        eprint("[esp_serial] 退出：Ctrl-C 或 Ctrl-]  |  想让 Ctrl-C 转发给板子就加 --forward-ctrl-c")
    import signal

    def _restore_on_signal(signum, frame):
        try:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, old_attrs)
        finally:
            os._exit(130)

    old_term = signal.signal(signal.SIGTERM, _restore_on_signal)
    old_hup = signal.signal(signal.SIGHUP, _restore_on_signal)
    try:
        import tty as tty_mod

        tty_mod.setraw(in_fd)
        selectable = link.can_select()
        while True:
            try:
                if selectable:
                    ready, _, _ = select.select([in_fd, link.fileno()], [], [], 0.2)
                    stdin_ready = in_fd in ready
                    link_ready = link.fileno() in ready
                else:
                    ready, _, _ = select.select([in_fd], [], [], 0.05)
                    stdin_ready = bool(ready)
                    link_ready = True  # USB 没有 fd，靠 read(timeout) 自己等
            except (OSError, ValueError):
                status = 1
                break
            if link_ready:
                data = link.read(0 if selectable else 0.02)
                if data is None:
                    eprint("\r\n[esp_serial] 连接已关闭")
                    status = 1
                    break
                if data:
                    out.write(data)
                    out.flush()
            if stdin_ready:
                chunk = os.read(in_fd, 4096)
                if not chunk:
                    break
                if b"\x03" in chunk and not args.forward_ctrl_c:  # Ctrl-C 退出
                    head, _, _ = chunk.partition(b"\x03")
                    if head:
                        payload, carry = translate_input(head, enter, carry)
                        if payload:
                            link.write(payload)
                    eprint("\r\n[esp_serial] 已退出（Ctrl-C）")
                    break
                if b"\x1d" in chunk:  # Ctrl-]
                    head, _, _ = chunk.partition(b"\x1d")
                    if head:
                        payload, carry = translate_input(head, enter, carry)
                        if payload:
                            link.write(payload)
                    eprint("\r\n[esp_serial] 退出")
                    break
                payload, carry = translate_input(chunk, enter, carry)
                if payload:
                    link.write(payload)
                if args.local_echo:
                    echo_local(chunk, out)
    except KeyboardInterrupt:
        status = 0
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGHUP, old_hup)
        link.close()
    return status


# --------------------------------------------------------------------------- #
# send / exec
# --------------------------------------------------------------------------- #

def build_payload(args) -> bytes:
    if getattr(args, "hex_data", None):
        cleaned = re.sub(r"[\s:_-]", "", args.hex_data)
        data = bytes.fromhex(cleaned)
    elif getattr(args, "file", None):
        with open(os.path.expanduser(args.file), "rb") as handle:
            data = handle.read()
    elif getattr(args, "text", None) is not None:
        text = args.text
        data = sys.stdin.buffer.read() if text == "-" else text.encode()
    elif getattr(args, "cmd", None):
        data = " ".join(args.cmd).encode()
    else:
        data = b""

    append = args.append
    if append is None:
        # 文本/命令默认补 CR；文件与十六进制是二进制载荷，默认原样发
        binary = bool(getattr(args, "file", None) or getattr(args, "hex_data", None))
        append = "none" if binary else "cr"
    return data + ENTER_SEQ[append]


def write_in_chunks(link: Link, payload: bytes, chunk: int = 0, delay: float = 0.0) -> None:
    if chunk and chunk > 0 and len(payload) > chunk:
        for i in range(0, len(payload), chunk):
            link.write(payload[i : i + chunk])
            if delay:
                time.sleep(delay)
    else:
        link.write(payload)


def capture_until(link: Link, pattern_bytes, timeout: float, idle: float = 0.0, settle: float = 0.05) -> tuple:
    """一直读到 pattern 命中 / 总超时 / 空闲超时。返回 (数据, 是否命中 pattern)。"""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    last_data = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if idle and pattern_bytes is None and buf and (now - last_data) >= idle:
            break
        data = link.read(min(0.1, max(0.01, deadline - now)))
        if data is None:
            break
        if data:
            buf.extend(data)
            last_data = time.monotonic()
            if pattern_bytes is not None and pattern_bytes.search(bytes(buf)):
                return bytes(buf), True
        elif pattern_bytes is None and not buf:
            if now - last_data >= min(settle, timeout):
                break
    matched = bool(pattern_bytes.search(bytes(buf))) if pattern_bytes is not None else False
    return bytes(buf), matched


def cmd_send(args) -> int:
    payload = build_payload(args)
    if not payload:
        eprint("[esp_serial] 没有要发送的内容（用 --text/--file/--hex）")
        return 2
    link = open_endpoint(args.endpoint, args.host, args.port, retries=args.reconnect_retries, verbose=True)
    try:
        write_in_chunks(link, payload, args.chunk, args.delay / 1000.0)
        eprint(f"[esp_serial] 已发送 {human_bytes(len(payload))} → {link.describe()}")
        if args.wait:
            data, matched = capture_until(link, re.compile(args.wait.encode()), args.timeout, args.idle)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            return 0 if matched else 3
    finally:
        link.close()
    return 0


def cmd_exec(args) -> int:
    payload = build_payload(args)
    link = open_endpoint(args.endpoint, args.host, args.port, retries=args.reconnect_retries, verbose=True)
    pattern = re.compile(args.until.encode()) if args.until else None
    try:
        if payload:
            link.write(payload)
        data, matched = capture_until(link, pattern, args.timeout, args.idle)
    finally:
        link.close()

    if args.strip_echo and payload and data.startswith(payload):
        data = data[len(payload) :]
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    if pattern is not None and not matched:
        eprint(f"[esp_serial] 超时：{args.timeout}s 内没等到 /{args.until}/")
        return 3
    return 0


# --------------------------------------------------------------------------- #
# pty（socat 替代）
# --------------------------------------------------------------------------- #

def relay_pty(args) -> int:
    master, slave = pty.openpty()
    make_raw(slave)
    os.set_blocking(master, False)
    slave_name = os.ttyname(slave)

    link_file = os.path.expanduser(args.link_file) if args.link_file else None
    if link_file:
        link_dir = os.path.dirname(os.path.abspath(link_file))
        if link_dir:
            os.makedirs(link_dir, exist_ok=True)
        tmp = link_file + ".tmp"
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(slave_name, tmp)
            os.replace(tmp, link_file)
        except OSError as exc:
            eprint(f"[esp_serial] 建符号链接失败：{exc}")

    print(slave_name, flush=True)
    eprint(f"[esp_serial] PTY 就绪：{slave_name}" + (f"（稳定路径 {link_file}）" if link_file else ""))
    eprint(f"[esp_serial] 用法：esptool --port {link_file or slave_name} ... / "
           f"mpremote connect port:{(link_file or slave_name)} repl / minicom -D {(link_file or slave_name)}")
    if _USB_LINK_ACTIVE:
        eprint("[esp_serial] 数据源：原生 USB（libusb 直连）")
    else:
        eprint(f"[esp_serial] 数据源：tcp://{args.host}:{args.port or DEFAULT_PORT}"
               + ("（断线自动重连）" if args.reconnect else ""))

    link = None
    status = 0
    try:
        while True:
            if link is None:
                try:
                    link = open_endpoint(args.endpoint, args.host, args.port, retries=0)
                except LinkClosed as exc:
                    if not args.reconnect:
                        eprint(f"[esp_serial] {exc}")
                        status = 1
                        break
                    eprint(f"[esp_serial] {exc}；{args.reconnect_delay:.1f}s 后重试")
                    time.sleep(args.reconnect_delay)
                    continue
                eprint(f"[esp_serial] 已连接 {link.describe()}")

            try:
                if link.can_select():
                    ready, _, _ = select.select([master, link.fileno()], [], [], 0.2)
                    master_ready = master in ready
                    link_ready = link.fileno() in ready
                else:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    master_ready = bool(ready)
                    link_ready = True  # USB 没有 fd
            except (OSError, ValueError):
                master_ready = False
                link_ready = False

            if link_ready:
                data = link.read(0 if link.can_select() else 0.02)
                if data is None:
                    eprint("[esp_serial] 桥接断开")
                    link.close()
                    link = None
                    if not args.reconnect:
                        status = 1
                        break
                    time.sleep(args.reconnect_delay)
                    continue
                if data:
                    _write_master(master, data)

            if master_ready:
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    data = b""
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        data = b""  # 还没有客户端打开从设备
                    else:
                        raise
                if data and link is not None:
                    try:
                        link.write(data)
                    except LinkClosed as exc:
                        eprint(f"[esp_serial] 写入桥接失败：{exc}")
                        link.close()
                        link = None
                        if not args.reconnect:
                            status = 1
                            break
    except KeyboardInterrupt:
        status = 0
    finally:
        if link is not None:
            link.close()
        os.close(master)
        os.close(slave)
    return status


def _write_master(master: int, data: bytes) -> None:
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        try:
            sent += os.write(master, view[sent:])
        except BlockingIOError:
            ready, _, _ = select.select([], [master], [], 0.5)
            if not ready:
                eprint("[esp_serial] PTY 缓冲区满，丢弃部分数据（客户端是不是读得太慢？）")
                return
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise


# --------------------------------------------------------------------------- #
# boot / reset / bridge（配合 esptool 烧写用）
# --------------------------------------------------------------------------- #

def _require_usb_link(link, what: str):
    if not hasattr(link, "enter_bootloader"):
        raise SystemExit(
            f"{what} 只支持 --usb 直连模式（USB-Serial-JTAG 的 DTR/RTS 控制）。"
            "TCP 桥接的 socket 没有调制解调器控制线。"
        )
    return link


def cmd_boot(args) -> int:
    """让板子进 ROM 下载模式（USB-Serial-JTAG 的 DTR/RTS 时序）。"""
    link = _require_usb_link(open_endpoint(args.endpoint, args.host, args.port), "boot")
    try:
        method = "?"
        eprint("[esp_serial] 发送下载模式复位序列（IO0 低 + EN 脉冲）…")
        method = link.enter_bootloader()
        eprint(f"[esp_serial] 复位时序：{method}")
    finally:
        link.close()
    eprint("[esp_serial] 已复位，板子应重新枚举为 ROM 下载模式")
    return 0


def cmd_reset(args) -> int:
    """硬复位，让板子从下载模式跳回正常固件。"""
    link = _require_usb_link(open_endpoint(args.endpoint, args.host, args.port), "reset")
    try:
        link.hard_reset()
    finally:
        link.close()
    eprint("[esp_serial] 已硬复位，板子应回到正常固件")
    return 0


def parse_listen(spec: str | None, default_port: int = 18080) -> tuple:
    if not spec:
        return DEFAULT_HOST, default_port
    spec = spec.strip()
    if spec.isdigit():
        return DEFAULT_HOST, int(spec)
    if ":" in spec:
        host, _, port = spec.rpartition(":")
        if port.isdigit():
            return host or DEFAULT_HOST, int(port)
    return spec, default_port


def cmd_bridge(args) -> int:
    """把串口端点（USB CDC / TCP / 设备）暴露成 TCP 服务端，给 esptool 等工具用。"""
    listen_host, listen_port = parse_listen(args.listen, args.listen_port)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_host, listen_port))
    server.listen(1)
    server.settimeout(0.5)

    link = None
    status = 0
    try:
        # 先把串口打开，再宣告地址——否则客户端可能在链路就绪前就开始发数据
        link = open_endpoint(args.endpoint, args.host, args.port, retries=args.reconnect_retries, verbose=True)
        eprint(f"[esp_serial] 数据源：{link.describe()}")
        print(f"{listen_host}:{listen_port}", flush=True)
        eprint(
            f"[esp_serial] 桥接服务已就绪 tcp://{listen_host}:{listen_port}"
            + ("（等一个客户端连上后退出）" if args.once else "")
        )
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break
            eprint(f"[esp_serial] 客户端接入 {addr[0]}:{addr[1]}")
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(0.2)
            try:
                _relay_socket(link, conn)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            eprint("[esp_serial] 客户端断开")
            if args.reset_on_close:
                try:
                    link.hard_reset()
                    eprint("[esp_serial] 已复位芯片，退出下载模式")
                except Exception as exc:  # noqa: BLE001
                    eprint(f"[esp_serial] 复位失败：{exc}")
            if args.once:
                break
            # 多客户端模式：数据源可能已复位失效，重建
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass
            link = open_endpoint(args.endpoint, args.host, args.port, retries=args.reconnect_retries, verbose=True)
    except KeyboardInterrupt:
        status = 0
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            server.close()
        except OSError:
            pass
    return status


def _relay_socket(link, conn) -> None:
    """在两个方向搬字节，直到任一端断开。"""
    while True:
        if link.can_select():
            try:
                ready, _, _ = select.select([conn, link.fileno()], [], [], 0.2)
            except (OSError, ValueError):
                return
            sock_ready = conn in ready
            link_ready = link.fileno() in ready
        else:
            try:
                ready, _, _ = select.select([conn], [], [], 0.02)
            except (OSError, ValueError):
                return
            sock_ready = bool(ready)
            link_ready = True
        if link_ready:
            try:
                data = link.read(0 if link.can_select() else 0.02)
            except LinkClosed as exc:
                eprint(f"[esp_serial] 串口断开：{exc}")
                return
            if data is None:
                return
            if data:
                try:
                    conn.sendall(data)
                except OSError:
                    return
        if sock_ready:
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, BlockingIOError):
                chunk = None
            except OSError:
                return
            if chunk == b"":
                return
            if chunk:
                try:
                    link.write(chunk)
                except LinkClosed as exc:
                    eprint(f"[esp_serial] 写入串口失败：{exc}")
                    return


# --------------------------------------------------------------------------- #
# flash
# --------------------------------------------------------------------------- #

def find_esptool():
    exe = _which("esptool") or _which("esptool.py")
    if exe:
        return [exe]
    try:
        import esptool  # type: ignore  # noqa: F401

        return [sys.executable, "-m", "esptool"]
    except Exception:
        return None


def cmd_flash(args) -> int:
    base = find_esptool()
    if base is None:
        eprint("[esp_serial] 没找到 esptool：pkg install esptool")
        return 2
    host = args.host or DEFAULT_HOST
    port = args.port or DEFAULT_PORT
    url = f"socket://{host}:{port}"

    argv = list(args.esptool_args)
    if argv and argv[0] == "--":
        argv = argv[1:]

    has_port = any(a in ("-p", "--port") or a.startswith("--port=") for a in argv)
    if not has_port:
        argv = ["--port", url] + argv

    # 桥接 socket 改不了 DTR/RTS，不能靠工具自动进下载模式
    has_before = any(a.startswith("--before") for a in argv)
    has_after = any(a.startswith("--after") for a in argv)
    extra = []
    if not has_before:
        extra += ["--before", "no-reset"]
    if not has_after:
        extra += ["--after", "no-reset"]
    if extra:
        argv += extra

    full = base + argv
    eprint("[esp_serial] 执行：" + shlex.join(full))
    eprint("[esp_serial] 提醒：先按住 BOOT → 点一下 RST/EN → 松开 BOOT，让板子进下载模式；"
           "桥接端口不能控制 DTR/RTS，所以不能自动复位。")
    try:
        return subprocess.call(full)
    except OSError as exc:
        eprint(f"[esp_serial] 启动 esptool 失败：{exc}")
        return 1


# --------------------------------------------------------------------------- #
# usb
# --------------------------------------------------------------------------- #

def _usb_identify_inner(args) -> int:
    try:
        import libusb_probe
    except Exception as exc:
        eprint(f"[esp_serial] 无法导入 libusb_probe：{exc}")
        return 2
    raw_fd = os.environ.get("TERMUX_USB_FD")
    if not raw_fd:
        eprint("[esp_serial] 没有 TERMUX_USB_FD 环境变量。请在 Termux 里用：")
        eprint("  esp_serial.py usb identify")
        return 2
    try:
        fd = int(raw_fd)
    except ValueError:
        eprint(f"[esp_serial] TERMUX_USB_FD 不是整数：{raw_fd!r}")
        return 2
    try:
        info = libusb_probe.identify_fd(fd)
    except libusb_probe.LibUsbError as exc:
        eprint(f"[esp_serial] {exc}")
        return 3
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
    else:
        print(libusb_probe.describe(info))
    return 0


def cmd_usb(args) -> int:
    if args.usb_action == "list":
        devices = termux_usb_devices()
        if args.json:
            print(json.dumps({"devices": usb_device_paths(devices)}, ensure_ascii=False, indent=2))
            return 0
        if devices is None:
            print("termux-usb 不可用：pkg install termux-api 并安装 Termux:API App")
            return 1
        paths = usb_device_paths(devices)
        if not paths:
            print("没有 USB 设备")
            return 1
        for idx, dev in enumerate(paths):
            print(f"[{idx}] {dev}")
        return 0

    # usb identify
    device = args.device
    if not device:
        paths = usb_device_paths(termux_usb_devices())
        if not paths:
            eprint("[esp_serial] 没有可识别的 USB 设备")
            return 1
        device = paths[0]
        if len(paths) > 1:
            eprint(f"[esp_serial] 有多个设备，默认用第一个：{device}（可用 usb identify /dev/bus/usb/... 指定）")

    inner = [sys.executable, os.path.abspath(__file__), "_usb-identify"]
    if args.json:
        inner.append("--json")
    cmd = ["termux-usb", "-r", "-E", "-e", shlex.join(inner), device]
    eprint("[esp_serial] 会在手机上弹出 USB 授权框，点「允许」...")
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        eprint("[esp_serial] 找不到 termux-usb：pkg install termux-api")
        return 2


# --------------------------------------------------------------------------- #
# 原生 USB 直连模式（--usb）
# --------------------------------------------------------------------------- #

def cmd_usb_link(args) -> int:
    """被 termux-usb -e 调起的进程：把子命令重新解析一遍，然后用 USB 链路执行。"""
    global _USB_LINK_ACTIVE
    if not os.environ.get("TERMUX_USB_FD"):
        eprint("[esp_serial] 缺少 TERMUX_USB_FD：--usb 模式必须经由 termux-usb 启动")
        return 2
    _USB_LINK_ACTIVE = True
    rebind_stdout()
    sub_args = build_parser().parse_args(args.rest)
    if not getattr(sub_args, "command", None):
        eprint("[esp_serial] 缺少子命令")
        return 2
    for attr in ("reconnect",):
        if getattr(sub_args, attr, False):
            setattr(sub_args, attr, False)
            eprint("[esp_serial] USB 直连模式不支持自动重连，已关闭")
    try:
        return sub_args.func(sub_args) or 0
    except KeyboardInterrupt:
        eprint("\n[esp_serial] 已中断")
        return 130
    finally:
        _USB_LINK_ACTIVE = False


def usb_reexec(argv_rest, device: str | None = None) -> int:
    """把当前命令包进 termux-usb 重新执行，让子进程拿到设备 fd。"""
    exe = shutil.which("termux-usb")
    if not exe:
        eprint("[esp_serial] 找不到 termux-usb：pkg install termux-api 并安装 Termux:API App")
        return 2

    if device is None:
        paths = usb_device_paths(termux_usb_devices())
        if not paths:
            # 芯片复位后会重新枚举，给它一点时间
            eprint("[esp_serial] 等设备重新枚举 …")
            deadline = time.time() + 25
            while time.time() < deadline and not paths:
                time.sleep(0.3)
                paths = usb_device_paths(termux_usb_devices())
        if not paths:
            eprint("[esp_serial] 没有可用的 USB 设备（检查 OTG 线和数据线）")
            return 1
        device = paths[0]
        if len(paths) > 1:
            eprint(f"[esp_serial] 检测到多个 USB 设备，使用第一个：{device}")

    inner = [sys.executable, os.path.abspath(__file__), "_usb-link"] + argv_rest
    command = [exe, "-r", "-E", "-e", shlex.join(inner), device]
    eprint(f"[esp_serial] 原生 USB 直连 {device}（需要授权时手机会弹框，点「允许」）")

    env = dict(os.environ)
    fifo_path = _make_output_fifo()
    if fifo_path:
        env[OUT_FIFO_ENV] = fifo_path

    try:
        proc = subprocess.Popen(command, env=env)
    except OSError as exc:
        eprint(f"[esp_serial] 启动 termux-usb 失败：{exc}")
        _remove_fifo(fifo_path)
        return 1

    try:
        returncode = _pump_fifo(fifo_path, proc) if fifo_path else None
        if returncode is None:
            returncode = proc.wait()
        return returncode
    finally:
        _remove_fifo(fifo_path)


def _make_output_fifo():
    """在 $TMPDIR 建一个只有自己能读写的命名管道。"""
    tmpdir = os.environ.get("TMPDIR") or os.path.join(os.environ.get("PREFIX", "/tmp"), "tmp")
    path = os.path.join(tmpdir, f"esp_serial_out_{os.getpid()}.fifo")
    try:
        os.makedirs(tmpdir, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        os.mkfifo(path, 0o600)
    except OSError as exc:
        eprint(f"[esp_serial] 建实时输出管道失败（{exc}）：输出会退化成子进程退出后才显示")
        return None
    return path


def _remove_fifo(path) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _pump_fifo(path, proc):
    """把子进程写进 FIFO 的输出实时搬到自己的 stdout；返回子进程退出码。"""
    out = sys.stdout.buffer
    try:
        rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        while True:
            try:
                chunk = os.read(rfd, 65536)
            except BlockingIOError:
                chunk = None
            if chunk:
                out.write(chunk)
                out.flush()
                continue
            if chunk == b"":
                # 写端还没打开时 FIFO 读到的是 EOF：子进程还活着就再等等
                if proc.poll() is None:
                    select.select([rfd], [], [], 0.05)
                    continue
                break
            select.select([rfd], [], [], 0.1)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass
    return proc.wait()


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #

def _add_endpoint(sp, allow_positional: bool = True):
    if allow_positional:
        sp.add_argument(
            "endpoint",
            nargs="?",
            help="端点：8080 / host:port / tcp://host:port / fd://N / /dev/pts/N（默认 127.0.0.1:8080）",
        )
    else:
        sp.add_argument(
            "--endpoint",
            default=None,
            help="端点：tcp://host:port / fd://N / /dev/pts/N（一般用 --port 就够）",
        )
    sp.add_argument("--host", default=DEFAULT_HOST, help="桥接主机（默认 127.0.0.1）")
    sp.add_argument("--port", type=int, default=None, help="桥接端口（默认 8080）")
    sp.add_argument(
        "--usb",
        action="store_true",
        help="不走 TCP 桥接，直接用 termux-usb + libusb 访问原生 USB CDC 设备",
    )


def _add_reconnect(sp, default_retries: int = 0):
    sp.add_argument("--reconnect-retries", type=int, default=default_retries,
                    help="连接失败重试次数（每次间隔 1s，默认 %d）" % default_retries)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="esp_serial.py",
        description="Termux 上的 ESP32 / USB 串口工具（TCPUART 桥接客户端 + socat 替代）",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"esp_serial.py {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # list
    p = sub.add_parser("list", aliases=["ls"], help="列出 termux-usb 设备 + 本地桥接端口")
    p.add_argument("--json", action="store_true")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--ports", help="要扫的端口，逗号分隔，如 8080,1234")
    p.add_argument("--range", help="端口范围，如 8000-8100")
    p.add_argument("--timeout", type=int, default=300, help="单端口超时（毫秒，默认 300）")
    p.add_argument("--no-scan", action="store_true", help="只列 USB 设备，不扫端口")
    p.set_defaults(func=cmd_list)

    # scan
    p = sub.add_parser("scan", help="扫描本地 TCP 桥接端口")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--ports", help="逗号分隔端口列表")
    p.add_argument("--range", help="端口范围，如 8000-8100")
    p.add_argument("--timeout", type=int, default=300, help="单端口超时（毫秒）")
    p.add_argument("--probe", action="store_true", help="连上去试着读一点数据")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scan)

    # doctor
    p = sub.add_parser("doctor", help="环境体检")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    # mon
    p = sub.add_parser("mon", help="只读监听（时间戳 / hex / 落盘）")
    _add_endpoint(p)
    p.add_argument("--stamp", action="store_true", help="每行加 [HH:MM:SS.mmm] 时间戳")
    p.add_argument("--hex", action="store_true", help="十六进制显示")
    p.add_argument("--log", help="原始字节另存到文件")
    p.add_argument("--duration", type=float, help="监听多少秒后退出")
    p.add_argument("--exit-on", help="收到匹配该正则的数据就退出")
    p.add_argument("--reconnect", action="store_true", help="断线自动重连")
    _add_reconnect(p)
    p.set_defaults(func=cmd_mon)

    # repl
    p = sub.add_parser("repl", help="交互式终端（Ctrl-] 退出）")
    _add_endpoint(p)
    p.add_argument("--enter", choices=sorted(ENTER_SEQ), default="cr",
                   help="回车发什么（默认 cr，micropython/AT 都用 \\r）")
    p.add_argument("--local-echo", dest="local_echo", action="store_true", default=True,
                   help="本地回显（默认开）")
    p.add_argument("--no-local-echo", dest="local_echo", action="store_false")
    p.add_argument("--forward-ctrl-c", dest="forward_ctrl_c", action="store_true",
                   default=False,
                   help="把 Ctrl-C 转发给板子而不是退出（MicroPython/AT 要中断程序时用）")
    _add_reconnect(p)
    p.set_defaults(func=cmd_repl)

    # send
    p = sub.add_parser("send", help="发送文本 / 文件 / 十六进制")
    _add_endpoint(p, allow_positional=False)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--text", help="要发送的文本（- 表示读 stdin）")
    src.add_argument("--file", help="要发送的文件")
    src.add_argument("--hex", dest="hex_data", help="十六进制字节串，如 '01 03 00 00'")
    p.add_argument("--append", choices=sorted(ENTER_SEQ), default=None,
                   help="末尾附加行尾（--text 默认 cr，--file 默认 none）")
    p.add_argument("--chunk", type=int, default=0, help="分块大小（字节，默认整包发）")
    p.add_argument("--delay", type=int, default=0, help="每块间隔毫秒")
    p.add_argument("--wait", help="发送后等这段正则的回显")
    p.add_argument("--timeout", type=float, default=5.0, help="--wait 的超时秒数")
    p.add_argument("--idle", type=float, default=0.3, help="--wait 时静默多久算结束")
    _add_reconnect(p)
    p.set_defaults(func=cmd_send)

    # exec
    p = sub.add_parser("exec", help="发一条命令并抓回显（脚本友好）")
    _add_endpoint(p, allow_positional=False)
    p.add_argument("cmd", nargs="*", help="命令文本（等价 --text）")
    p.add_argument("--text", help="命令文本（- 表示读 stdin）")
    p.add_argument("--file", help="从文件读取要发送的内容")
    p.add_argument("--hex", dest="hex_data", help="十六进制字节串")
    p.add_argument("--append", choices=sorted(ENTER_SEQ), default=None)
    p.add_argument("--until", help="等到匹配该正则就停（如 '>>>'）")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--idle", type=float, default=0.4, help="没给 --until 时，静默多久算结束")
    p.add_argument("--strip-echo", action="store_true", help="去掉回显里重复的发送内容")
    _add_reconnect(p)
    p.set_defaults(func=cmd_exec)

    # pty
    p = sub.add_parser("pty", help="把桥接端点暴露成 /dev/pts/N（socat 替代）")
    _add_endpoint(p)
    p.add_argument("--link-file", help="额外建一个稳定符号链接，如 ~/esp32")
    p.add_argument("--no-reconnect", dest="reconnect", action="store_false", default=True,
                   help="桥接断开就退出（默认会自动重连）")
    p.add_argument("--reconnect-delay", type=float, default=1.0, help="重连间隔秒数")
    p.set_defaults(func=relay_pty)

    # boot / reset
    p = sub.add_parser("boot", help="让板子进 ROM 下载模式（USB-JTAG 的 DTR/RTS 时序）")
    _add_endpoint(p)
    p.set_defaults(func=cmd_boot)

    p = sub.add_parser("reset", help="硬复位板子（退出下载模式跑固件）")
    _add_endpoint(p)
    p.set_defaults(func=cmd_reset)

    # bridge
    p = sub.add_parser("bridge", help="把串口暴露成 TCP 服务端（给 esptool/socat 用）")
    _add_endpoint(p)
    p.add_argument("--listen", default=None, help="监听地址，默认 127.0.0.1:18080")
    p.add_argument("--listen-port", type=int, default=18080, help="只给 --listen 主机时用的端口")
    p.add_argument("--once", action="store_true", default=True, help="服务一个客户端后退出（默认）")
    p.add_argument("--no-once", dest="once", action="store_false", help="持续服务多个客户端")
    p.add_argument("--reset-on-close", action="store_true",
                   help="客户端断开后硬复位芯片（烧写完自动退出下载模式）")
    _add_reconnect(p)
    p.set_defaults(func=cmd_bridge)

    # flash
    p = sub.add_parser("flash", help="用 esptool 通过 socket:// 直接烧写")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--endpoint", default=None, help="也可直接给 tcp://host:port")
    p.add_argument("esptool_args", nargs=argparse.REMAINDER,
                   help="原样传给 esptool 的参数，例如 -- write-flash -z 0x1000 fw.bin")
    p.set_defaults(func=cmd_flash)

    # usb
    p = sub.add_parser("usb", help="USB 设备：list / identify")
    usb_sub = p.add_subparsers(dest="usb_action", metavar="<动作>")
    pu = usb_sub.add_parser("list", help="列出 termux-usb 设备")
    pu.add_argument("--json", action="store_true")
    pu = usb_sub.add_parser("identify", aliases=["id"], help="读描述符：VID:PID / 芯片 / 端点")
    pu.add_argument("device", nargs="?", help="/dev/bus/usb/001/002（默认第一个）")
    pu.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_usb)

    # 内部：被 termux-usb -e 调起的进程
    p = sub.add_parser("_usb-identify", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_usb_identify_inner)

    p = sub.add_parser("_usb-link", help=argparse.SUPPRESS)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_usb_link)

    # 内部命令不进 --help 列表
    sub._choices_actions = [
        action for action in sub._choices_actions if not action.dest.lstrip("-").startswith("_")
    ]
    return parser


def main(argv=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--usb" in raw_argv and (not raw_argv or raw_argv[0] != "_usb-link"):
        rest = [a for a in raw_argv if a != "--usb"]
        if not rest:
            eprint("[esp_serial] --usb 要跟一个子命令，例如：esp_serial.py mon --usb")
            return 2
        return usb_reexec(rest)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    # 没指定端点、8080 又没有桥接服务、但插着 USB 设备 → 自动按 --usb 直连重跑一次。
    # （板子插的是芯片自带 USB 或 CDC 桥时，裸命令也能直接用，不用记着加 --usb）
    if (args.command in USB_AUTO_COMMANDS and not _USB_LINK_ACTIVE
            and not getattr(args, "endpoint", None) and not getattr(args, "port", None)):
        host = getattr(args, "host", DEFAULT_HOST) or DEFAULT_HOST
        probe_port_num = getattr(args, "port", None) or DEFAULT_PORT
        if not probe_port(host, probe_port_num, 0.25):
            devices = usb_device_paths(termux_usb_devices(timeout=5))
            if devices:
                eprint(
                    f"[esp_serial] {host}:{probe_port_num} 没有桥接服务，"
                    f"但检测到 USB 设备 {devices[0]} → 自动改用 USB 直连"
                )
                return usb_reexec(raw_argv)
    if getattr(args, "usb_action", None) is None and args.command == "usb":
        parser.parse_args(["usb", "--help"])
        return 2

    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        eprint("\n[esp_serial] 已中断")
        return 130
    except LinkClosed as exc:
        eprint(f"[esp_serial] {exc}")
        return 1
    except BrokenPipeError:
        return 0
    except (OSError, ValueError) as exc:
        eprint(f"[esp_serial] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
