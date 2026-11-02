#!/usr/bin/env python3
"""usb_cdc.py — 绕过桥接 App，直接用 libusb 走 ESP32 原生 USB 的 CDC-ACM 串口。

适用对象
--------
USB 描述符里带标准 **CDC-ACM** 接口的设备，最典型的就是 ESP32-S2/S3/C3
插 USB 后枚举出的 ``303a:1001``（"USB JTAG/serial debug unit"）：

    iface 0  class 0x02/0x02  ← CDC 控制接口（中断端点）
    iface 1  class 0x0a/0x02  ← CDC 数据接口（bulk IN / bulk OUT）  ← 用它收发
    iface 2  class 0xff/...   ← JTAG 接口，本模块不碰

对 CH340/CP210x/FTDI 这类 USB-UART 桥，厂商私有初始化序列本模块不实现，
请走 ``TCPUART + TCP`` 那条路线（同样由 esp_serial.py 支持）。

用法（由 esp_serial.py 的 ``--usb`` 自动调用）
---------------------------------------------
    termux-usb -r -E -e "esp_serial.py _usb-link mon --stamp" /dev/bus/usb/001/002

``-E`` 让 termux-usb 把设备 fd 放进 ``TERMUX_USB_FD``；本模块 wrap 它、claim
两个接口、下发 SET_LINE_CODING / SET_CONTROL_LINE_STATE，然后就是 bulk 收发。
"""

from __future__ import annotations

import ctypes
import os
import sys

import libusb_probe as lp

# CDC 类请求
CDC_SET_LINE_CODING = 0x20
CDC_SET_CONTROL_LINE_STATE = 0x22
CDC_REQTYPE_HOST_TO_DEVICE = 0x21  # Class | Interface | OUT

# 标准请求 CLEAR_FEATURE(HALT)，用来给端点解挂（如果上一次跑崩了）
_REQ_CLEAR_FEATURE = 0x01
_FEATURE_ENDPOINT_HALT = 0x00

CDC_CLASS = 0x02
CDC_SUBCLASS_ACM = 0x02
CDC_DATA_CLASS = 0x0A

LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_ACCESS = -3

DEFAULT_BAUDRATE = 115200
MAX_PACKET = 4096


# --------------------------------------------------------------------------- #
# 纯函数（可单测）
# --------------------------------------------------------------------------- #

def line_coding(baudrate: int = DEFAULT_BAUDRATE, stop_bits: int = 0,
                parity: int = 0, data_bits: int = 8) -> bytes:
    """CDC SET_LINE_CODING 的 7 字节负载（小端）。"""
    return (
        int(baudrate).to_bytes(4, "little")
        + bytes([stop_bits & 0xFF, parity & 0xFF, data_bits & 0xFF])
    )


def find_cdc_interfaces(cfg: dict):
    """从 config 描述符里定位 CDC 控制接口 + 数据接口。

    返回 ``{"control": 控制接口号或 None, "data": 数据接口号,
              "in": bulk IN 端点, "out": bulk OUT 端点}``；找不到返回 None。
    """
    control_iface = None
    for iface in cfg.get("interfaces", []):
        if iface["class"] == CDC_CLASS and iface["subclass"] == CDC_SUBCLASS_ACM:
            control_iface = iface["number"]
            break

    for iface in cfg.get("interfaces", []):
        if iface["alternate"] != 0:
            continue
        is_data_iface = iface["class"] == CDC_DATA_CLASS
        bulk_in = next(
            (e for e in iface["endpoints"] if e["direction"] == "in" and e["transfer_type"] == 2),
            None,
        )
        bulk_out = next(
            (e for e in iface["endpoints"] if e["direction"] == "out" and e["transfer_type"] == 2),
            None,
        )
        if bulk_in is None or bulk_out is None:
            continue
        if not is_data_iface:
            continue
        return {
            "control": control_iface,
            "data": iface["number"],
            "in": bulk_in["address"],
            "out": bulk_out["address"],
            "in_max_packet": bulk_in["max_packet_size"],
            "out_max_packet": bulk_out["max_packet_size"],
        }
    return None


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #

class UsbCdcLink:
    """和 esp_serial.Link 同接口的 USB-CDC 链路（duck typing，不继承 Link）。

    ``read(timeout)`` -> bytes / b""（超时）/ None（设备掉线）
    """

    kind = "usb"

    def __init__(self, fd: int, baudrate: int = DEFAULT_BAUDRATE, read_timeout_ms: int = 200):
        self.fd = fd
        self.baudrate = baudrate
        self.read_timeout_ms = read_timeout_ms
        self.lib = None
        self.ctx = None
        self.handle = None
        self.info = {}
        self.claimed = []
        self.detached = []
        self.in_ep = None
        self.out_ep = None
        self.control_iface = None
        self.data_iface = None

    # ---------------------------------------------------------------- open
    def open(self) -> "UsbCdcLink":
        from esp_serial import LinkClosed  # 延迟导入，避免循环依赖

        try:
            self.lib, _ = lp.load()
        except lp.LibUsbError as exc:
            raise LinkClosed(str(exc)) from exc

        lib = self.lib
        self.ctx = lp.init_context(lib)
        try:
            self.handle = lp.wrap_device(lib, self.ctx, self.fd)
        except lp.LibUsbError as exc:
            lp.exit_context(lib, self.ctx)
            self.ctx = None
            raise LinkClosed(
                f"{exc}：fd 不是 termux-usb 递进来的 USB fd，或设备已被别的 App 占用"
            ) from exc

        try:
            self.info = lp.device_descriptor(lib, self.handle)
            cfg = lp.config_descriptor(lib, self.handle, 0)
            found = find_cdc_interfaces(cfg)
            if found is None:
                raise LinkClosed(
                    "这个设备没有标准 CDC-ACM 数据接口，直连模式不支持它。"
                    "USB-UART 桥（CH340/CP210x/FTDI）请用 TCPUART 桥接方案。"
                )
            self.control_iface = found["control"]
            self.data_iface = found["data"]
            self.in_ep = found["in"]
            self.out_ep = found["out"]

            for iface in (self.control_iface, self.data_iface):
                if iface is None:
                    continue
                self._claim_with_retry(iface)

            self._configure_port()
        except Exception:
            self.close()
            raise
        return self

    def _claim_with_retry(self, iface: int, attempts: int = 6, delay: float = 0.5) -> None:
        """claim 一个接口，自动处理内核驱动占用和"上一个会话刚关闭"的竞争。"""
        import time as _time

        from esp_serial import LinkClosed

        lib = self.lib
        last = None
        for attempt in range(attempts):
            # Android 内核的 cdc_acm 常常抢先绑住 CDC 接口，必须卸掉才能 claim；
            # 上一个进程刚退出时驱动可能又绑回来，所以每轮都重新检查。
            if lp.kernel_driver_active(lib, self.handle, iface):
                if lp.detach_kernel_driver(lib, self.handle, iface):
                    if iface not in self.detached:
                        self.detached.append(iface)
                        print(
                            f"[usb_cdc] 已从接口 {iface} 卸下内核驱动（cdc_acm），退出时恢复",
                            file=sys.stderr, flush=True,
                        )
            try:
                lp.claim_interface(lib, self.handle, iface)
                self.claimed.append(iface)
                return
            except lp.LibUsbError as exc:
                last = exc
                busy = "LIBUSB_ERROR_BUSY" in str(exc)
                if busy and attempt + 1 < attempts:
                    _time.sleep(delay)
                    continue
                if busy:
                    raise LinkClosed(
                        f"接口 {iface} 被占用（LIBUSB_ERROR_BUSY）：另有一个 App 或进程拿着这个串口。\n"
                        "  排查顺序：\n"
                        "   1) 关掉 ArduinoDroid、串口终端、设备配套 App（插入时它们会自动连上）\n"
                        "   2) 等一两秒重试（上一个 esp 会话可能还没释放）\n"
                        "   3) 拔掉 OTG 再插回；仍不行就重启手机（usbfs 会残留占用）"
                    ) from exc
                raise LinkClosed(f"claim 接口 {iface} 失败：{exc}") from exc
        raise LinkClosed(f"claim 接口 {iface} 失败：{last}")

    def _configure_port(self) -> None:
        lib, handle = self.lib, self.handle
        w_index = self.control_iface or 0
        # 清掉可能残留的 halt 标记
        for ep in (self.in_ep, self.out_ep):
            try:
                lp.control_out(
                    lib, handle, 0x02, _REQ_CLEAR_FEATURE, _FEATURE_ENDPOINT_HALT, ep
                )
            except lp.LibUsbError:
                pass
        try:
            lp.control_out(
                lib, handle, CDC_REQTYPE_HOST_TO_DEVICE, CDC_SET_LINE_CODING,
                0, w_index, line_coding(self.baudrate),
            )
        except lp.LibUsbError:
            pass  # USB-Serial-JTAG 会忽略波特率，失败了也不影响收发
        try:
            # 保持 DTR/RTS 都不拉低。ESP32-S3 的 USB-Serial-JTAG 把 RTS 接到 EN、
            # DTR 接到 IO0：打开端口就把它们置位，等于把芯片按在复位里（esptool 会一直等不到响应）。
            # 需要复位时由 boot/reset 命令显式发时序。
            lp.control_out(
                lib, handle, CDC_REQTYPE_HOST_TO_DEVICE, CDC_SET_CONTROL_LINE_STATE,
                0x0000, w_index,
            )
        except lp.LibUsbError:
            pass

    # ---------------------------------------------------------------- 控制线
    def set_dtr_rts(self, dtr: bool, rts: bool) -> None:
        """通过 CDC SET_CONTROL_LINE_STATE 设置 DTR/RTS（True = 断言/拉低）。"""
        from esp_serial import LinkClosed

        if self.handle is None:
            raise LinkClosed("USB 链路已关闭")
        value = (0x02 if rts else 0) | (0x01 if dtr else 0)
        lp.control_out(
            self.lib, self.handle, CDC_REQTYPE_HOST_TO_DEVICE,
            CDC_SET_CONTROL_LINE_STATE, value, self.control_iface or 0,
        )

    def bootloader_reset(self, settle: float = 0.1) -> None:
        """USB-Serial-JTAG 的「进下载模式」时序。

        与 esptool 的 ``usb_jtag_bootloader_reset`` 逐步对应：DTR/RTS 低有效，
        所以 True 表示把线路拉低（IO0=低 选下载模式，EN=低 触发复位）。
        """
        import time as _time

        self.set_dtr_rts(False, False)   # 空闲：(DTR=高, RTS=高)
        _time.sleep(settle)
        self.set_dtr_rts(True, False)    # IO0 拉低 → 选择下载模式
        _time.sleep(settle)
        self.set_dtr_rts(True, True)     # EN 拉低 → 芯片复位
        _time.sleep(settle)
        self.set_dtr_rts(False, True)    # 复位期间释放 IO0
        self.set_dtr_rts(False, False)   # EN 抬起 → 进 ROM 下载模式

    def bridge_bootloader_reset(self, enter_boot_delay: float = 0.1,
                                reset_delay: float = 0.05) -> None:
        """经典 DTR/RTS 时序，用于 CH340/CH343/CP210x/FTDI 这类 USB-UART 桥。

        这类芯片的 DTR 接 IO0、RTS 接 EN（低有效）。步骤与 esptool 的
        ``unix_tight_bootloader_reset`` 一一对应。
        """
        import time as _time

        self.set_dtr_rts(False, False)   # 都释放
        self.set_dtr_rts(True, True)     # 都拉低
        self.set_dtr_rts(False, True)    # IO0=高 & EN=低 → 芯片进复位
        _time.sleep(enter_boot_delay)
        self.set_dtr_rts(True, False)    # IO0=低 & EN=高 → 出来进下载模式
        _time.sleep(reset_delay)
        self.set_dtr_rts(False, False)   # IO0=高，完成

    def is_usb_jtag(self) -> bool:
        """是不是芯片自带的 USB-Serial-JTAG（303a:xxxx）。"""
        return self.info.get("vid") == 0x303A

    def enter_bootloader(self) -> str:
        """按芯片类型选合适的复位时序，返回用了哪套。"""
        if self.is_usb_jtag():
            self.bootloader_reset()
            return "USB-Serial-JTAG"
        self.bridge_bootloader_reset()
        return "USB-UART 桥（经典 DTR/RTS）"

    def hard_reset(self, hold: float = 0.2, post_release: float = 0.2) -> None:
        """脉冲 EN 让芯片重启（从下载模式跳回正常固件）。"""
        import time as _time

        self.set_dtr_rts(False, True)    # EN 拉低
        _time.sleep(hold)
        self.set_dtr_rts(False, False)   # EN 抬起
        if post_release:
            _time.sleep(post_release)

    # ---------------------------------------------------------------- io
    def can_select(self) -> bool:
        return False  # 没有可 select 的 fd

    def read(self, timeout: float = 0.2):
        from esp_serial import LinkClosed

        if self.handle is None:
            return None
        timeout_ms = max(1, int(timeout * 1000) or 1)
        rc, data = lp.bulk_read(self.lib, self.handle, self.in_ep, MAX_PACKET, timeout_ms)
        if rc == 0:
            return data
        if rc == LIBUSB_ERROR_TIMEOUT:
            return b""
        if rc == LIBUSB_ERROR_NO_DEVICE:
            return None
        if rc == LIBUSB_ERROR_PIPE:
            return b""  # 端点被 stall，忽略这一拍
        raise LinkClosed(f"USB 读取失败：{lp.error_name(self.lib, rc)}")

    def write(self, data: bytes) -> int:
        from esp_serial import LinkClosed

        if self.handle is None:
            raise LinkClosed("USB 链路已关闭")
        sent = 0
        while sent < len(data):
            chunk = data[sent : sent + MAX_PACKET]
            rc, transferred = lp.bulk_write(self.lib, self.handle, self.out_ep, chunk, 2000)
            if rc < 0:
                if rc == LIBUSB_ERROR_PIPE:
                    try:
                        lp.control_out(
                            self.lib, self.handle, 0x02, _REQ_CLEAR_FEATURE,
                            _FEATURE_ENDPOINT_HALT, self.out_ep,
                        )
                    except lp.LibUsbError:
                        pass
                    continue
                raise LinkClosed(f"USB 写入失败：{lp.error_name(self.lib, rc)}")
            if transferred <= 0:
                raise LinkClosed("USB 写入返回 0 字节")
            sent += transferred
        return sent

    def fileno(self) -> int:
        return -1

    def describe(self) -> str:
        vid = self.info.get("vid")
        pid = self.info.get("pid")
        product = self.info.get("product") or ""
        ven = self.info.get("vid_pid") or (
            f"{vid:04x}:{pid:04x}" if vid is not None and pid is not None else "?"
        )
        name = " ".join(x for x in (ven, product) if x)
        return (
            f"usb:{name} (CDC iface {self.data_iface}, IN=0x{self.in_ep:02x}, "
            f"OUT=0x{self.out_ep:02x}, {self.baudrate}bps)"
        )

    def close(self) -> None:
        lib = self.lib
        if lib is None:
            return
        if self.handle is not None:
            for iface in reversed(self.claimed):
                lp.release_interface(lib, self.handle, iface)
            self.claimed = []
            for iface in self.detached:
                lp.attach_kernel_driver(lib, self.handle, iface)
            self.detached = []
            try:
                lp.close_handle(lib, self.handle)
            except Exception:
                pass
            self.handle = None
        if self.ctx is not None:
            lp.exit_context(lib, self.ctx)
            self.ctx = None


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def from_env(varname: str = "TERMUX_USB_FD", baudrate: int = DEFAULT_BAUDRATE) -> UsbCdcLink:
    """从 termux-usb -E 递进来的 fd 建立链路。"""
    from esp_serial import LinkClosed

    raw = os.environ.get(varname)
    if not raw:
        raise LinkClosed(
            f"没有 {varname} 环境变量。直连模式要用 termux-usb 起："
            "esp_serial.py mon --usb（子命令会自动包一层 termux-usb）"
        )
    try:
        fd = int(raw)
    except ValueError as exc:
        raise LinkClosed(f"{varname}={raw!r} 不是整数 fd") from exc
    return UsbCdcLink(fd, baudrate=baudrate).open()
