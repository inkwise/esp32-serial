#!/usr/bin/env python3
"""esp_serial.py / libusb_probe.py 的单元测试。

全部离线可跑：用一个本地 TCP 服务器模拟 TCPUART 桥接（开机发假 ESP32 日志、
收到数据就回 ``ECHO:`` 前缀），不需要真板子、不需要 libusb。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import esp_serial  # noqa: E402
import libusb_probe  # noqa: E402
import usb_cdc  # noqa: E402

ESP = os.path.join(ROOT, "esp_serial.py")

BOOT_BANNER = (
    b"\r\nESP-ROM:esp32s3-20210327\r\n"
    b"Build:Mar 27 2021\r\n"
    b"rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
    b"entry 0x403c98d4\r\n"
)


# --------------------------------------------------------------------------- #
# 假桥接服务器（相当于 TCPUART）
# --------------------------------------------------------------------------- #
class FakeBridge:
    """监听 127.0.0.1 的随机端口，模拟串口桥接。"""

    def __init__(self, banner: bytes = BOOT_BANNER, echo: bool = True):
        self._stop = threading.Event()
        self.received = bytearray()
        self.clients = 0
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.banner = banner
        self.echo = echo
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            self.clients += 1
            threading.Thread(target=self._serve_client, args=(conn,), daemon=True).start()

    def _serve_client(self, conn):
        try:
            if self.banner:
                conn.sendall(self.banner)
            conn.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                self.received.extend(data)
                if self.echo:
                    try:
                        conn.sendall(b"ECHO:" + data)
                    except OSError:
                        break  # 对端已关，正常情况
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def wait_for(self, needle: bytes, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.received:
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self._stop.set()
        try:
            self.server.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def run_cli(*args, timeout: float = 25.0, stdin_data: bytes | None = None, env=None):
    return subprocess.run(
        [sys.executable, ESP, *args],
        capture_output=True,
        input=stdin_data,
        timeout=timeout,
        env=env,
    )


class BridgeTestCase(unittest.TestCase):
    bridge: FakeBridge

    def setUp(self):
        self.bridge = FakeBridge()
        self.addCleanup(self.bridge.close)
        self.tmpdir = tempfile.mkdtemp(prefix="esp_serial_test_")
        self.addCleanup(self._rm_tmpdir)

    def _rm_tmpdir(self):
        for name in os.listdir(self.tmpdir):
            path = os.path.join(self.tmpdir, name)
            try:
                if os.path.islink(path) or os.path.isfile(path):
                    os.unlink(path)
            except OSError:
                pass
        os.rmdir(self.tmpdir)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
class TestHelpers(unittest.TestCase):
    def test_hexdump_lines(self):
        lines = list(esp_serial.hexdump_lines(b"ESP32", 0))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("00000000  45 53 50 33 32"))
        self.assertIn("|ESP32", lines[0])

    def test_hexdump_offset_and_width(self):
        data = bytes(range(40))
        lines = list(esp_serial.hexdump_lines(data, base_offset=16))
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("00000010"))
        self.assertTrue(lines[2].startswith("00000030"))

    def test_translate_input_cr(self):
        # 单按回车（raw 模式只有一个 \r）必须立刻发出去，不能被缓存吞掉
        out, carry = esp_serial.translate_input(b"print(1)\r", b"\r")
        self.assertEqual(out, b"print(1)\r")
        self.assertEqual(carry, b"")

    def test_translate_input_single_cr(self):
        out, carry = esp_serial.translate_input(b"\r", b"\r")
        self.assertEqual(out, b"\r")
        self.assertEqual(carry, b"")

    def test_translate_input_crlf_in_one_chunk(self):
        out, carry = esp_serial.translate_input(b"a\r\n", b"\r")
        self.assertEqual(out, b"a\r")
        self.assertEqual(carry, b"")

    def test_translate_input_to_crlf(self):
        out, _ = esp_serial.translate_input(b"a\rb\n", b"\r\n")
        self.assertEqual(out, b"a\r\nb\r\n")

    def test_translate_input_lf_and_crlf(self):
        out, _ = esp_serial.translate_input(b"a\r\nb\nc\r", b"\r")
        self.assertEqual(out, b"a\rb\rc\r")  # 3 个行尾（末尾那个 \r 也算）

    def test_enter_sequences(self):
        self.assertEqual(esp_serial.ENTER_SEQ["cr"], b"\r")
        self.assertEqual(esp_serial.ENTER_SEQ["crlf"], b"\r\n")
        self.assertEqual(esp_serial.ENTER_SEQ["none"], b"")

    def test_build_payload_defaults(self):
        ns = argparse.Namespace(text="print(1)", file=None, hex_data=None, cmd=None, append=None)
        self.assertEqual(esp_serial.build_payload(ns), b"print(1)\r")

        ns = argparse.Namespace(text=None, file=None, hex_data="41 42:43", cmd=None, append=None)
        self.assertEqual(esp_serial.build_payload(ns), b"ABC")

    def test_build_payload_file_has_no_cr(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
            handle.write(b"\x00\x01\x02")
            path = handle.name
        self.addCleanup(os.unlink, path)
        ns = argparse.Namespace(text=None, file=path, hex_data=None, cmd=None, append=None)
        self.assertEqual(esp_serial.build_payload(ns), b"\x00\x01\x02")

    def test_human_bytes(self):
        self.assertEqual(esp_serial.human_bytes(512), "512B")
        self.assertIn("KiB", esp_serial.human_bytes(4096))

    def test_parse_port_spec(self):
        self.assertEqual(esp_serial.parse_port_spec("8080,1234", None), [8080, 1234])
        ports = esp_serial.parse_port_spec(None, "9000-9002")
        self.assertEqual(ports, [9000, 9001, 9002])
        self.assertIn(8080, esp_serial.parse_port_spec(None, None))


class TestUsbDescriptorParsing(unittest.TestCase):
    def _device_descriptor(self, vid=0x10C4, pid=0xEA60):
        return (
            bytes([18, 1, 0x00, 0x02, 0, 0, 0, 64])
            + vid.to_bytes(2, "little")
            + pid.to_bytes(2, "little")
            + bytes([0x00, 0x01, 1, 2, 3, 1])
        )

    def _config_descriptor(self):
        iface0 = bytes([9, 4, 0, 0, 2, 0xFF, 0, 0, 4])
        ep0_in = bytes([7, 5, 0x82, 0x02, 0x40, 0x00, 0])
        ep0_out = bytes([7, 5, 0x02, 0x02, 0x40, 0x00, 0])
        iface1 = bytes([9, 4, 1, 0, 2, 0xFF, 0, 0, 4])
        ep1_in = bytes([7, 5, 0x81, 0x02, 0x40, 0x00, 0])
        ep1_out = bytes([7, 5, 0x01, 0x02, 0x40, 0x00, 0])
        body = iface0 + ep0_in + ep0_out + iface1 + ep1_in + ep1_out
        total = 9 + len(body)
        header = bytes([9, 2]) + total.to_bytes(2, "little") + bytes([2, 1, 0, 0x80, 50])
        return header + body

    def test_device_descriptor(self):
        info = libusb_probe.parse_device_descriptor(self._device_descriptor())
        self.assertEqual(info["vid"], 0x10C4)
        self.assertEqual(info["pid"], 0xEA60)
        self.assertEqual(info["bcd_usb"], "0x0200")
        self.assertEqual(info["num_configurations"], 1)
        self.assertEqual(info["i_product"], 2)

    def test_device_descriptor_too_short(self):
        with self.assertRaises(ValueError):
            libusb_probe.parse_device_descriptor(b"\x12\x01")

    def test_config_descriptor(self):
        cfg = libusb_probe.parse_config_descriptor(self._config_descriptor())
        self.assertEqual(cfg["num_interfaces"], 2)
        self.assertEqual(cfg["max_power_ma"], 100)
        self.assertFalse(cfg["self_powered"])  # 0x80 = 总线供电
        self.assertEqual(len(cfg["interfaces"]), 2)
        first = cfg["interfaces"][0]
        self.assertEqual(first["class"], 0xFF)
        self.assertEqual(len(first["endpoints"]), 2)
        self.assertEqual(first["endpoints"][0]["direction"], "in")
        self.assertEqual(first["endpoints"][0]["transfer_type"], 2)
        self.assertEqual(first["endpoints"][0]["max_packet_size"], 64)

    def test_config_descriptor_bad_type(self):
        with self.assertRaises(ValueError):
            libusb_probe.parse_config_descriptor(bytes([9, 1, 0, 0, 0, 1, 0, 0x80, 50]))

    def test_pick_serial_endpoints(self):
        cfg = libusb_probe.parse_config_descriptor(self._config_descriptor())
        pick = libusb_probe.pick_serial_endpoints(cfg)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["in"], 0x82)
        self.assertEqual(pick["out"], 0x02)
        self.assertEqual(pick["interface"], 0)

    def test_pick_serial_endpoints_none(self):
        cfg = {"interfaces": [{"number": 0, "alternate": 0, "class": 3,
                              "endpoints": [{"address": 0x81, "direction": "in",
                                             "transfer_type": 3, "max_packet_size": 8}]}]}
        self.assertIsNone(libusb_probe.pick_serial_endpoints(cfg))

    def test_string_descriptor(self):
        name = "CP2102 USB to UART Bridge"
        raw = bytes([len(name) * 2 + 2, 3]) + name.encode("utf-16-le")
        self.assertEqual(libusb_probe.parse_string_descriptor(raw), name)
        self.assertEqual(libusb_probe.parse_string_descriptor(b""), "")

    def test_chip_label(self):
        self.assertIn("CP2102", libusb_probe.chip_label(0x10C4, 0xEA60))
        self.assertIn("CH340", libusb_probe.chip_label(0x1A86, 0x7523))
        self.assertIn("ESP32", libusb_probe.chip_label(0x303A, 0x1001))
        self.assertIsNone(libusb_probe.chip_label(0x1234, 0x5678))
        self.assertTrue(libusb_probe.is_esp_related(0x10C4, 0xEA60))
        self.assertFalse(libusb_probe.is_esp_related(0x1234, 0x5678))

    def test_describe_contains_key_lines(self):
        info = {
            "vid": 0x10C4, "pid": 0xEA60, "vid_pid": "10c4:ea60",
            "chip": "CP2102/CP2104 USB-UART (Silicon Labs)",
            "manufacturer": "Silicon Labs", "product": "CP2102 USB to UART Bridge",
            "serial": "0001", "bcd_usb": "0x0200", "bcd_device": "0x0100",
            "device_class": 0, "device_subclass": 0, "device_protocol": 0,
            "config": libusb_probe.parse_config_descriptor(self._config_descriptor()),
            "serial_endpoints": {"interface": 0, "in": 0x82, "out": 0x02,
                                 "interface_class": 0xFF,
                                 "in_max_packet": 64, "out_max_packet": 64},
        }
        text = libusb_probe.describe(info)
        self.assertIn("10c4:ea60", text)
        self.assertIn("CP2102", text)
        self.assertIn("串口端点", text)
        self.assertIn("0x82", text)


class TestLibusbOptional(unittest.TestCase):
    def test_identify_without_fd_env(self):
        env = dict(os.environ)
        env.pop("TERMUX_USB_FD", None)
        proc = run_cli("_usb-identify", env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"TERMUX_USB_FD", proc.stderr)

    def test_libusb_state_is_reported(self):
        path = libusb_probe.available()
        self.assertTrue(path is None or isinstance(path, str))


# --------------------------------------------------------------------------- #
# usb identify 的参数串联（用 stub termux-usb，不弹真授权框）
# --------------------------------------------------------------------------- #
STUB_TERMUX_USB = """#!/bin/bash
dev=""; cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    -l) echo '["/dev/bus/usb/001/002"]'; exit 0;;
    -r|-E) shift;;
    -e) cmd="$2"; shift 2;;
    *) dev="$1"; shift;;
  esac
done
echo "[stub] device=$dev" >&2
echo "[stub] cmd=$cmd" >&2
exec 3</dev/null
TERMUX_USB_FD=3 exec bash -c "$cmd"
"""


class TestUsbIdentifyPlumbing(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="esp_usbstub_")
        stub = os.path.join(self.tmpdir, "termux-usb")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB_TERMUX_USB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = self.tmpdir + os.pathsep + self.env.get("PATH", "")
        self.env.pop("TERMUX_USB_FD", None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def test_identify_runs_through_termux_usb(self):
        proc = run_cli("usb", "identify", "/dev/bus/usb/001/002", env=self.env)
        stderr = proc.stderr.decode()
        self.assertIn("[stub] device=/dev/bus/usb/001/002", stderr)
        self.assertIn("_usb-identify", stderr)
        # 有 libusb 就真读描述符（0/3），没有就报缺依赖（3）
        self.assertIn(proc.returncode, (0, 3), proc.stderr)

    def test_identify_json_flag_is_forwarded(self):
        proc = run_cli("usb", "identify", "--json", env=self.env)
        self.assertIn("_usb-identify --json", proc.stderr.decode())
        self.assertIn(proc.returncode, (0, 3), proc.stderr)

    def test_usb_list_uses_stub(self):
        proc = run_cli("usb", "list", "--json", env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["devices"], ["/dev/bus/usb/001/002"])


# --------------------------------------------------------------------------- #
# USB CDC 直连（纯函数部分）
# --------------------------------------------------------------------------- #
def esp32s3_config():
    """ESP32-S3 原生 USB 的配置描述符：CDC 控制 + CDC 数据 + JTAG 三个接口。"""
    def descr(*parts):
        return bytes(parts)

    cd_control = descr(9, 4, 0, 0, 1, 0x02, 0x02, 0x00, 5)
    cd_control_ep = descr(7, 5, 0x82, 0x03, 0x40, 0x00, 8)
    cd_data = descr(9, 4, 1, 0, 2, 0x0A, 0x02, 0x00, 6)
    data_out = descr(7, 5, 0x01, 0x02, 0x40, 0x00, 0)
    data_in = descr(7, 5, 0x81, 0x02, 0x40, 0x00, 0)
    jtag = descr(9, 4, 2, 0, 2, 0xFF, 0xFF, 0x01, 7)
    jtag_out = descr(7, 5, 0x02, 0x02, 0x40, 0x00, 0)
    jtag_in = descr(7, 5, 0x83, 0x02, 0x40, 0x00, 0)
    body = cd_control + cd_control_ep + cd_data + data_out + data_in + jtag + jtag_out + jtag_in
    header = bytes([9, 2]) + (9 + len(body)).to_bytes(2, "little") + bytes([3, 1, 0, 0x80, 250])
    return header + body


class TestUsbCdc(unittest.TestCase):
    def test_line_coding_115200_8n1(self):
        self.assertEqual(usb_cdc.line_coding(115200), b"\x00\xc2\x01\x00\x00\x00\x08")
        self.assertEqual(len(usb_cdc.line_coding(9600)), 7)

    def test_find_cdc_interfaces_esp32s3(self):
        cfg = libusb_probe.parse_config_descriptor(esp32s3_config())
        found = usb_cdc.find_cdc_interfaces(cfg)
        self.assertIsNotNone(found)
        self.assertEqual(found["control"], 0)
        self.assertEqual(found["data"], 1)
        self.assertEqual(found["in"], 0x81)
        self.assertEqual(found["out"], 0x01)

    def test_find_cdc_interfaces_none_for_vendor_bridge(self):
        # CP2102 那种只有 vendor-specific 接口的桥，直连模式要拒绝
        iface = bytes([9, 4, 0, 0, 2, 0xFF, 0, 0, 4])
        ep_in = bytes([7, 5, 0x82, 0x02, 0x40, 0x00, 0])
        ep_out = bytes([7, 5, 0x02, 0x02, 0x40, 0x00, 0])
        body = iface + ep_in + ep_out
        raw = bytes([9, 2]) + (9 + len(body)).to_bytes(2, "little") + bytes([1, 1, 0, 0x80, 50]) + body
        cfg = libusb_probe.parse_config_descriptor(raw)
        self.assertIsNone(usb_cdc.find_cdc_interfaces(cfg))

    def test_find_cdc_interfaces_needs_both_directions(self):
        iface = bytes([9, 4, 1, 0, 1, 0x0A, 0x00, 0, 0])
        ep_in = bytes([7, 5, 0x81, 0x02, 0x40, 0x00, 0])
        body = iface + ep_in
        raw = bytes([9, 2]) + (9 + len(body)).to_bytes(2, "little") + bytes([1, 1, 0, 0x80, 50]) + body
        cfg = libusb_probe.parse_config_descriptor(raw)
        self.assertIsNone(usb_cdc.find_cdc_interfaces(cfg))

    def test_usb_link_not_selectable(self):
        self.assertFalse(usb_cdc.UsbCdcLink(0).can_select())
        self.assertEqual(usb_cdc.UsbCdcLink(0).fileno(), -1)

    def test_from_env_requires_fd(self):
        env_backup = os.environ.pop("TERMUX_USB_FD", None)
        self.addCleanup(lambda: os.environ.__setitem__("TERMUX_USB_FD", env_backup) if env_backup else None)
        with self.assertRaises(esp_serial.LinkClosed):
            usb_cdc.from_env()

    def test_base_links_are_selectable(self):
        self.assertTrue(esp_serial.Link().can_select())
        self.assertTrue(esp_serial.TcpLink("127.0.0.1", 1).can_select())


class TestLiveOutput(unittest.TestCase):
    """termux-usb 会缓存子进程 stdout，所以 --usb 模式改用 FIFO 回传实时输出。"""

    def _make_fifo(self):
        tmpdir = tempfile.mkdtemp(prefix="esp_fifo_")
        fifo = os.path.join(tmpdir, "out.fifo")
        os.mkfifo(fifo, 0o600)
        self.addCleanup(shutil.rmtree, tmpdir, True)
        return fifo

    def test_child_writes_through_fifo(self):
        fifo = self._make_fifo()
        env = dict(os.environ)
        env[esp_serial.OUT_FIFO_ENV] = fifo
        rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, rfd)
        code = (
            "import sys; sys.path.insert(0, %r); import esp_serial; "
            "esp_serial.rebind_stdout(); "
            "print('LIVE-TEXT', flush=True); "
            "sys.stdout.buffer.write(b'LIVE-BIN'); sys.stdout.buffer.flush()"
        ) % ROOT
        proc = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, timeout=20
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = b""
        deadline = time.time() + 5
        while time.time() < deadline:
            ready, _, _ = select.select([rfd], [], [], 0.2)
            if not ready:
                continue
            chunk = os.read(rfd, 4096)
            if not chunk:
                break
            got += chunk
        self.assertIn(b"LIVE-TEXT", got)
        self.assertIn(b"LIVE-BIN", got)

    def test_without_env_stdout_is_untouched(self):
        env = dict(os.environ)
        env.pop(esp_serial.OUT_FIFO_ENV, None)
        code = (
            "import sys; sys.path.insert(0, %r); import esp_serial; "
            "esp_serial.rebind_stdout(); print('PLAIN-STDOUT')"
        ) % ROOT
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"PLAIN-STDOUT", proc.stdout)

    def test_missing_fifo_falls_back(self):
        env = dict(os.environ)
        env[esp_serial.OUT_FIFO_ENV] = "/definitely/not/here.fifo"
        code = (
            "import sys; sys.path.insert(0, %r); import esp_serial; "
            "esp_serial.rebind_stdout(); print('FALLBACK-OK')"
        ) % ROOT
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"FALLBACK-OK", proc.stdout)


class TestUsbReexec(unittest.TestCase):
    """--usb 要把命令重新包进 termux-usb，这里用 stub 验证参数串联。"""

    STUB = """#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in
    -l) echo '["/dev/bus/usb/001/002"]'; exit 0;;
    -r|-E) shift;;
    -e) echo "[stub] -e=$2" >&2; shift 2;;
    *) echo "[stub] dev=$1" >&2; shift;;
  esac
done
exit 0
"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="esp_reexec_")
        stub = os.path.join(self.tmpdir, "termux-usb")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(self.STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = self.tmpdir + os.pathsep + self.env.get("PATH", "")
        self.env.pop("TERMUX_USB_FD", None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def test_usb_flag_reexecs_through_termux_usb(self):
        proc = run_cli("mon", "--usb", "--stamp", env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        err = proc.stderr.decode()
        self.assertIn("[stub] dev=/dev/bus/usb/001/002", err)
        self.assertIn("_usb-link mon --stamp", err)

    def test_usb_flag_keeps_other_args(self):
        proc = run_cli("send", "--usb", "--text", "hi", "--append", "none", env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("_usb-link send --text hi --append none", proc.stderr.decode())

    def test_usb_without_subcommand(self):
        proc = run_cli("--usb", env=self.env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--usb 要跟一个子命令", proc.stderr.decode())


# --------------------------------------------------------------------------- #
# 链路
# --------------------------------------------------------------------------- #
class TestLinks(BridgeTestCase):
    def test_fd_link_roundtrip(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        link = esp_serial.FdLink(read_fd, name="pipe", close_fd=False)
        self.addCleanup(link.close)
        os.write(write_fd, b"hello")
        data = link.read(1.0)
        self.assertEqual(data, b"hello")
        self.assertEqual(link.read(0.05), b"")

    def test_tcp_link_read_and_write(self):
        link = esp_serial.open_endpoint(str(self.bridge.port))
        self.addCleanup(link.close)
        self.assertEqual(link.describe(), f"tcp://127.0.0.1:{self.bridge.port}")
        data = link.read(2.0)
        self.assertIn(b"ESP-ROM", data)
        link.write(b"ping\r")
        self.assertTrue(self.bridge.wait_for(b"ping\r"))

    def test_open_endpoint_forms(self):
        forms = [
            str(self.bridge.port),
            f"127.0.0.1:{self.bridge.port}",
            f"tcp://127.0.0.1:{self.bridge.port}",
            f"socket://127.0.0.1:{self.bridge.port}",
        ]
        for form in forms:
            link = esp_serial.open_endpoint(form)
            self.addCleanup(link.close)
            self.assertIsInstance(link, esp_serial.TcpLink)

    def test_open_endpoint_unparsable(self):
        with self.assertRaises(SystemExit):
            esp_serial.open_endpoint("nothing-here")

    def test_connect_failure_message(self):
        with self.assertRaises(esp_serial.LinkClosed) as ctx:
            esp_serial.connect_tcp("127.0.0.1", 1, retries=0)
        self.assertIn("连不上", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 命令：list / scan / doctor
# --------------------------------------------------------------------------- #
class TestDiscoveryCommands(BridgeTestCase):
    def test_scan_finds_bridge(self):
        found = esp_serial.scan_ports("127.0.0.1", [self.bridge.port, 1])
        self.assertEqual(found, [self.bridge.port])

    def test_scan_cli_json(self):
        proc = run_cli("scan", "--ports", str(self.bridge.port), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["open"], [self.bridge.port])

    def test_scan_cli_probe_reads_banner(self):
        proc = run_cli("scan", "--ports", str(self.bridge.port), "--probe", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("ESP", bytes.fromhex(payload["samples"][str(self.bridge.port)]).decode("latin-1"))

    def test_list_cli_json(self):
        proc = run_cli("list", "--json", "--no-scan")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload["termux_usb_available"], bool)
        self.assertIsInstance(payload["termux_usb_devices"], list)

    def test_list_cli_reports_open_port(self):
        proc = run_cli("list", "--json", "--ports", str(self.bridge.port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn(self.bridge.port, payload["open_ports"])

    def test_doctor_cli_json(self):
        proc = run_cli("doctor", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        names = [c["name"] for c in payload["checks"]]
        self.assertIn("termux-usb", names)
        self.assertIn("libusb", names)
        self.assertIn("本地桥接端口", names)

    def test_doctor_human_output(self):
        proc = run_cli("doctor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = proc.stdout.decode()
        self.assertIn("环境体检", text)
        self.assertIn("termux-usb", text)


# --------------------------------------------------------------------------- #
# 命令：mon
# --------------------------------------------------------------------------- #
class TestMon(BridgeTestCase):
    def test_mon_stamped(self):
        proc = run_cli("mon", "--port", str(self.bridge.port), "--stamp", "--duration", "1.2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = proc.stdout.decode()
        self.assertIn("ESP-ROM", text)
        self.assertIn("boot:0x8", text)
        self.assertRegex(text, r"\[\d\d:\d\d:\d\d\.\d{3}\] ")

    def test_mon_hex(self):
        proc = run_cli("mon", "--port", str(self.bridge.port), "--hex", "--duration", "1.0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = proc.stdout.decode()
        self.assertIn("00000000", text)
        self.assertIn("45 53 50", text.upper())  # "ESP"

    def test_mon_log_file(self):
        log_path = os.path.join(self.tmpdir, "esp.log")
        proc = run_cli("mon", "--port", str(self.bridge.port), "--log", log_path, "--duration", "1.0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(log_path, "rb") as handle:
            raw = handle.read()
        self.assertIn(b"ESP-ROM", raw)
        self.assertIn(b"entry 0x403c98d4", raw)

    def test_mon_exit_on_pattern(self):
        proc = run_cli("mon", "--port", str(self.bridge.port), "--exit-on", "entry 0x")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"entry 0x403c98d4", proc.stdout)
        self.assertIn("命中退出条件".encode(), proc.stderr)

    def test_mon_connect_failure(self):
        proc = run_cli("mon", "--port", "1", "--duration", "0.5")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("连不上".encode(), proc.stderr)


# --------------------------------------------------------------------------- #
# 命令：send / exec
# --------------------------------------------------------------------------- #
class TestSendExec(BridgeTestCase):
    def test_send_text_appends_cr(self):
        proc = run_cli("send", "--port", str(self.bridge.port), "--text", "hello")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.bridge.wait_for(b"hello\r"))

    def test_send_hex(self):
        proc = run_cli("send", "--port", str(self.bridge.port), "--hex", "01 03 00 00", "--append", "none")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.bridge.wait_for(b"\x01\x03\x00\x00"))

    def test_send_file_in_chunks(self):
        path = os.path.join(self.tmpdir, "fw.bin")
        payload = bytes(range(64))
        with open(path, "wb") as handle:
            handle.write(payload)
        proc = run_cli("send", "--port", str(self.bridge.port), "--file", path,
                       "--chunk", "16", "--delay", "5", "--append", "none")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.bridge.wait_for(payload))

    def test_exec_until_pattern(self):
        proc = run_cli("exec", "--port", str(self.bridge.port), "print(1+1)", "--until", "ECHO:")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"ECHO:print(1+1)", proc.stdout)

    def test_exec_timeout_returns_3(self):
        proc = run_cli("exec", "--port", str(self.bridge.port), "hi",
                       "--until", "NEVER_MATCHES", "--timeout", "1.0")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("超时".encode(), proc.stderr)

    def test_exec_idle_without_pattern(self):
        proc = run_cli("exec", "--port", str(self.bridge.port), "ping",
                       "--timeout", "2.0", "--idle", "0.3", "--strip-echo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"ECHO:ping", proc.stdout)

    def test_repl_pipe_mode(self):
        proc = run_cli("repl", "--port", str(self.bridge.port), stdin_data=b"abc\r\ndef\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.bridge.wait_for(b"abc"))
        self.assertIn(b"ECHO:", proc.stdout)


# --------------------------------------------------------------------------- #
# 命令：repl 在真 tty 上的按键行为
# --------------------------------------------------------------------------- #
class TestReplRawMode(unittest.TestCase):
    """用 pty 模拟真终端：raw 模式下 Ctrl-C / Ctrl-] 必须真的能退出。"""

    def _run_repl(self, keys: bytes, extra=(), settle: float = 2.0):
        import pty

        bridge = FakeBridge()
        self.addCleanup(bridge.close)
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, ESP, "repl", f"tcp://127.0.0.1:{bridge.port}", *extra],
            stdin=slave, stdout=slave, stderr=slave, preexec_fn=os.setsid,
        )
        os.close(slave)
        try:
            time.sleep(settle)
            while select.select([master], [], [], 0.05)[0]:
                try:
                    os.read(master, 4096)
                except OSError:
                    break
            os.write(master, keys)
            try:
                code = proc.wait(timeout=6)
                exited = True
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                exited, code = False, None
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        return exited, code, bytes(bridge.received)

    def test_ctrl_c_exits(self):
        exited, code, received = self._run_repl(b"")
        self.assertTrue(exited, "Ctrl-C 没能退出 repl")
        self.assertEqual(code, 0)
        self.assertEqual(received, b"", "Ctrl-C 不该被转发给板子")

    def test_ctrl_bracket_exits(self):
        exited, code, _ = self._run_repl(b"")
        self.assertTrue(exited, "Ctrl-] 没能退出 repl")

    def test_forward_ctrl_c_option(self):
        exited, _, received = self._run_repl(b"", extra=("--forward-ctrl-c",))
        self.assertFalse(exited, "--forward-ctrl-c 下不该退出")
        self.assertEqual(received, b"", "Ctrl-C 应该被转发给板子")


# --------------------------------------------------------------------------- #
# 命令：pty（socat 替代）
# --------------------------------------------------------------------------- #
class TestPty(BridgeTestCase):
    def test_pty_relays_and_creates_stable_link(self):
        link_file = os.path.join(self.tmpdir, "esp32")
        proc = subprocess.Popen(
            [sys.executable, ESP, "pty", "--port", str(self.bridge.port),
             "--link-file", link_file, "--no-reconnect"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._terminate, proc)
        try:
            slave_name = proc.stdout.readline().strip()
            self.assertTrue(slave_name.startswith("/dev/pts/"), slave_name)

            deadline = time.time() + 3
            while time.time() < deadline and not os.path.islink(link_file):
                time.sleep(0.02)
            self.assertTrue(os.path.islink(link_file))
            self.assertEqual(os.readlink(link_file), slave_name)

            fd = os.open(slave_name, os.O_RDWR | os.O_NOCTTY)
            try:
                os.write(fd, b"ping\r")
                self.assertTrue(self.bridge.wait_for(b"ping\r"), "数据没到桥接端")
                ready, _, _ = select.select([fd], [], [], 3.0)
                self.assertTrue(ready, "桥接回来的数据没到 PTY")
                echoed = os.read(fd, 4096)
                self.assertIn(b"ECHO:ping", echoed)
            finally:
                os.close(fd)
        finally:
            self._terminate(proc)

    def test_pty_exits_when_no_reconnect_and_no_bridge(self):
        proc = run_cli("pty", "--port", "1", "--no-reconnect")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("连不上".encode(), proc.stderr)

    @staticmethod
    def _terminate(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
# CLI 杂项
# --------------------------------------------------------------------------- #
class TestCliMisc(unittest.TestCase):
    def test_help(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"TCPUART", proc.stdout)

    def test_version(self):
        proc = run_cli("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(esp_serial.VERSION.encode(), proc.stdout)

    def test_no_args_prints_help(self):
        proc = run_cli()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("<命令>".encode(), proc.stdout)

    def test_esptool_absent_still_reports(self):
        proc = run_cli("flash", "--", "version")
        # 有 esptool 就跑，没有就报缺依赖；两种都不能崩
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assertTrue(proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
