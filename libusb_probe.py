#!/usr/bin/env python3
"""libusb_probe.py — 用 ctypes 调 libusb 读 termux-usb 传入 fd 的设备信息（只读）。

设计约束
--------
* 只做 ``GET_DESCRIPTOR`` 标准控制传输，**不占用接口**、不做 bulk 收发，
  所以设备即使正被 TCPUART 之类的桥接应用使用，也不会被破坏。
* 需要 ``pkg install libusb``（提供 ``libusb-1.0.so``）。
* Android 上普通进程打不开 ``/dev/bus/usb/...``，必须由 ``termux-usb -E -e``
  把 fd 以 ``TERMUX_USB_FD`` 环境变量交给子进程，这里再用
  ``libusb_wrap_sys_device()`` 把这个 fd 包成 libusb 句柄。

主要入口
--------
``identify_fd(fd) -> dict``   读描述符，返回结构化信息
``describe(info) -> str``     人类可读文本
``available() -> str | None`` libusb 路径（None 表示没装）

描述符解析函数（``parse_*``）是纯函数，可脱离硬件单测。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

DESC_DEVICE = 0x01
DESC_CONFIG = 0x02
DESC_STRING = 0x03
DESC_INTERFACE = 0x04
DESC_ENDPOINT = 0x05

# 标准请求：GET_DESCRIPTOR
_BMREQUEST_IN = 0x80
_REQ_GET_DESCRIPTOR = 0x06
_TIMEOUT_MS = 2000

# (vid, pid) -> 芯片说明。ESP32 开发板基本落在这张表里。
USB_SERIAL_CHIPS = {
    (0x10C4, 0xEA60): "CP2102/CP2104 USB-UART (Silicon Labs)",
    (0x10C4, 0xEA70): "CP2105 双路 USB-UART",
    (0x10C4, 0xEA71): "CP2108 四路 USB-UART",
    (0x1A86, 0x7523): "CH340 USB-UART",
    (0x1A86, 0x7522): "CH340 变体",
    (0x1A86, 0x5523): "CH341A USB-UART",
    (0x1A86, 0x55D3): "CH343 USB-UART",
    (0x1A86, 0x55D4): "CH9102 USB-UART",
    (0x0403, 0x6001): "FT232R USB-UART",
    (0x0403, 0x6010): "FT2232 USB-UART",
    (0x0403, 0x6015): "FT231X USB-UART",
    (0x067B, 0x2303): "PL2303 USB-UART",
    (0x303A, 0x1001): "Espressif ESP32-S2/S3/C3 原生 USB (JTAG-serial)",
    (0x303A, 0x0002): "Espressif ESP32 原生 USB (CDC)",
}


class LibUsbError(RuntimeError):
    """libusb 调用失败。"""


# --------------------------------------------------------------------------- #
# libusb 动态加载
# --------------------------------------------------------------------------- #

_lib_state: dict = {"tried": False, "lib": None, "path": None}


def _candidate_names() -> list:
    names = []
    env_path = os.environ.get("LIBUSB_PATH")
    if env_path:
        names.append(env_path)
    prefix = os.environ.get("PREFIX", "")
    if prefix:
        for leaf in ("libusb-1.0.so.0", "libusb-1.0.so"):
            names.append(os.path.join(prefix, "lib", leaf))
    found = ctypes.util.find_library("usb-1.0")
    if found:
        names.append(found)
    names.extend(["libusb-1.0.so.0", "libusb-1.0.so"])
    return names


def _load():
    """返回 (CDLL 或 None, 实际路径或 None)，结果缓存。"""
    if _lib_state["tried"]:
        return _lib_state["lib"], _lib_state["path"]
    _lib_state["tried"] = True
    for name in _candidate_names():
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        _setup_prototypes(lib)
        _lib_state["lib"] = lib
        _lib_state["path"] = name
        return lib, name
    return None, None


def available():
    """libusb 可用则返回 .so 路径，否则返回 None。"""
    return _load()[1]


def _setup_prototypes(lib) -> None:
    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_init.restype = ctypes.c_int
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_exit.restype = None
    lib.libusb_wrap_sys_device.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int64,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.libusb_wrap_sys_device.restype = ctypes.c_int
    lib.libusb_control_transfer.argtypes = [
        ctypes.c_void_p,   # dev_handle
        ctypes.c_uint8,    # bmRequestType
        ctypes.c_uint8,    # bRequest
        ctypes.c_uint16,   # wValue
        ctypes.c_uint16,   # wIndex
        ctypes.POINTER(ctypes.c_uint8),  # data
        ctypes.c_uint16,   # wLength
        ctypes.c_uint,     # timeout
    ]
    lib.libusb_control_transfer.restype = ctypes.c_int
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    lib.libusb_close.restype = None
    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_claim_interface.restype = ctypes.c_int
    lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_release_interface.restype = ctypes.c_int
    lib.libusb_bulk_transfer.argtypes = [
        ctypes.c_void_p,                 # dev_handle
        ctypes.c_uint8,                  # endpoint
        ctypes.POINTER(ctypes.c_uint8),  # data
        ctypes.c_int,                    # length
        ctypes.POINTER(ctypes.c_int),    # transferred
        ctypes.c_uint,                   # timeout
    ]
    lib.libusb_bulk_transfer.restype = ctypes.c_int
    for name in (
        "libusb_kernel_driver_active",
        "libusb_detach_kernel_driver",
        "libusb_attach_kernel_driver",
    ):
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
            fn.restype = ctypes.c_int
    if hasattr(lib, "libusb_error_name"):
        lib.libusb_error_name.argtypes = [ctypes.c_int]
        lib.libusb_error_name.restype = ctypes.c_char_p


def _err(lib, rc: int) -> str:
    try:
        name = lib.libusb_error_name(rc)
    except Exception:  # pragma: no cover - 老版本没有该符号
        name = None
    return f"libusb error {rc}" + (f" ({name.decode()})" if name else "")


def _require():
    lib, path = _load()
    if lib is None:
        raise LibUsbError(
            "找不到 libusb-1.0。请先安装：pkg install libusb"
            "（或用 LIBUSB_PATH 指定 .so 路径）"
        )
    return lib, path


# --------------------------------------------------------------------------- #
# 描述符解析（纯函数，便于单测）
# --------------------------------------------------------------------------- #

def parse_device_descriptor(data: bytes) -> dict:
    """解析 18 字节 device descriptor。"""
    if len(data) < 18:
        raise ValueError(f"device descriptor 太短：{len(data)} 字节")
    return {
        "bcd_usb": f"0x{int.from_bytes(data[2:4], 'little'):04x}",
        "device_class": data[4],
        "device_subclass": data[5],
        "device_protocol": data[6],
        "max_packet_size0": data[7],
        "vid": int.from_bytes(data[8:10], "little"),
        "pid": int.from_bytes(data[10:12], "little"),
        "bcd_device": f"0x{int.from_bytes(data[12:14], 'little'):04x}",
        "i_manufacturer": data[14],
        "i_product": data[15],
        "i_serial": data[16],
        "num_configurations": data[17],
    }


def parse_config_descriptor(data: bytes) -> dict:
    """解析 config descriptor（含其后的 interface / endpoint 描述符）。"""
    if len(data) < 9:
        raise ValueError(f"config descriptor 太短：{len(data)} 字节")
    if data[1] != DESC_CONFIG:
        raise ValueError(f"不是 config descriptor（bDescriptorType=0x{data[1]:02x}）")
    total = int.from_bytes(data[2:4], "little")
    cfg = {
        "total_length": total,
        "num_interfaces": data[4],
        "configuration_value": data[5],
        "attributes": data[7],
        "self_powered": bool(data[7] & 0x40),
        "remote_wakeup": bool(data[7] & 0x20),
        "max_power_ma": data[8] * 2,
        "interfaces": [],
    }
    iface = None
    i = 0
    end = min(len(data), total or len(data))
    while i + 2 <= end:
        blen, btype = data[i], data[i + 1]
        if blen < 2:
            break
        chunk = data[i : i + blen]
        if btype == DESC_INTERFACE and blen >= 9:
            iface = {
                "number": chunk[2],
                "alternate": chunk[3],
                "num_endpoints": chunk[4],
                "class": chunk[5],
                "subclass": chunk[6],
                "protocol": chunk[7],
                "endpoints": [],
            }
            cfg["interfaces"].append(iface)
        elif btype == DESC_ENDPOINT and blen >= 7 and iface is not None:
            addr = chunk[2]
            attr = chunk[3]
            iface["endpoints"].append(
                {
                    "address": addr,
                    "number": addr & 0x0F,
                    "direction": "in" if addr & 0x80 else "out",
                    # 0=control 1=isochronous 2=bulk 3=interrupt
                    "transfer_type": attr & 0x03,
                    "max_packet_size": int.from_bytes(chunk[4:6], "little"),
                    "interval": chunk[6],
                }
            )
        i += blen
    return cfg


def parse_string_descriptor(data: bytes) -> str:
    """解析 string descriptor（UTF-16LE，2 字节头）。"""
    if len(data) < 2:
        return ""
    payload = data[2 : data[0]] if data[0] >= 2 else data[2:]
    try:
        return payload.decode("utf-16-le", "replace").strip("\x00")
    except Exception:  # pragma: no cover
        return ""


def chip_label(vid: int, pid: int):
    """VID/PID -> 芯片名，未知返回 None。"""
    return USB_SERIAL_CHIPS.get((vid, pid))


def is_esp_related(vid: int, pid: int) -> bool:
    return bool(chip_label(vid, pid)) or vid in (0x10C4, 0x1A86, 0x0403, 0x303A, 0x067B)


def pick_serial_endpoints(cfg: dict):
    """从 config 里挑出「像串口」的 bulk in/out 端点。

    优先级：vendor-specific(0xFF) / CDC data(0x0A) / CDC(0x02) 接口，
    要求同时有 bulk-in 与 bulk-out。找不到则退回任意含 bulk in+out 的接口。
    """
    fallback = None
    for iface in cfg.get("interfaces", []):
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
        hit = {
            "interface": iface["number"],
            "interface_class": iface["class"],
            "in": bulk_in["address"],
            "out": bulk_out["address"],
            "in_max_packet": bulk_in["max_packet_size"],
            "out_max_packet": bulk_out["max_packet_size"],
        }
        if iface["class"] in (0xFF, 0x0A, 0x02) and iface["alternate"] == 0:
            return hit
        if fallback is None and iface["alternate"] == 0:
            fallback = hit
    return fallback


# --------------------------------------------------------------------------- #
# 控制传输封装
# --------------------------------------------------------------------------- #

def _ctrl_in(lib, handle, b_request: int, w_value: int, w_index: int, length: int) -> bytes:
    if length <= 0:
        return b""
    buf = (ctypes.c_uint8 * length)()
    rc = lib.libusb_control_transfer(
        handle, _BMREQUEST_IN, b_request, w_value, w_index, buf, length, _TIMEOUT_MS
    )
    if rc < 0:
        raise LibUsbError(_err(lib, rc))
    return bytes(bytearray(buf[:rc]))


def _read_string(lib, handle, index: int, lang_id: int = 0x0409) -> str:
    if not index:
        return ""
    try:
        payload = _ctrl_in(
            lib, handle, _REQ_GET_DESCRIPTOR, (DESC_STRING << 8) | index, lang_id, 255
        )
    except LibUsbError:
        return ""
    return parse_string_descriptor(payload)


def _read_config(lib, handle, index: int = 0) -> dict:
    raw = _ctrl_in(lib, handle, _REQ_GET_DESCRIPTOR, (DESC_CONFIG << 8) | index, 0, 255)
    if len(raw) >= 4:
        total = int.from_bytes(raw[2:4], "little")
        if total > len(raw):
            raw = _ctrl_in(
                lib, handle, _REQ_GET_DESCRIPTOR, (DESC_CONFIG << 8) | index, 0, total
            )
    return parse_config_descriptor(raw)


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #

def identify_fd(fd: int) -> dict:
    """读取 fd 对应 USB 设备的描述符信息。

    只做只读控制传输，不 claim 接口，可与桥接 App 共存（但别同时抢着收发）。
    """
    lib, _ = _require()
    ctx = ctypes.c_void_p()
    rc = lib.libusb_init(ctypes.byref(ctx))
    if rc < 0:
        raise LibUsbError(_err(lib, rc))
    handle = ctypes.c_void_p()
    try:
        rc = lib.libusb_wrap_sys_device(ctx, fd, ctypes.byref(handle))
        if rc < 0:
            raise LibUsbError(
                _err(lib, rc)
                + "：wrap_sys_device 失败，通常说明这个 fd 不是 termux-usb 给的 USB fd，"
                "或者设备已被其它进程独占"
            )
        dev = parse_device_descriptor(
            _ctrl_in(lib, handle, _REQ_GET_DESCRIPTOR, DESC_DEVICE << 8, 0, 18)
        )
        cfg = _read_config(lib, handle, 0)
        info = dict(dev)
        info["vid_pid"] = f"{dev['vid']:04x}:{dev['pid']:04x}"
        info["chip"] = chip_label(dev["vid"], dev["pid"])
        info["manufacturer"] = _read_string(lib, handle, dev["i_manufacturer"])
        info["product"] = _read_string(lib, handle, dev["i_product"])
        info["serial"] = _read_string(lib, handle, dev["i_serial"])
        info["config"] = cfg
        info["serial_endpoints"] = pick_serial_endpoints(cfg)
        return info
    finally:
        if handle:
            lib.libusb_close(handle)
        lib.libusb_exit(ctx)


def describe(info: dict) -> str:
    """把 identify_fd() 的结果格式化成多行文本。"""
    lines = [
        f"VID:PID      {info.get('vid_pid', '?')}",
        f"芯片         {info.get('chip') or '未知（可能不是 USB-UART，或不在内置表里）'}",
        f"厂商         {info.get('manufacturer') or '-'}",
        f"产品         {info.get('product') or '-'}",
        f"序列号       {info.get('serial') or '-'}",
        f"bcdUSB       {info.get('bcd_usb', '?')}   bcdDevice {info.get('bcd_device', '?')}",
        "设备类       class={:#04x} subclass={:#04x} protocol={:#04x}".format(
            info.get("device_class", 0),
            info.get("device_subclass", 0),
            info.get("device_protocol", 0),
        ),
    ]
    cfg = info.get("config") or {}
    lines.append(
        "配置         value={} 接口数={} 供电={}mA{}".format(
            cfg.get("configuration_value", "?"),
            cfg.get("num_interfaces", "?"),
            cfg.get("max_power_ma", "?"),
            " 自供电" if cfg.get("self_powered") else "",
        )
    )
    type_name = {0: "ctrl", 1: "iso", 2: "bulk", 3: "int"}
    for iface in cfg.get("interfaces", []):
        eps = ", ".join(
            "0x{:02x}({},{},{}B)".format(
                e["address"],
                "IN" if e["direction"] == "in" else "OUT",
                type_name.get(e["transfer_type"], "?"),
                e["max_packet_size"],
            )
            for e in iface["endpoints"]
        )
        lines.append(
            "  接口 {}.{}  class={:#04x}/{:#04x}/{:#04x}  端点: {}".format(
                iface["number"],
                iface["alternate"],
                iface["class"],
                iface["subclass"],
                iface["protocol"],
                eps or "-",
            )
        )
    sp = info.get("serial_endpoints")
    if sp:
        lines.append(
            "串口端点     iface {}  IN=0x{:02x}  OUT=0x{:02x}    ← 直连收发要用的端点".format(
                sp["interface"], sp["in"], sp["out"]
            )
        )
    else:
        lines.append("串口端点     未找到 bulk in/out 端点对（不是标准 USB-UART 形态）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 给 usb_cdc.py 用的低层封装
# --------------------------------------------------------------------------- #

def load():
    """返回 (lib, .so 路径)；没装 libusb 抛 LibUsbError。"""
    return _require()


def error_name(lib, rc: int) -> str:
    return _err(lib, rc)


def init_context(lib):
    ctx = ctypes.c_void_p()
    rc = lib.libusb_init(ctypes.byref(ctx))
    if rc < 0:
        raise LibUsbError(_err(lib, rc))
    return ctx


def exit_context(lib, ctx) -> None:
    if ctx:
        lib.libusb_exit(ctx)


def wrap_device(lib, ctx, fd: int):
    handle = ctypes.c_void_p()
    rc = lib.libusb_wrap_sys_device(ctx, fd, ctypes.byref(handle))
    if rc < 0:
        raise LibUsbError(_err(lib, rc))
    return handle


def close_handle(lib, handle) -> None:
    if handle:
        lib.libusb_close(handle)


def device_descriptor(lib, handle) -> dict:
    return parse_device_descriptor(
        _ctrl_in(lib, handle, _REQ_GET_DESCRIPTOR, DESC_DEVICE << 8, 0, 18)
    )


def config_descriptor(lib, handle, index: int = 0) -> dict:
    return _read_config(lib, handle, index)


def read_string(lib, handle, index: int) -> str:
    return _read_string(lib, handle, index)


def control_in(
    lib, handle, bm_request_type: int, b_request: int, w_value: int, w_index: int,
    length: int, timeout_ms: int = _TIMEOUT_MS,
) -> bytes:
    buf = (ctypes.c_uint8 * max(1, length))()
    rc = lib.libusb_control_transfer(
        handle, bm_request_type, b_request, w_value, w_index, buf, length, timeout_ms
    )
    if rc < 0:
        raise LibUsbError(_err(lib, rc))
    return bytes(bytearray(buf[:rc]))


def control_out(
    lib, handle, bm_request_type: int, b_request: int, w_value: int, w_index: int,
    data: bytes = b"", timeout_ms: int = _TIMEOUT_MS,
) -> int:
    payload = (ctypes.c_uint8 * max(1, len(data))).from_buffer_copy(data or b"\x00")
    rc = lib.libusb_control_transfer(
        handle, bm_request_type, b_request, w_value, w_index, payload, len(data), timeout_ms
    )
    if rc < 0:
        raise LibUsbError(_err(lib, rc))
    return rc


def claim_interface(lib, handle, interface: int) -> None:
    rc = lib.libusb_claim_interface(handle, interface)
    if rc < 0:
        raise LibUsbError(_err(lib, rc))


def kernel_driver_active(lib, handle, interface: int) -> bool:
    """该接口上是否绑着内核驱动（Android 上常见：cdc_acm 抢先绑了 CDC 接口）。"""
    fn = getattr(lib, "libusb_kernel_driver_active", None)
    if fn is None:
        return False
    try:
        return fn(handle, interface) == 1
    except Exception:  # pragma: no cover
        return False


def detach_kernel_driver(lib, handle, interface: int) -> bool:
    fn = getattr(lib, "libusb_detach_kernel_driver", None)
    if fn is None:
        return False
    return fn(handle, interface) == 0


def attach_kernel_driver(lib, handle, interface: int) -> bool:
    fn = getattr(lib, "libusb_attach_kernel_driver", None)
    if fn is None:
        return False
    return fn(handle, interface) == 0


def release_interface(lib, handle, interface: int) -> None:
    try:
        lib.libusb_release_interface(handle, interface)
    except Exception:  # pragma: no cover - 释放失败不致命
        pass


def bulk_read(lib, handle, endpoint: int, length: int, timeout_ms: int) -> tuple:
    """返回 (rc, data)。rc == -7 (TIMEOUT) 表示这段时间没有数据。"""
    buf = (ctypes.c_uint8 * max(1, length))()
    transferred = ctypes.c_int(0)
    rc = lib.libusb_bulk_transfer(handle, endpoint, buf, length, ctypes.byref(transferred), timeout_ms)
    if rc < 0:
        return rc, b""
    return 0, bytes(bytearray(buf[: transferred.value]))


def bulk_write(lib, handle, endpoint: int, data: bytes, timeout_ms: int) -> tuple:
    """返回 (rc, 实际发送字节数)。"""
    if not data:
        return 0, 0
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    transferred = ctypes.c_int(0)
    rc = lib.libusb_bulk_transfer(handle, endpoint, buf, len(data), ctypes.byref(transferred), timeout_ms)
    return rc, transferred.value
