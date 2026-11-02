#!/usr/bin/env python3
"""espport.py — 把 USB-Serial-JTAG 的 CDC 链路包装成 pyserial 兼容的串口对象。

这样 esptool 可以**直接**跑在 USB 上（`--port esp-usb://0`），不需要 TCP 桥接或
/dev/pts 中转，DTR/RTS 也由 esptool 自己控（这正是进下载模式需要的东西）。

只实现 esptool 真正会用到的那些属性和方法：
``read`` / ``write`` / ``inWaiting`` / ``flushInput`` / ``reset_input_buffer`` /
``timeout`` / ``dtr`` / ``rts`` / ``setDTR`` / ``setRTS`` / ``name`` / ``close``。
"""

from __future__ import annotations

import time


class SerialException(Exception):
    """与 pyserial 同名的异常，esptool 会捕获它。"""


class UsbPort:
    """薄薄一层：把 UsbCdcLink 伪装成 serial.Serial。"""

    URL_PREFIX = "esp-usb://"

    def __init__(self, link, name: str = "esp-usb://0", timeout: float = 0.1,
                 close_link: bool = False):
        self.link = link
        self.close_link = close_link
        self.name = name
        self.port = name
        self.baudrate = 115200
        self.timeout = timeout
        self.write_timeout = 2.0
        self.inter_byte_timeout = None
        self.xonxoff = False
        self.rtscts = False
        self.dsrdtr = False
        self.is_open = True
        self._dtr = False
        self._rts = False

    # ---------------------------------------------------------------- 控制线
    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value) -> None:
        self._dtr = bool(value)
        self._apply_lines()

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value) -> None:
        self._rts = bool(value)
        self._apply_lines()

    def setDTR(self, value) -> None:
        self.dtr = value

    def setRTS(self, value) -> None:
        self.rts = value

    def _apply_lines(self) -> None:
        try:
            self.link.set_dtr_rts(self._dtr, self._rts)
        except Exception:  # noqa: BLE001 - 设备复位瞬间会失败，忽略
            pass

    # ---------------------------------------------------------------- IO
    def read(self, size: int = 1) -> bytes:
        """pyserial 语义：最多等 timeout 秒，返回已读到的字节（可能少于 size）。"""
        timeout = 0.1 if self.timeout is None else self.timeout
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < size:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            data = self.link.read(min(left, 0.05))
            if data is None:
                raise SerialException("USB 设备已断开")
            if data:
                buf.extend(data)
                deadline = time.monotonic() + timeout
        return bytes(buf)

    def write(self, data) -> int:
        try:
            return self.link.write(bytes(data))
        except Exception as exc:  # noqa: BLE001
            raise SerialException(str(exc)) from exc

    @property
    def in_waiting(self) -> int:
        return 0  # esptool 只是用它判断"有没有多余数据"，返回 0 是安全的

    def inWaiting(self) -> int:
        return self.in_waiting

    def flush(self) -> None:
        pass

    def flushInput(self) -> None:
        pass

    def flushOutput(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def isOpen(self) -> bool:
        """pyserial 的老式方法名；esptool 的复位路径会调用它。"""
        return self.is_open

    # 经典复位路径会顺手查这些调制解调器状态线，USB-UART 桥没有，返回 False 即可
    def getCTS(self) -> bool:
        return False

    def getDSR(self) -> bool:
        return False

    def getRI(self) -> bool:
        return False

    def getCD(self) -> bool:
        return False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        # esptool 干完活会 close() 端口；默认不真的关链路，
        # 否则外层就没法再发复位时序了（复位交给 esp_esptool 收尾做）。
        self.is_open = False
        if self.close_link:
            try:
                self.link.close()
            except Exception:  # noqa: BLE001
                pass

    def __repr__(self) -> str:
        return f"<UsbPort {self.name}>"
