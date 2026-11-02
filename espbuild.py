#!/usr/bin/env python3
"""espbuild.py — 在 Termux(bionic) 上原生编译 Arduino-ESP32 工程。

原理
----
工具链不是 glibc 版、也不需要 proot：编译器来自手机里 ArduinoDroid 的 APK
（它把 xtensa-esp32s3-elf-gcc / cc1 / as / ld 全部交叉编译成了 Android aarch64
可执行文件），我们从 ``.apks`` 里抽出来，补齐 crosstool-NG 期望的目录布局，
再用它编译官方的 arduino-esp32 3.x 核心 + IDF 预编译库。

一条编译流水线：
    1. .ino 预处理（拼成 .cpp + 生成函数原型）
    2. 编译 sketch / core / variant / 用到的库   （并行）
    3. 打包 core.a
    4. 链接成 .elf（用 SDK 的 ld 脚本 + IDF 静态库）
    5. esptool elf2image → 可烧录 .bin
    6. 拼出完整 flash 布局（bootloader / partition / boot_app0 / app）

目录约定（由 ``esp setup`` 准备）::

    ~/.esp/ad/            从 APK 解出来的东西
        bin/              98 个 bionic 可执行文件
        sdk/              arduino core + idf libs + 工具链 sysroot
    ~/.esp/tc/bin/        补齐布局后的工具链入口（xtensa-esp32s3-elf-gcc 等）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
ESP_ROOT = Path(os.environ.get("ESP_ROOT", HOME / ".esp"))
AD_DIR = ESP_ROOT / "ad"
SDK_DIR = AD_DIR / "sdk"
BIN_DIR = AD_DIR / "bin"
TC_DIR = ESP_ROOT / "tc"

CORE_REL = "hardware/esp32"
LIBS_REL = "tools/esp32-arduino-libs"

C_SRC_EXT = {".c"}
CXX_SRC_EXT = {".cpp", ".cc", ".cxx"}
ASM_SRC_EXT = {".S", ".s"}


class BuildError(RuntimeError):
    pass


def _clean_define(value: str) -> str:
    """board 里的 loop_core 形如 -DARDUINO_RUNNING_CORE=1，取到 = 之后的部分。"""
    return value[2:] if value.startswith("-D") else value


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# 环境定位
# --------------------------------------------------------------------------- #

def find_core_dir() -> Path:
    """返回 hardware/esp32/<版本> 目录（取版本号最大的）。"""
    base = SDK_DIR / CORE_REL
    if not base.is_dir():
        raise BuildError(f"找不到核心目录 {base}，先跑 esp setup")
    versions = [p for p in base.iterdir() if p.is_dir() and re.match(r"^\d", p.name)]
    if not versions:
        raise BuildError(f"{base} 下没有版本目录")
    versions.sort(key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)])
    return versions[-1]


def available_targets() -> list:
    """已安装 SDK 的目标芯片列表，如 ['esp32s3']。"""
    base = SDK_DIR / LIBS_REL
    targets = set()
    if base.is_dir():
        for release in base.iterdir():
            if not release.is_dir():
                continue
            for child in release.iterdir():
                # 目标目录里有 flags/ 和 lib/ 才算一个真正的芯片 SDK
                if child.is_dir() and (child / "flags").is_dir():
                    targets.add(child.name)
    return sorted(targets)


def boards_file_for(arch: str) -> Path:
    """某颗芯片对应的 boards.txt（由 esp setup 从 APK 的 res/raw 导出）。"""
    path = ESP_ROOT / f"boards_{arch}.txt"
    if path.exists():
        return path
    # 兼容：只有 esp32s3 的文件时给出可操作提示
    raise BuildError(
        f"没有 {arch} 的板子定义（{path} 不存在）。\n"
        "  板子表来自 ArduinoDroid 的 res/raw/*_boards.txt，重新跑 `esp setup --force` 可以再导一遍；"
        "如果 APK 里本来就没有这颗芯片的表，那就只能换一个带它的 APK。"
    )


def find_libs_dir(target: str = "esp32s3") -> Path:
    """返回 IDF 预编译库目录 .../esp32-arduino-libs/<idf-release>/<target>。"""
    base = SDK_DIR / LIBS_REL
    if not base.is_dir():
        raise BuildError(f"找不到 IDF 库目录 {base}")
    for cand in sorted(base.iterdir()):
        if (cand / target).is_dir():
            return cand / target
    installed = available_targets() or ["（无）"]
    raise BuildError(
        f"本机没有 {target} 的 SDK（已装：{', '.join(installed)}）。\n"
        "  编译某个芯片需要 ArduinoDroid 对应的 SDK 包"
        f"（APK 里的 split_{target}_sdk.apk）。\n"
        "  烧录不受影响——esptool 会自动识别芯片，可以直接烧现成的 .bin。"
    )


def toolchain_bin() -> Path:
    path = TC_DIR / "bin"
    if not (path / "xtensa-esp32s3-elf-gcc").exists():
        raise BuildError(f"工具链没准备好（{path}），先跑 esp setup")
    return path


def esptool_cmd() -> list:
    """优先用 venv 里的 esptool。"""
    venv_py = ESP_ROOT / "venv" / "bin" / "python"
    if venv_py.exists():
        return [str(venv_py), "-m", "esptool"]
    found = shutil.which("esptool")
    if found:
        return [found]
    raise BuildError("没找到 esptool：~/.esp/venv/bin/pip install pyserial esptool")


# --------------------------------------------------------------------------- #
# boards.txt 解析
# --------------------------------------------------------------------------- #

_ARCH_HINTS = ("esp32s3", "esp32s2", "esp32c6", "esp32c3", "esp32c2", "esp8266", "esp32")


def board_arch(board_key: str) -> str:
    """把板子 key 归到芯片架构上（如 esp32s3box → esp32s3）。"""
    key = board_key.lower()
    for arch in _ARCH_HINTS:
        if key.startswith(arch):
            return arch
    return "esp32s3"


def parse_boards(boards_file: Path) -> dict:
    """把 boards.txt 解析成 {board_key: {prop: value}}（含 menu 展开）。"""
    props = {}
    for raw in boards_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key] = value

    boards = {}
    keys = {k.split(".")[0] for k in props if "." in k}
    for board in keys:
        info = {}
        prefix = board + "."
        for key, value in props.items():
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if rest.startswith("menu."):
                continue
            info[rest] = value
        if "name" not in info:
            continue
        # 菜单项：{menu: {option: {prop: value}}}
        menus = {}
        mprefix = prefix + "menu."
        for key, value in props.items():
            if not key.startswith(mprefix):
                continue
            parts = key[len(mprefix):].split(".")
            if len(parts) < 3:
                continue
            menu, option, prop = parts[0], parts[1], ".".join(parts[2:])
            menus.setdefault(menu, {}).setdefault(option, {})[prop] = value
        info["_menus"] = menus
        boards[board] = info
    return boards


def apply_menus(board: dict, choices: dict | None = None) -> dict:
    """把菜单默认值（可被 choices 覆盖）合并进板子属性。"""
    choices = choices or {}
    info = {k: v for k, v in board.items() if k != "_menus"}
    for menu, options in (board.get("_menus") or {}).items():
        pick = choices.get(menu)
        if pick is None:
            # boards.txt 里第一个列出的就是 Arduino IDE 的默认值
            pick = next(iter(options))
        info.update(options.get(pick, {}))
    return _expand_vars(info)


def _expand_vars(info: dict) -> dict:
    """展开 boards.txt 里的 {build.xxx} 引用（如 memory_type={build.boot}_{build.psram_type}）。"""
    for _ in range(4):
        changed = False
        for key, value in list(info.items()):
            if not isinstance(value, str) or "{" not in value:
                continue
            def repl(match):
                ref = match.group(1)
                if ref in info and isinstance(info[ref], str):
                    return info[ref]
                return match.group(0)
            new = re.sub(r"\{([\w.]+)\}", repl, value)
            if new != value:
                info[key] = new
                changed = True
        if not changed:
            break
    return info


# --------------------------------------------------------------------------- #
# .ino → .cpp
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.S)


def strip_comments_and_strings(text: str) -> str:
    """把注释和字符串替换成等长空白，便于做括号/大括号扫描。"""
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))
    return _TOKEN_RE.sub(blank, text)


def arduino_prototypes(source: str) -> str:
    """从 .ino 源码里挑出函数定义，生成前置声明。

    Arduino IDE 用 ctags 做这件事；这里用一个够用的启发式扫描：
    在顶层作用域上找 ``return_type name(args) {`` 形式，跳过已有原型、
    控制语句、宏和类内方法。
    """
    cleaned = strip_comments_and_strings(source)
    lines = cleaned.splitlines()
    prototypes = []
    depth = 0
    skip_keywords = {
        "if", "for", "while", "switch", "return", "else", "do", "catch",
        "class", "struct", "namespace", "template", "enum", "typedef", "case",
    }
    # 逐行拼出「顶层语句」，遇到 { 时判断是不是函数定义
    buf = ""
    for raw_line in lines:
        line = raw_line.strip()
        if depth == 0 and line.startswith("#"):
            buf = ""          # 预处理指令不参与签名拼接
            continue
        if depth == 0:
            buf = (buf + " " + line).strip() if buf else line
            if "{" in buf:
                head = buf.split("{", 1)[0].strip()
                if _looks_like_signature(head):
                    name = _signature_name(head)
                    if name and name not in skip_keywords:
                        prototypes.append(head.rstrip() + ";")
                buf = ""
                depth += line.count("{") - line.count("}")
                if depth < 0:
                    depth = 0
                continue
            if ";" in buf:
                buf = ""
            depth += line.count("{") - line.count("}")
            if depth < 0:
                depth = 0
        else:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                depth = 0
                buf = ""
    # 去重保序
    seen, uniq = set(), []
    for proto in prototypes:
        key = re.sub(r"\s+", " ", proto)
        if key not in seen:
            seen.add(key)
            uniq.append(key)
    return "\n".join(uniq)


def _looks_like_signature(head: str) -> bool:
    if "(" not in head or ")" not in head:
        return False
    if head.startswith("#") or "=" in head:
        return False
    if ":" in head and "::" not in head:
        return False  # 构造函数初始化列表 / 标签
    return bool(re.match(r"^[A-Za-z_][\w\s\*&:<>,]*\s+[A-Za-z_]\w*\s*\(", head))


def _signature_name(head: str):
    match = re.search(r"([A-Za-z_]\w*)\s*\(", head)
    return match.group(1) if match else None


def ino_to_cpp(ino_files: list, sketch_name: str) -> str:
    """把目录里所有 .ino 拼成一个 .cpp（主文件在最前），并插入函数原型。"""
    chunks = []
    for path in ino_files:
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    joined = "\n".join(chunks)
    protos = arduino_prototypes(joined)
    header = f"#include <Arduino.h>\n\n/* 由 espbuild 自动生成的函数原型 */\n{protos}\n\n#line 1\n"
    return header + joined


# --------------------------------------------------------------------------- #
# 源文件收集
# --------------------------------------------------------------------------- #

def iter_sources(root: Path, skip_dirs=(".git", "build", "node_modules")):
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix in C_SRC_EXT | CXX_SRC_EXT | ASM_SRC_EXT:
            yield path


def find_library_dirs(include_names, libraries_root: Path) -> list:
    """按 ``#include <Foo.h>`` 找库目录（arduino-esp32 3.x 用 libraries/<Lib>/src）。"""
    used = []
    seen = set()
    for name in sorted(include_names):
        for lib in libraries_root.iterdir() if libraries_root.is_dir() else []:
            if not lib.is_dir():
                continue
            src = lib / "src"
            if not src.is_dir():
                continue
            if (src / name).is_file() or (src / (name + ".h")).is_file():
                if lib not in seen:
                    seen.add(lib)
                    used.append(lib)
                break
            # 子目录里的头文件（如 WiFi.h 在 src/）
            hits = list(src.rglob(name)) + list(src.rglob(name + ".h"))
            if hits:
                if lib not in seen:
                    seen.add(lib)
                    used.append(lib)
                break
    return used


def scan_includes(paths) -> set:
    names = set()
    pattern = re.compile(r'#\s*include\s*[<"]([^>"]+)[>"]')
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in pattern.finditer(text):
            name = match.group(1)
            if name.startswith("."):
                continue
            names.add(Path(name).name)
    return names


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 分区表：把 CSV 编译成 IDF 5.x 认的二进制（含 MD5 校验段）
# --------------------------------------------------------------------------- #

PART_MAGIC = 0x50AA          # 小端写入后为 AA 50
PART_MD5_MAGIC = 0xEBEB

_PART_TYPES = {"app": 0x00, "data": 0x01}
_PART_SUBTYPES = {
    "app": {
        "factory": 0x00, "test": 0x20,
        **{f"ota_{i}": 0x10 + i for i in range(16)},
    },
    "data": {
        "ota": 0x00, "phy": 0x01, "nvs": 0x02, "coredump": 0x03,
        "nvs_keys": 0x04, "efuse": 0x05, "undefined": 0x06,
        "esphttpd": 0x80, "fat": 0x81, "spiffs": 0x82, "littlefs": 0x83,
    },
}


def _to_int(text: str) -> int:
    text = text.strip()
    return int(text, 0) if text else 0


def parse_partition_csv(text: str) -> list:
    """解析分区表 CSV，返回 [{name,type,subtype,offset,size,flags}]。offset 可为 None。"""
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [cell.strip() for cell in line.split(",")]
        if len(parts) < 5:
            continue
        name, ptype, subtype, offset, size = parts[:5]
        flags = parts[5] if len(parts) > 5 and parts[5] else ""
        rows.append(
            {
                "name": name,
                "type": ptype.lower(),
                "subtype": subtype.lower(),
                "offset": _to_int(offset) if offset else None,
                "size": _to_int(size),
                "flags": _to_int(flags) if flags else 0,
            }
        )
    return rows


def partition_rows_to_bin(rows: list, alignment: int = 0x1000) -> bytes:
    """把分区条目编码成 32 字节一条 + MD5（IDF 5.x 需要）。"""
    import hashlib

    body = bytearray()
    cursor = 0x8000  # 分区表后面就是第一个分区
    for row in rows:
        offset = row["offset"]
        if offset is None:
            offset = (cursor + alignment - 1) // alignment * alignment
        ptype = _PART_TYPES.get(row["type"], int(row["type"], 0) if row["type"].isdigit() else 0xFF)
        subtype_table = _PART_SUBTYPES.get(row["type"], {})
        subtype = subtype_table.get(row["subtype"], 0xFF)
        label = row["name"].encode("utf-8")[:16].ljust(16, b"\x00")
        body += PART_MAGIC.to_bytes(2, "little")
        body += bytes([ptype & 0xFF, subtype & 0xFF])
        body += offset.to_bytes(4, "little")
        body += row["size"].to_bytes(4, "little")
        body += label
        body += row["flags"].to_bytes(4, "little")
        cursor = offset + row["size"]

    # MD5 记录的实际字节布局（对照原厂分区表得到）：
    #   EB EB + 14 个 0xFF + 16 字节 MD5
    # 注意摘要位于条目的第 16..31 字节，不是 label 所在的 12..27。
    digest = hashlib.md5(bytes(body)).digest()
    body += PART_MD5_MAGIC.to_bytes(2, "little")
    body += b"\xFF" * 14
    body += digest
    return bytes(body)


def compile_partition_table(csv_path: Path) -> bytes:
    return partition_rows_to_bin(parse_partition_csv(Path(csv_path).read_text(encoding="utf-8")))


class Builder:
    def __init__(self, sketch_dir: Path, board: dict, out_dir: Path | None = None,
                 jobs: int | None = None, verbose: bool = False, defines=None):
        self.sketch_dir = Path(sketch_dir).expanduser().resolve()
        self.board = board
        self.board_dir = find_core_dir()
        self.libs_dir = find_libs_dir(board.get("build.mcu", "esp32s3"))
        self.tc = toolchain_bin()
        self.verbose = verbose
        self.extra_defines = list(defines or [])
        self.jobs = jobs or max(2, (os.cpu_count() or 4))
        self.out_dir = Path(out_dir) if out_dir else self.sketch_dir / "build"
        self.obj_dir = self.out_dir / "obj"
        self.errors = []

        self.name = self._sketch_name()
        self.cores = self.board_dir / "cores" / board.get("build.core", "esp32")
        self.variant_dir = self.board_dir / "variants" / board.get("build.variant", "esp32s3")
        self.memory_type = board.get("build.memory_type") or (
            f"{board.get('build.boot', 'qio')}_{board.get('build.psram_type', 'qspi')}"
        )
        self.mem_dir = self.libs_dir / self.memory_type

        self.session = time.time()

    # ---------------------------------------------------------------- 准备
    def _sketch_name(self) -> str:
        inos = sorted(self.sketch_dir.glob("*.ino"))
        if not inos:
            raise BuildError(f"{self.sketch_dir} 里没有 .ino")
        return self.sketch_dir.name

    def flags(self) -> dict:
        libs = self.libs_dir
        flags_dir = libs / "flags"
        read = lambda name: (flags_dir / name).read_text(encoding="utf-8").strip()

        b = self.board
        board_defines = [
            "-DARDUINO=10607",
            f"-DARDUINO_{b.get('build.board', 'ESP32S3_DEV')}",
            "-DARDUINO_ARCH_ESP32",
            f'-DARDUINO_BOARD="{b.get("build.board", "ESP32S3_DEV")}"',
            f'-DARDUINO_VARIANT="{b.get("build.variant", "esp32s3")}"',
            f"-DESP32={b.get('build.mcu', 'esp32s3')[-1].upper() if False else 100}",
            "-DESP32S3",
            f"-DF_CPU={b.get('build.f_cpu', '240000000L')}",
            f"-DCORE_DEBUG_LEVEL={b.get('build.code_debug', 0)}",
            "-DARDUINO_USB_MODE={}".format(b.get("build.usb_mode", 1)),
            "-DARDUINO_USB_CDC_ON_BOOT={}".format(b.get("build.cdc_on_boot", 0)),
            "-DARDUINO_USB_MSC_ON_BOOT={}".format(b.get("build.msc_on_boot", 0)),
            "-DARDUINO_USB_DFU_ON_BOOT={}".format(b.get("build.dfu_on_boot", 0)),
            "-D{}".format(_clean_define(b.get("build.loop_core", "-DARDUINO_RUNNING_CORE=1"))),
            "-D{}".format(_clean_define(b.get("build.event_core", "-DARDUINO_EVENT_RUNNING_CORE=1"))),
        ]
        extra = b.get("build.defines", "")
        if extra:
            board_defines += extra.split()
        for item in self.extra_defines:
            board_defines.append(item if item.startswith("-D") else f"-D{item}")

        def tokens(text: str) -> list:
            # SDK 的 flags 是按 shell 语法写的，\" 要还原成真正的引号
            return [tok.replace('\\"', '"') for tok in text.split()]

        tc_cxx = SDK_DIR / "tools" / "esp-x32" / "2507" / "xtensa-esp-elf" / "include" / "c++" / "14.2.0"
        cxx_includes = []
        if tc_cxx.is_dir():
            for sub in ("", "xtensa-esp-elf", "xtensa-esp-elf/esp32s3", "backward"):
                path = tc_cxx / sub if sub else tc_cxx
                if path.is_dir():
                    cxx_includes += ["-isystem", str(path)]

        return {
            "common": tokens(
                " ".join(
                    [
                        read("S_flags"),
                        read("defines"),
                        "-iprefix",
                        f"{libs / 'include'}/",
                        read("includes"),
                    ]
                )
            ) + cxx_includes,
            "c": tokens(read("c_flags")),
            "cpp": tokens(read("cpp_flags")),
            "ld": tokens(read("ld_flags")),
            "ld_scripts": tokens(read("ld_scripts")),
            "ld_libs": tokens(read("ld_libs")),
            "board": board_defines,
        }

    def include_dirs(self, library_dirs) -> list:
        dirs = [self.cores, self.variant_dir, self.mem_dir / "include"]
        for lib in library_dirs:
            dirs.append(lib / "src")
        return ["-I" + str(d) for d in dirs]

    # ---------------------------------------------------------------- 编译
    def _compile_one(self, src: Path, extra_includes, flags, common) -> Path | None:
        rel = src.relative_to(src.anchor)
        obj = self.obj_dir / ("_".join(rel.parts)[:180] + ".o")
        obj.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in ASM_SRC_EXT:
            cmd = [str(self.tc / "xtensa-esp32s3-elf-gcc"), "-c", "-x", "assembler-with-cpp"]
            cmd += flags["common"] + flags["board"] + extra_includes
        elif src.suffix in C_SRC_EXT:
            cmd = [str(self.tc / "xtensa-esp32s3-elf-gcc"), "-c", "-std=gnu17"]
            cmd += flags["common"] + flags["c"] + flags["board"] + extra_includes
        else:
            cmd = [str(self.tc / "xtensa-esp32s3-elf-g++"), "-c"]
            cmd += flags["common"] + flags["cpp"] + flags["board"] + extra_includes
        cmd += ["-MMD", "-o", str(obj), str(src)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            self.errors.append({"file": str(src), "cmd": cmd, "stderr": proc.stderr})
            return None
        if self.verbose and proc.stderr.strip():
            eprint(proc.stderr.strip())
        return obj

    def compile_all(self):
        flags = self.flags()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.obj_dir.mkdir(parents=True, exist_ok=True)

        # sketch
        inos = sorted(self.sketch_dir.glob("*.ino"))
        main_ino = self.sketch_dir / f"{self.name}.ino"
        if main_ino.exists():
            inos = [main_ino] + [p for p in inos if p != main_ino]
        cpp_text = ino_to_cpp(inos, self.name)
        sketch_cpp = self.out_dir / f"{self.name}.ino.cpp"
        sketch_cpp.write_text(cpp_text, encoding="utf-8")

        sketch_sources = [sketch_cpp] + [
            p for p in iter_sources(self.sketch_dir) if p.parent == self.sketch_dir
        ]
        core_sources = list(iter_sources(self.cores))
        variant_sources = list(iter_sources(self.variant_dir))

        # 用到的库
        includes = scan_includes(sketch_sources + core_sources)
        library_dirs = find_library_dirs(includes, self.board_dir / "libraries")
        lib_sources = []
        for lib in library_dirs:
            lib_sources.extend(iter_sources(lib / "src"))

        all_sources = sketch_sources + core_sources + variant_sources + lib_sources
        includes_flags = self.include_dirs(library_dirs)

        eprint(
            f"[espbuild] {self.name}: 编译 {len(all_sources)} 个源文件"
            f"（core {len(core_sources)} / 库 {len(lib_sources)} / sketch {len(sketch_sources)}）"
            f" 并行 {self.jobs}"
        )

        t0 = time.time()
        objects = []
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = [
                pool.submit(self._compile_one, src, includes_flags, flags, flags["common"])
                for src in all_sources
            ]
            for idx, fut in enumerate(futures, 1):
                result = fut.result()
                if result:
                    objects.append(result)
                if idx % 25 == 0 or idx == len(all_sources):
                    eprint(f"[espbuild]   {idx}/{len(all_sources)}")
        if self.errors:
            first = self.errors[0]
            raise BuildError(
                f"{len(self.errors)} 个源文件编译失败，第一个是 {first['file']}:\n"
                + first["stderr"][-4000:]
            )

        # core.a（把 core + variant 打包，和 Arduino 行为一致，避免重复链接）
        core_objs = [o for o in objects if "/obj/" in str(o) and self._is_core_obj(o)]
        if core_objs:
            ar = str(self.tc / "xtensa-esp32s3-elf-ar")
            core_a = self.out_dir / "core.a"
            subprocess.run([ar, "rcs", str(core_a)] + [str(o) for o in core_objs], check=True)
        else:
            core_a = None

        eprint(f"[espbuild] 编译完成，用时 {time.time() - t0:.1f}s")
        return objects, core_a, flags, includes_flags

    def _is_core_obj(self, obj: Path) -> bool:
        name = obj.name
        core_prefix = "_".join(self.cores.parts) + "_"
        variant_prefix = "_".join(self.variant_dir.parts) + "_"
        return name.startswith(core_prefix) or name.startswith(variant_prefix) or \
            f"cores_esp32_" in name or f"variants_{self.variant_dir.name}_" in name

    # ---------------------------------------------------------------- 链接
    def link(self, objects, core_a, flags, includes_flags):
        elf = self.out_dir / f"{self.name}.elf"
        linker = str(self.tc / "xtensa-esp32s3-elf-gcc")
        map_file = self.out_dir / f"{self.name}.map"

        link_objs = [str(o) for o in objects if core_a is None or not self._is_core_obj(o)]

        cmd = [linker, "-o", str(elf), "-Wl,--Map=" + str(map_file)]
        cmd += flags["ld"]
        cmd += ["-L" + str(self.libs_dir / "lib"), "-L" + str(self.mem_dir)]
        # ld 脚本必须能在 -L 里找到
        cmd += ["-L" + str(self.libs_dir / "ld"), "-L" + str(self.mem_dir)]
        cmd += flags["ld_scripts"]
        cmd += ["-Wl,--start-group"]
        cmd += link_objs
        if core_a is not None:
            cmd += [str(core_a)]
        cmd += flags["ld_libs"]
        cmd += ["-Wl,--end-group"]
        cmd += ["-L" + str(self.tc / "xtensa-esp32s3-elf" / "lib")]
        cmd += ["-lm"]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise BuildError("链接失败:\n" + proc.stderr[-6000:])
        eprint(f"[espbuild] 链接完成 → {elf}")
        return elf

    # ---------------------------------------------------------------- 产物
    def make_binaries(self, elf: Path):
        esptool = esptool_cmd()
        flash_mode = self.board.get("build.flash_mode", "dio")
        flash_freq = self.board.get("build.flash_freq", "80m")
        flash_size = self.board.get("build.flash_size", "4MB")

        app_bin = self.out_dir / f"{self.name}.bin"
        cmd = esptool + [
            "--chip", self.board.get("build.mcu", "esp32s3"),
            "elf2image",
            "--flash-mode", flash_mode,
            "--flash-freq", flash_freq,
            "--flash-size", flash_size,
            "-o", str(app_bin), str(elf),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise BuildError("elf2image 失败:\n" + proc.stderr[-3000:])

        # 启动引导：SDK 里给了预编译 bootloader elf，转成 bin
        boot_elf = self.libs_dir / "bin" / f"bootloader_{flash_mode}_{flash_freq.replace('m', 'm')}.elf"
        if not boot_elf.exists():
            cands = sorted((self.libs_dir / "bin").glob("bootloader_*.elf"))
            boot_elf = cands[0] if cands else None
        boot_bin = self.out_dir / "bootloader.bin"
        if boot_elf and boot_elf.exists():
            subprocess.run(
                esptool + [
                    "--chip", self.board.get("build.mcu", "esp32s3"), "elf2image",
                    "--flash-mode", flash_mode, "--flash-freq", flash_freq,
                    "--flash-size", flash_size, "-o", str(boot_bin), str(boot_elf),
                ],
                capture_output=True, text=True,
            )

        # 分区表
        parts_name = self.board.get("build.partitions", "default")
        parts_dir = self.board_dir / "tools" / "partitions"
        parts_csv = parts_dir / f"{parts_name}.csv"
        parts_bin = self.out_dir / "partitions.bin"
        # SDK 里已经带好编译好的 .bin；没有才用 CSV 现编（绝不能直接把 CSV 当 bin 烧！）
        prebuilt = parts_dir / f"{parts_name}.bin"
        if parts_csv.exists():
            # 自己按 CSV 生成：SDK 里的 .bin 是旧格式（没有 MD5），IDF 5.5 会拒绝
            parts_bin.write_bytes(compile_partition_table(parts_csv))
        elif prebuilt.exists():
            shutil.copy(prebuilt, parts_bin)
        else:
            gen = self.board_dir / "tools" / "gen_esp32part.py"
            if gen.exists():
                proc = subprocess.run(
                    [sys.executable, str(gen), str(parts_csv), str(parts_bin)],
                    capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    raise BuildError(f"生成分区表失败：{proc.stderr[-1000:]}")
            else:
                raise BuildError(
                    f"找不到 {prebuilt.name} 或 gen_esp32part.py，无法生成分区表"
                )
        boot_app0 = self.board_dir / "tools" / "partitions" / "boot_app0.bin"

        layout = [
            ("0x0", boot_bin if boot_bin.exists() else None, "bootloader"),
            ("0x8000", parts_bin if parts_bin.exists() else None, "分区表"),
            ("0xe000", boot_app0 if boot_app0.exists() else None, "boot_app0"),
            ("0x10000", app_bin, "应用"),
        ]
        return app_bin, layout

    # ---------------------------------------------------------------- 入口
    def run(self):
        objects, core_a, flags, includes_flags = self.compile_all()
        elf = self.link(objects, core_a, flags, includes_flags)
        app_bin, layout = self.make_binaries(elf)

        size = app_bin.stat().st_size
        max_size = int(self.board.get("upload.maximum_size", 1310720))
        meta = {
            "sketch": self.name,
            "flash_mode": self.board.get("build.flash_mode", "dio"),
            "flash_freq": self.board.get("build.flash_freq", "80m"),
            "flash_size": self.board.get("build.flash_size", "4MB"),
            "board": self.board.get("name"),
            "elf": str(elf),
            "bin": str(app_bin),
            "size": size,
            "max_size": max_size,
            "layout": [(addr, str(p) if p else None, label) for addr, p, label in layout],
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (self.out_dir / "build.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        eprint(f"[espbuild] 应用大小 {size} 字节 / 上限 {max_size}（{size * 100 // max(max_size, 1)}%）")
        return meta


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #

def load_board(board_key="esp32s3", choices=None, boards_file=None) -> dict:
    if boards_file is None:
        arch = board_arch(board_key)
        boards_file = boards_file_for(arch)
        if not boards_file.exists():
            boards_file = ESP_ROOT / "boards_esp32s3.txt"
    boards = parse_boards(Path(boards_file))
    if board_key not in boards:
        raise BuildError(f"{boards_file} 里没有板子 {board_key}（可选：{', '.join(list(boards)[:8])}…）")
    info = apply_menus(boards[board_key], choices)
    info["_key"] = board_key
    return info


def build_sketch(sketch_dir, board_key="esp32s3", choices=None, jobs=None, verbose=False,
                 defines=None):
    board = load_board(board_key, choices)
    return Builder(Path(sketch_dir), board, jobs=jobs, verbose=verbose, defines=defines).run()


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="espbuild.py", description="原生编译 Arduino-ESP32 工程")
    parser.add_argument("sketch", nargs="?", default=".", help="工程目录（含 .ino），默认当前目录")
    parser.add_argument("--board", default="esp32s3", help="boards.txt 里的板子 key，默认 esp32s3")
    parser.add_argument("--jobs", type=int, default=None, help="并行编译数")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--define", action="append", default=[], metavar="NAME=VALUE",
                        help="额外编译宏，如 --define RGB_PIN=38")
    parser.add_argument("--menu", action="append", default=[], metavar="MENU=OPTION",
                        help="覆盖菜单项，如 --menu FlashSize=16M --menu PSRAM=enabled")
    args = parser.parse_args(argv)

    choices = {}
    for item in args.menu:
        if "=" in item:
            key, _, value = item.partition("=")
            choices[key] = value
    try:
        meta = build_sketch(args.sketch, args.board, choices, args.jobs, args.verbose,
                            args.define)
    except BuildError as exc:
        eprint(f"[espbuild] {exc}")
        return 1
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
