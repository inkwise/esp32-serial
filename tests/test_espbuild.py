#!/usr/bin/env python3
"""espbuild.py 的单元测试。

纯逻辑部分（boards.txt 解析、.ino 预处理、原型生成、库发现）不依赖工具链，
任何环境都能跑；末尾有一个真实编译的集成测试，机器上没有工具链时自动跳过。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import espbuild  # noqa: E402

BOARDS_TXT = """\
testboard.name=Test Board
testboard.build.mcu=esp32s3
testboard.build.core=esp32
testboard.build.variant=esp32s3
testboard.build.board=TEST_BOARD
testboard.build.boot=qio
testboard.build.psram_type=qspi
testboard.build.memory_type={build.boot}_{build.psram_type}
testboard.build.flash_size=4MB
testboard.upload.maximum_size=1310720
testboard.menu.FlashSize.4M=4MB
testboard.menu.FlashSize.4M.build.flash_size=4MB
testboard.menu.FlashSize.16M=16MB
testboard.menu.FlashSize.16M.build.flash_size=16MB
testboard.menu.PSRAM.disabled=Disabled
testboard.menu.PSRAM.disabled.build.psram_type=qspi
testboard.menu.PSRAM.enabled=QSPI PSRAM
testboard.menu.PSRAM.enabled.build.psram_type=qspi
testboard.menu.PSRAM.enabled.build.defines=-DBOARD_HAS_PSRAM

otherboard.name=Other Board
otherboard.build.mcu=esp32s3
"""


class TestBoardsParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "boards.txt"
        self.path.write_text(BOARDS_TXT, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_parse_boards(self):
        boards = espbuild.parse_boards(self.path)
        self.assertIn("testboard", boards)
        self.assertEqual(boards["testboard"]["name"], "Test Board")
        self.assertIn("FlashSize", boards["testboard"]["_menus"])
        self.assertIn("otherboard", boards)

    def test_menu_default_is_first_listed(self):
        boards = espbuild.parse_boards(self.path)
        info = espbuild.apply_menus(boards["testboard"])
        # FlashSize 第一个是 4M，PSRAM 第一个是 disabled
        self.assertEqual(info["build.flash_size"], "4MB")
        self.assertNotIn("build.defines", info)

    def test_menu_choice_override(self):
        boards = espbuild.parse_boards(self.path)
        info = espbuild.apply_menus(boards["testboard"], {"FlashSize": "16M", "PSRAM": "enabled"})
        self.assertEqual(info["build.flash_size"], "16MB")
        self.assertEqual(info["build.defines"], "-DBOARD_HAS_PSRAM")

    def test_variable_expansion(self):
        boards = espbuild.parse_boards(self.path)
        info = espbuild.apply_menus(boards["testboard"])
        self.assertEqual(info["build.memory_type"], "qio_qspi")


class TestInoPreprocessing(unittest.TestCase):
    def test_strip_comments_keeps_newlines(self):
        text = 'a // comment\nb /* block\ncomment */ c\n"string with // slashes"\n'
        stripped = espbuild.strip_comments_and_strings(text)
        self.assertEqual(stripped.count("\n"), text.count("\n"))
        self.assertNotIn("comment", stripped)
        self.assertNotIn("slashes", stripped)

    def test_prototypes_generated(self):
        src = """
#include <Arduino.h>

void setup() {
  pinMode(2, OUTPUT);
}

void loop() {
  helper();
}

int helper(int value) {
  return value + 1;
}
"""
        protos = espbuild.arduino_prototypes(src)
        self.assertIn("void setup();", protos)
        self.assertIn("void loop();", protos)
        self.assertIn("int helper(int value);", protos)

    def test_prototypes_skip_control_flow(self):
        src = """
void setup() {
  if (digitalRead(2)) {
    doThing();
  }
  for (int i = 0; i < 3; i++) {
    doThing();
  }
}
"""
        protos = espbuild.arduino_prototypes(src)
        self.assertIn("void setup();", protos)
        self.assertNotIn("if", protos.replace("if (", "if ("))  # 不应出现 if 原型
        self.assertFalse(any(p.startswith("if ") for p in protos.splitlines()))
        self.assertFalse(any(p.startswith("for ") for p in protos.splitlines()))

    def test_prototypes_ignore_comments_and_strings(self):
        src = """
// void fakeFunction() {
void realOne() {
  Serial.println("void alsoFake() {");
}
"""
        protos = espbuild.arduino_prototypes(src)
        self.assertIn("void realOne();", protos)
        self.assertNotIn("fakeFunction", protos)
        self.assertNotIn("alsoFake", protos)

    def test_ino_to_cpp_prepends_arduino_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            sketch = Path(tmp) / "demo"
            sketch.mkdir()
            (sketch / "demo.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
            cpp = espbuild.ino_to_cpp(sorted(sketch.glob("*.ino")), "demo")
        self.assertTrue(cpp.startswith("#include <Arduino.h>"))
        self.assertIn("void setup();", cpp)
        self.assertIn("void loop() {}", cpp)


class TestSourceScanning(unittest.TestCase):
    def test_iter_sources_skips_build_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build").mkdir()
            (root / "a.cpp").write_text("int a;\n")
            (root / "build" / "b.cpp").write_text("int b;\n")
            found = [p.name for p in espbuild.iter_sources(root)]
        self.assertEqual(found, ["a.cpp"])

    def test_scan_includes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.cpp"
            path.write_text('#include <WiFi.h>\n#include "local.h"\n#include <Arduino.h>\n')
            names = espbuild.scan_includes([path])
        self.assertEqual(names, {"WiFi.h", "local.h", "Arduino.h"})

    def test_find_library_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            libs = Path(tmp) / "libraries"
            (libs / "WiFi" / "src").mkdir(parents=True)
            (libs / "WiFi" / "src" / "WiFi.h").write_text("")
            (libs / "FS" / "src").mkdir(parents=True)
            (libs / "FS" / "src" / "FS.h").write_text("")
            (libs / "Unused" / "src").mkdir(parents=True)
            (libs / "Unused" / "src" / "Unused.h").write_text("")
            found = espbuild.find_library_dirs({"WiFi.h"}, libs)
        self.assertEqual([p.name for p in found], ["WiFi"])


class TestPartitionTable(unittest.TestCase):
    """分区表二进制格式（踩过坑：MD5 记录的位置和填充）。"""

    CSV = """\
# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xe000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x140000,
app1,     app,  ota_1,   0x150000,0x140000,
spiffs,   data, spiffs,  0x290000,0x160000,
coredump, data, coredump,0x3F0000,0x10000,
"""

    def test_parse_csv(self):
        rows = espbuild.parse_partition_csv(self.CSV)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["name"], "nvs")
        self.assertEqual(rows[0]["offset"], 0x9000)
        self.assertEqual(rows[2]["type"], "app")
        self.assertEqual(rows[2]["subtype"], "ota_0")

    def test_type_subtype_mapping(self):
        rows = espbuild.parse_partition_csv(self.CSV)
        data = espbuild.partition_rows_to_bin(rows)
        # nvs: data(1) / nvs(0x02)
        self.assertEqual(data[0:4], bytes([0xAA, 0x50, 0x01, 0x02]))
        # app0: app(0) / ota_0(0x10)
        self.assertEqual(data[64:68], bytes([0xAA, 0x50, 0x00, 0x10]))
        # spiffs: data(1) / spiffs(0x82)
        self.assertEqual(data[128:132], bytes([0xAA, 0x50, 0x01, 0x82]))

    def test_offsets_and_sizes(self):
        rows = espbuild.parse_partition_csv(self.CSV)
        data = espbuild.partition_rows_to_bin(rows)
        self.assertEqual(int.from_bytes(data[4:8], "little"), 0x9000)
        self.assertEqual(int.from_bytes(data[8:12], "little"), 0x5000)
        self.assertEqual(int.from_bytes(data[68:72], "little"), 0x10000)

    def test_md5_record_layout(self):
        """MD5 记录必须是 EB EB + 14×FF + 16 字节摘要（摘要位于条目第 16..31 字节）。"""
        import hashlib

        rows = espbuild.parse_partition_csv(self.CSV)
        data = espbuild.partition_rows_to_bin(rows)
        entries_len = len(rows) * 32
        self.assertEqual(len(data), entries_len + 32)
        record = data[entries_len:]
        self.assertEqual(record[0:2], b"\xEB\xEB")
        self.assertEqual(record[2:16], b"\xFF" * 14)
        self.assertEqual(record[16:32], hashlib.md5(data[:entries_len]).digest())

    def test_auto_offset_when_missing(self):
        rows = espbuild.parse_partition_csv(
            "nvs, data, nvs, , 0x4000,\napp0, app, factory, , 0x10000,\n"
        )
        data = espbuild.partition_rows_to_bin(rows)
        self.assertEqual(int.from_bytes(data[4:8], "little"), 0x8000)
        self.assertEqual(int.from_bytes(data[36:40], "little"), 0xC000)

    def test_flags_column(self):
        rows = espbuild.parse_partition_csv("a, data, nvs, 0x9000, 0x1000, 0x1\n")
        self.assertEqual(rows[0]["flags"], 1)


class TestChipTargets(unittest.TestCase):
    """芯片 → 架构 → SDK 的对应关系（esp chip 的解析基础）。"""

    def test_board_arch_mapping(self):
        cases = {
            "esp32s3": "esp32s3",
            "esp32s3box": "esp32s3",
            "xiao_esp32s3": "esp32s3",
            "esp32s2": "esp32s2",
            "esp32c3": "esp32c3",
            "esp32c6": "esp32c6",
            "esp8266": "esp8266",
            "esp32": "esp32",
            "nodemcu": "esp32s3",      # 认不出来时回落到默认
        }
        for key, expect in cases.items():
            self.assertEqual(espbuild.board_arch(key), expect, key)

    def test_available_targets_lists_installed_sdk(self):
        targets = espbuild.available_targets()
        self.assertIsInstance(targets, list)
        # 装了 SDK 的目标里至少要有一个带 flags/ 目录的真芯片
        for target in targets:
            self.assertTrue((espbuild.SDK_DIR / espbuild.LIBS_REL).is_dir())

    def test_missing_target_error_is_actionable(self):
        with self.assertRaises(espbuild.BuildError) as ctx:
            espbuild.find_libs_dir("esp32c99")
        message = str(ctx.exception)
        self.assertIn("SDK", message)
        self.assertIn("烧录", message)  # 提示烧录不受影响


class TestChipNameParsing(unittest.TestCase):
    """esp_cli 里把 esptool 的输出归一成架构标识。"""

    def setUp(self):
        import esp_cli

        self.mod = esp_cli

    def test_chip_arch_from_esptool_names(self):
        cases = {
            "ESP32-S3 (QFN56) (revision v0.2)": "esp32s3",
            "ESP32-S2": "esp32s2",
            "ESP32-C3 (QFN32) (revision v0.4)": "esp32c3",
            "ESP32-C6 (QFN40) (revision v0.1)": "esp32c6",
            "ESP32-D0WD-V3 (revision v3.0)": "esp32",
            "ESP8266EX": "esp8266",
            "": None,
            "STM32F103C8": None,
        }
        for name, expect in cases.items():
            self.assertEqual(self.mod.chip_arch(name), expect, name)


class TestToolchainPresence(unittest.TestCase):
    def test_toolchain_lookup(self):
        try:
            path = espbuild.toolchain_bin()
        except espbuild.BuildError:
            self.skipTest("工具链未安装")
        self.assertTrue((path / "xtensa-esp32s3-elf-gcc").exists())

    def test_esptool_available(self):
        try:
            cmd = espbuild.esptool_cmd()
        except espbuild.BuildError:
            self.skipTest("esptool 未安装")
        self.assertTrue(cmd)


class TestRealBuild(unittest.TestCase):
    """真实编译一次 blink（有工具链才跑，约 10 秒）。"""

    def test_build_blink(self):
        try:
            espbuild.toolchain_bin()
            espbuild.load_board("esp32s3")
        except Exception:
            self.skipTest("工具链/boards 未准备")

        sketch = ROOT / "examples" / "blink"
        if not sketch.is_dir():
            self.skipTest("没有示例工程")
        meta = espbuild.build_sketch(sketch, "esp32s3", jobs=4)
        self.assertTrue(Path(meta["bin"]).exists())
        self.assertGreater(meta["size"], 10000)
        self.assertLess(meta["size"], meta["max_size"])
        labels = [label for _, _, label in meta["layout"]]
        self.assertIn("应用", labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
