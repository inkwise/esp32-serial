#!/usr/bin/env python3
"""esp_esptool.py — 让 esptool 直接跑在 USB-Serial-JTAG 上（不经 TCP / PTY 中转）。

通常由 ``esp flash`` 自动调用，也可以手动用：

    # 1) 先进下载模式
    esp-serial boot --usb
    # 2) 用 esptool 干活（芯片已在 bootloader，所以 --before no-reset）
    termux-usb -r -E -e \\
      "$HOME/.esp/venv/bin/python $HOME/project/esp32-serial/esp_esptool.py \\
         --chip esp32s3 --before no-reset --after no-reset flash-id" \\
      /dev/bus/usb/001/002

做法：把 ``serial.serial_for_url("esp-usb://0")`` 打补丁指向我们自己的
``espport.UsbPort``（底层是 libusb + CDC），其余参数原样交给 esptool。
干完活默认脉冲 EN 复位，让芯片回到正常固件。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PORT_URL = "esp-usb://0"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    raw_fd = os.environ.get("TERMUX_USB_FD")
    if not raw_fd:
        print(
            "[esp_esptool] 缺少 TERMUX_USB_FD：必须由 termux-usb 启动，"
            "或者直接用 `esp flash` / `esp-serial boot --usb`",
            file=sys.stderr, flush=True,
        )
        return 2
    try:
        fd = int(raw_fd)
    except ValueError:
        print(f"[esp_esptool] TERMUX_USB_FD={raw_fd!r} 不是整数", file=sys.stderr)
        return 2

    try:
        import serial
        import esptool
        import usb_cdc
        from espport import UsbPort
    except ImportError as exc:
        print(
            f"[esp_esptool] 导入失败：{exc}\n"
            "提示：这个脚本要用 esptool 所在的解释器跑（~/.esp/venv/bin/python）",
            file=sys.stderr,
        )
        return 2

    reset_after = "--no-reset-after" not in argv
    argv = [arg for arg in argv if arg != "--no-reset-after"]

    if not any(arg in ("-p", "--port") or arg.startswith("--port=") for arg in argv):
        argv = ["--port", PORT_URL] + argv

    link = usb_cdc.UsbCdcLink(fd, baudrate=115200).open()
    port = UsbPort(link, PORT_URL)

    original_for_url = serial.serial_for_url

    def patched_for_url(url, *args, **kwargs):
        if isinstance(url, str) and url.startswith(UsbPort.URL_PREFIX):
            return port
        return original_for_url(url, *args, **kwargs)

    serial.serial_for_url = patched_for_url

    rc = 0
    try:
        esptool.main(argv)
    except SystemExit as exc:  # esptool 用 sys.exit 表达结果
        rc = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[esp_esptool] esptool 出错：{exc}", file=sys.stderr)
        rc = 1
    finally:
        serial.serial_for_url = original_for_url
        if reset_after:
            try:
                link.hard_reset()
                print("[esp_esptool] 已复位，芯片回到正常固件", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[esp_esptool] 复位失败：{exc}", file=sys.stderr)
        port.close()
        link.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
