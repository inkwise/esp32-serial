#!/usr/bin/env python3
"""esp — Termux 上的 ESP32 工具套件入口。

一个命令覆盖：环境自举、编译、进下载模式、烧录、串口监视、一键全流程。

    esp setup          从 ArduinoDroid 的 APK 里自举原生 xtensa 工具链（幂等）
    esp info           设备 / 工具链 / 工程状态
    esp new NAME       新建一个 sketch 模板
    esp build [dir]    原生编译 → build/*.bin
    esp flash [dir]    进下载模式 → 桥接 → esptool 烧写 → 复位
    esp mon            串口监视（时间戳 / hex / 落盘）
    esp boot           只让板子进下载模式
    esp reset          硬复位
    esp all [dir]      build + flash + mon
    esp clean [dir]    删掉 build 目录
    esp boards         列出可用板子

设计要点
--------
* 编译走 bionic 版 xtensa 工具链（从手机上已有的 ArduinoDroid APK 里抽出来的），
  不需要 proot、不需要联网下载工具链。
* 烧录走「自己进下载模式 + USB→TCP 桥接 + esptool」三段式：
  USB-Serial-JTAG 的复位要控 DTR/RTS，而 esptool 通过 socket:// 拿不到这些线，
  所以由本工具负责按下复位，esptool 负责它最擅长的协议与擦写。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/data/data/com.termux/files/home"))
HERE = Path(__file__).resolve().parent
ESP_ROOT = Path(os.environ.get("ESP_ROOT", HOME / ".esp"))
AD_DIR = ESP_ROOT / "ad"
SDK_DIR = AD_DIR / "sdk"
BIN_DIR = AD_DIR / "bin"
TC_DIR = ESP_ROOT / "tc"
VENV = ESP_ROOT / "venv"
CONFIG_PATH = ESP_ROOT / "config.json"

ESP_SERIAL = HERE / "esp_serial.py"
ESPBUILD = HERE / "espbuild.py"

DEFAULT_APK = "/storage/emulated/0/MT2/apks/ArduinoDroid_9.7.6.apks"
DEFAULTS = {
    "apk": DEFAULT_APK,
    "board": "esp32s3",
    # 手机开发板默认把 Serial 指到 USB-Serial-JTAG（否则串口里什么都看不到）
    "menus": {"CDCOnBoot": "cdc"},
    "srcdir": str(HOME / "esp-projects"),
    "bridge_port": 18080,
}
VERSION = "1.0.0"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, flush=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    ESP_ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def py_module_ok(python: Path, module: str) -> bool:
    if not python.exists():
        return False
    proc = subprocess.run([str(python), "-c", f"import {module}"], capture_output=True)
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# setup：从 APK 自举工具链
# --------------------------------------------------------------------------- #

def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def extract_from_apk(apk: Path) -> None:
    """抽出 bionic 可执行文件、SDK 归档、boards.txt。"""
    import zipfile

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    eprint(f"[setup] 读取 {apk}")
    outer = zipfile.ZipFile(apk)

    libs_apk = next((n for n in outer.namelist() if "arm64_v8a" in n), None)
    if libs_apk is None:
        raise SystemExit("APK 里没有 arm64-v8a 的 split，无法提取工具链")

    import io

    inner = zipfile.ZipFile(io.BytesIO(outer.read(libs_apk)))
    count = 0
    for info in inner.infolist():
        if not info.filename.startswith("lib/arm64-v8a/"):
            continue
        base = os.path.basename(info.filename)
        if not (base.startswith("lib") and base.endswith(".so")):
            continue
        name = base[3:-3]
        path = BIN_DIR / name
        path.write_bytes(inner.read(info.filename))
        path.chmod(0o755)
        count += 1
    eprint(f"[setup] 抽出 {count} 个可执行文件 → {BIN_DIR}")

    # SDK 归档（res/raw 里的 7z）
    sdk_apk = next((n for n in outer.namelist() if "esp32s3_sdk" in n), None)
    sdk_7z = AD_DIR / "sdk.7z"
    if sdk_apk and not SDK_DIR.exists():
        sdk_inner = zipfile.ZipFile(io.BytesIO(outer.read(sdk_apk)))
        raw_name = next((i.filename for i in sdk_inner.infolist()
                         if i.filename.startswith("res/raw/") and i.file_size > 1_000_000), None)
        if raw_name:
            sdk_7z.write_bytes(sdk_inner.read(raw_name))
            eprint(f"[setup] 解开 SDK（{sdk_7z.stat().st_size / 1048576:.0f}MB）…")
            seven = shutil.which("7z") or shutil.which("7za")
            if not seven:
                raise SystemExit("需要 7z 解压 SDK：pkg install p7zip")
            SDK_DIR.parent.mkdir(parents=True, exist_ok=True)
            proc = _run([seven, "x", "-y", f"-o{SDK_DIR}", str(sdk_7z)])
            if proc.returncode != 0:
                raise SystemExit(f"7z 解压失败：{proc.stderr[-500:]}")
            sdk_7z.unlink(missing_ok=True)

    # boards.txt（base.apk 的 res/raw）
    base_name = next((n for n in outer.namelist() if n.endswith("base.apk")), None)
    if base_name:
        base = zipfile.ZipFile(io.BytesIO(outer.read(base_name)))
        for entry in base.infolist():
            if entry.filename.startswith("res/raw/") and entry.filename.endswith("_boards.txt"):
                target = ESP_ROOT / ("boards_" + os.path.basename(entry.filename)[len("res/raw/"):])
                target.write_bytes(base.read(entry.filename))
        eprint("[setup] 已导出 boards.txt")


def build_toolchain_layout() -> None:
    """把散装的可执行文件 + SDK 目录拼成 crosstool-NG 期望的布局。"""
    sdk_tc = SDK_DIR / "tools" / "esp-x32" / "2507"
    if not sdk_tc.is_dir():
        cands = list((SDK_DIR / "tools").glob("esp-*/")) if (SDK_DIR / "tools").is_dir() else []
        if not cands:
            raise SystemExit("SDK 里找不到 esp-x32 工具链目录")
        sdk_tc = sorted(cands)[-1]

    bin_dir = TC_DIR / "bin"
    libexec = TC_DIR / "libexec" / "gcc" / "xtensa-esp32s3-elf" / "14.2.0"
    libdir = TC_DIR / "lib" / "gcc" / "xtensa-esp32s3-elf" / "14.2.0"
    triple = TC_DIR / "xtensa-esp32s3-elf"
    for path in (bin_dir, libexec, libdir, triple / "bin"):
        path.mkdir(parents=True, exist_ok=True)

    # 1) 编译器驱动（复制，不软链：gcc 用 /proc/self/exe 推断 prefix）
    driver_map = {
        "esp32s3_xtensa-esp32s3-elf-gcc": "xtensa-esp32s3-elf-gcc",
        "esp32s3_xtensa-esp32s3-elf-g++": "xtensa-esp32s3-elf-g++",
        "esp32s3_xtensa-esp32s3-elf-ar": "xtensa-esp32s3-elf-ar",
        "esp32s3_xtensa-esp32s3-elf-size": "xtensa-esp32s3-elf-size",
    }
    for src_name, dst_name in driver_map.items():
        src = BIN_DIR / src_name
        dst = bin_dir / dst_name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            dst.chmod(0o755)

    # 2) as / ld / collect2
    for src_name, dst in (("esp32s3_as", triple / "bin" / "as"), ("esp32s3_ld", triple / "bin" / "ld")):
        src = BIN_DIR / src_name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            dst.chmod(0o755)
    collect2 = BIN_DIR / "esp32s3_collect2"
    if collect2.exists() and not (libexec / "collect2").exists():
        shutil.copy2(collect2, libexec / "collect2")
        (libexec / "collect2").chmod(0o755)

    # 3) cc1 / cc1plus（SDK 里带的是 ELF aarch64，只是叫 .exe）
    src_libexec = sdk_tc / "libexec" / "gcc" / "xtensa-esp-elf" / "14.2.0"
    for suffix, dst_name in (("cc1.exe", "cc1"), ("cc1plus.exe", "cc1plus")):
        src = src_libexec / suffix
        dst = libexec / dst_name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            dst.chmod(0o755)

    # 4) 运行时数据：libgcc / crt / include（必须是真目录，gcc 会从它往上找 as/ld）
    src_gcc = sdk_tc / "lib" / "gcc" / "xtensa-esp-elf" / "14.2.0"
    if src_gcc.is_dir():
        for item in src_gcc.iterdir():
            link = libdir / item.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(item)

    # 5) sysroot：include / lib / usr
    for sub in ("include", "lib", "usr", "share"):
        src = sdk_tc / "xtensa-esp-elf" / sub
        link = triple / sub
        if src.exists() and not link.exists() and not link.is_symlink():
            link.symlink_to(src)
    ld_bfd = sdk_tc / "xtensa-esp-elf" / "bin" / "ld.bfd"
    link = triple / "bin" / "ld.bfd"
    if ld_bfd.exists() and not link.exists() and not link.is_symlink():
        link.symlink_to(ld_bfd)

    gcc = bin_dir / "xtensa-esp32s3-elf-gcc"
    proc = _run([str(gcc), "--version"])
    if proc.returncode != 0:
        raise SystemExit(f"工具链自检失败：{proc.stderr[-500:]}")
    eprint(f"[setup] 工具链就绪：{proc.stdout.splitlines()[0]}")


def ensure_venv() -> Path:
    """准备 esptool 的 venv（pyserial 需要给 Android 打个小补丁）。"""
    python = VENV / "bin" / "python"
    if not python.exists():
        eprint("[setup] 创建 venv 并安装 esptool / pyserial …")
        ESP_ROOT.mkdir(parents=True, exist_ok=True)
        proc = _run([sys.executable, "-m", "venv", str(VENV)])
        if proc.returncode != 0:
            raise SystemExit("创建 venv 失败：" + proc.stderr[-500:])
        for pkg in ("esptool", "pyserial"):
            proc = _run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", pkg])
            if proc.returncode != 0:
                raise SystemExit(f"装 {pkg} 失败：{proc.stderr[-800:]}")
        patch_pyserial_android()
    return python


def patch_pyserial_android() -> None:
    """pyserial 不认识 android，会让 esptool 直接 import 失败；按 linux 处理即可。"""
    target = VENV / "lib"
    if not target.is_dir():
        return
    for site in target.glob("python*/site-packages/serial/tools/list_ports_posix.py"):
        text = site.read_text(encoding="utf-8")
        if "android" in text:
            continue
        old = "if plat[:5] == 'linux':"
        if old in text:
            site.write_text(
                text.replace(old, "if plat[:5] == 'linux' or plat == 'android':", 1),
                encoding="utf-8",
            )
            eprint("[setup] 已给 pyserial 打 Android 补丁")


def cmd_setup(args) -> int:
    cfg = load_config()
    apk = Path(args.apk or cfg["apk"]).expanduser()
    ESP_ROOT.mkdir(parents=True, exist_ok=True)

    need_extract = not (BIN_DIR / "esp32s3_xtensa-esp32s3-elf-gcc").exists() or args.force
    if need_extract:
        if not apk.exists():
            raise SystemExit(
                f"找不到 APK：{apk}\n"
                "把 ArduinoDroid 的 .apks 放到手机上，然后：esp setup --apk /path/to/xxx.apks"
            )
        extract_from_apk(apk)
    else:
        eprint(f"[setup] 已解包过（{BIN_DIR}），跳过；要重来加 --force")

    if not SDK_DIR.exists():
        raise SystemExit("SDK 目录缺失，重跑 esp setup --force")

    build_toolchain_layout()
    python = ensure_venv()
    patch_pyserial_android()

    if not (ESP_ROOT / "boards_esp32s3.txt").exists():
        eprint("[setup] 警告：没导出 boards.txt，espbuild 会用不到板子配置")

    cfg["apk"] = str(apk)
    save_config(cfg)
    eprint("[setup] 完成。下一步：esp new hello && cd hello && esp all")
    return 0


# --------------------------------------------------------------------------- #
# 设备与工具链信息
# --------------------------------------------------------------------------- #

def usb_devices() -> list:
    exe = shutil.which("termux-usb")
    if not exe:
        return []
    proc = _run([exe, "-l"])
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []


def toolchain_version() -> str:
    gcc = TC_DIR / "bin" / "xtensa-esp32s3-elf-gcc"
    if not gcc.exists():
        return "未安装（跑 esp setup）"
    proc = _run([str(gcc), "--version"])
    return proc.stdout.splitlines()[0] if proc.returncode == 0 else "异常"


def cmd_info(args) -> int:
    cfg = load_config()
    print("== 环境 ==")
    print(f"  Termux      {os.environ.get('PREFIX', '?')}")
    print(f"  Python      {sys.version.split()[0]}")
    print(f"  CPU          {os.cpu_count()} 核")
    print()
    print("== 工具链 ==")
    print(f"  gcc         {toolchain_version()}")
    venv_py = VENV / "bin" / "python"
    if venv_py.exists():
        proc = _run([str(venv_py), "-m", "esptool", "version"])
        ver = proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 else "异常"
        print(f"  esptool     {ver}")
    else:
        print("  esptool     未安装（跑 esp setup）")
    print(f"  板子默认     {cfg['board']}  菜单 {cfg.get('menus') or '默认'}")
    print()
    print("== 芯片与 SDK ==")
    try:
        import espbuild

        targets = espbuild.available_targets() or ["（无）"]
    except Exception:  # noqa: BLE001
        targets = ["（未知）"]
    print(f"  已装 SDK     {', '.join(targets)}")
    boards_files = sorted(p.name for p in ESP_ROOT.glob("boards_*.txt"))
    print(f"  板子表       {', '.join(boards_files) or '（无）'}")
    if cfg.get("chip"):
        print(f"  上次识别     {cfg['chip']}（esp chip 可重新识别并 --save）")
    else:
        print("  上次识别     （还没跑过 esp chip）")
    print()
    print("== 设备 ==")
    devices = usb_devices()
    if not devices:
        print("  没有 USB 设备（插 OTG + 数据线）")
    else:
        for dev in devices:
            print(f"  {dev}")
        print("  详情：esp-serial usb identify")
    print()
    print("== 工程 ==")
    print(f"  默认目录     {cfg['srcdir']}")
    projects = sorted(Path(cfg["srcdir"]).glob("*/")) if Path(cfg["srcdir"]).is_dir() else []
    if projects:
        for proj in projects:
            bins = list((proj / "build").glob("*.bin")) if (proj / "build").is_dir() else []
            mark = f"已编译 {bins[0].stat().st_size} 字节" if bins else "未编译"
            print(f"  {proj.name:24} {mark}")
    else:
        print("  （还没有工程，esp new NAME 建一个）")
    return 0


# --------------------------------------------------------------------------- #
# 工程
# --------------------------------------------------------------------------- #

SKETCH_TEMPLATE = """\
// {name} — esp 工具链示例
#include <Arduino.h>

void setup() {{
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("=== {name} 启动 (esp native toolchain) ===");
}}

void loop() {{
  static unsigned long last = 0;
  if (millis() - last > 2000) {{
    last = millis();
    Serial.printf("[{name}] uptime %lu ms\\n", millis());
  }}
}}
"""


def cmd_new(args) -> int:
    cfg = load_config()
    root = Path(args.dir or cfg["srcdir"]).expanduser()
    name = args.name
    target = root / name
    if target.exists():
        eprint(f"目录已存在：{target}")
        return 1
    target.mkdir(parents=True)
    (target / f"{name}.ino").write_text(SKETCH_TEMPLATE.format(name=name), encoding="utf-8")
    eprint(f"[esp] 已创建 {target}")
    eprint(f"[esp] 下一步：cd {target} && esp all")
    print(target)
    return 0


def resolve_project(args) -> Path:
    cfg = load_config()
    if args.dir:
        path = Path(args.dir).expanduser().resolve()
        return path
    # 当前目录有 .ino 就用当前目录
    if list(Path.cwd().glob("*.ino")):
        return Path.cwd()
    projects = sorted(Path(cfg["srcdir"]).glob("*/")) if Path(cfg["srcdir"]).is_dir() else []
    if len(projects) == 1:
        return projects[0]
    if not projects:
        raise SystemExit("当前目录没有 .ino，也没有工程；先 esp new NAME")
    names = ", ".join(p.name for p in projects)
    raise SystemExit(f"有多个工程，指定一个：esp build <目录>（现有：{names}）")


def cmd_build(args) -> int:
    import espbuild

    cfg = load_config()
    project = resolve_project(args)
    choices = dict(cfg.get("menus") or {})
    for item in args.menu or []:
        key, _, value = item.partition("=")
        choices[key] = value
    board_key = args.board or cfg["board"]
    try:
        meta = espbuild.build_sketch(
            project, board_key, choices, jobs=args.jobs, verbose=args.verbose,
            defines=getattr(args, "define", None),
        )
    except espbuild.BuildError as exc:
        eprint(f"[esp] 编译失败：{exc}")
        return 1
    print(f"[esp] 固件：{meta['bin']}（{meta['size']} 字节，{meta['size'] * 100 // max(meta['max_size'], 1)}% 上限）")
    return 0


def cmd_clean(args) -> int:
    project = resolve_project(args)
    build = project / "build"
    if build.is_dir():
        shutil.rmtree(build)
        eprint(f"[esp] 已删除 {build}")
    else:
        eprint("[esp] 没有 build 目录")
    return 0


def cmd_boards(args) -> int:
    cfg = load_config()
    arch = getattr(args, "arch", None) or cfg.get("chip") or cfg.get("board", "esp32s3")
    try:
        import espbuild

        arch = espbuild.board_arch(arch)
        boards_file = espbuild.boards_file_for(arch)
    except Exception as exc:  # noqa: BLE001
        eprint(f"[esp] {exc}")
        return 1
    import espbuild

    board_keys = sorted(espbuild.parse_boards(boards_file))
    print(f"共 {len(board_keys)} 块板子（默认 {cfg['board']}）：")
    for key in board_keys[:60]:
        info = espbuild.parse_boards(boards_file)[key]
        print(f"  {key:28} {info.get('name', '')}")
    return 0


# --------------------------------------------------------------------------- #
# 烧录
# --------------------------------------------------------------------------- #

def wait_port_open(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def wait_device(timeout: float = 10.0) -> bool:
    """等 termux-usb 重新看到设备（复位后要重新枚举）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if usb_devices():
            return True
        time.sleep(0.3)
    return False


# esptool 报出来的芯片名 → 本工具的板子架构 / ArduinoDroid 的 SDK 包名
CHIP_TABLE = {
    "ESP32-S3": "esp32s3",
    "ESP32-S2": "esp32s2",
    "ESP32-C6": "esp32c6",
    "ESP32-C5": "esp32c5",
    "ESP32-C3": "esp32c3",
    "ESP32-C2": "esp32c2",
    "ESP32-P4": "esp32p4",
    "ESP8266": "esp8266",
    "ESP32": "esp32",          # 经典款（要放在 S2/S3/C3 之后匹配）
}


def chip_arch(chip_name: str) -> str | None:
    """把 'ESP32-S3 (QFN56) ...' 这类字符串归一成 esp32s3。"""
    if not chip_name:
        return None
    upper = chip_name.upper()
    for key in ("ESP32-S3", "ESP32-S2", "ESP32-C6", "ESP32-C5", "ESP32-C3",
                "ESP32-C2", "ESP32-P4", "ESP8266"):
        if upper.startswith(key):
            return CHIP_TABLE[key]
    if upper.startswith("ESP32"):
        return "esp32"
    return None


def detect_chip(no_boot: bool = False) -> dict | None:
    """让 esptool 自己去问 ROM：芯片型号、revision、flash 大小、MAC。

    需要先进入下载模式（会自动做），所以调用方要知道这会复位板子。
    """
    if not no_boot:
        rc = subprocess.call([sys.executable, str(ESP_SERIAL), "boot", "--usb"])
        if rc != 0:
            eprint("[esp] 进下载模式失败，先确认板子插好、能枚举")
            return None
        time.sleep(1.5)

    _rc, text = run_esp_esptool(
        ["--before", "no-reset", "--after", "hard-reset", "flash-id"], capture=True
    )
    info = {"raw": text}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Chip type:"):
            value = line.split(":", 1)[1].strip()
            info["name"] = value
            info["arch"] = chip_arch(value)
        elif line.startswith("Features:"):
            info["features"] = line.split(":", 1)[1].strip()
        elif line.startswith("Detected flash size:"):
            info["flash_size"] = line.split(":", 1)[1].strip()
        elif line.startswith("MAC:"):
            info["mac"] = line.split(":", 1)[1].strip()
    if "arch" not in info:
        eprint("[esp] 没能识别芯片，esptool 原始输出：")
        eprint(text[-1500:])
        return None
    return info


def cmd_chip(args) -> int:
    info = detect_chip(no_boot=args.no_boot)
    if info is None:
        return 1
    cfg = load_config()
    arch = info["arch"]
    try:
        import espbuild

        targets = espbuild.available_targets()
    except Exception:  # noqa: BLE001
        targets = []
    boards_ok = (ESP_ROOT / f"boards_{arch}.txt").exists()

    print("== 芯片（由 esptool 直接问 ROM 得到）==")
    print(f"  型号        {info.get('name', '?')}")
    if info.get("features"):
        print(f"  特性        {info['features']}")
    if info.get("flash_size"):
        print(f"  Flash       {info['flash_size']}")
    if info.get("mac"):
        print(f"  MAC         {info['mac']}")
    print()
    print(f"  架构标识    {arch}")
    print(f"  可编译 SDK  {'✓ ' + arch if arch in targets else '✗ 未安装（已装：' + ', '.join(targets or ['无']) + '）'}")
    print(f"  板子定义    {'✓ boards_' + arch + '.txt' if boards_ok else '✗ 缺 boards_' + arch + '.txt'}")
    if arch in targets and boards_ok:
        print(f"  默认板子    {arch}（可用 esp boards --arch {arch} 看全部）")
    print()

    if args.save:
        cfg["chip"] = arch
        cfg["board"] = cfg.get("board") if espbuild_mod_same_arch(cfg, arch) else arch
        save_config(cfg)
        eprint(f"[esp] 已保存：chip={cfg['chip']} board={cfg['board']}")

    if arch not in targets:
        eprint(
            f"[esp] 提示：编译 {arch} 需要 ArduinoDroid 的 split_{arch}_sdk 包；"
            "烧录现成 .bin 不受影响（esptool 会自动识别芯片）。"
        )
    return 0


def espbuild_mod_same_arch(cfg: dict, arch: str) -> bool:
    try:
        import espbuild

        return espbuild.board_arch(cfg.get("board", "")) == arch
    except Exception:  # noqa: BLE001
        return False


def usb_device_path() -> str | None:
    devices = [d for d in usb_devices() if isinstance(d, str) and d.startswith("/dev/bus/usb/")]
    return devices[0] if devices else None


def run_esp_esptool(esptool_args: list, device: str | None = None,
                    capture: bool = False):
    """把 esptool 包进 termux-usb 跑，端口由 esp_esptool.py 接管（USB 直连）。

    返回 (退出码, 输出文本)；capture=False 时输出直接打到终端、文本为空。
    """
    exe = shutil.which("termux-usb")
    if not exe:
        eprint("[esp] 找不到 termux-usb：pkg install termux-api")
        return 2, ""
    venv_python = VENV / "bin" / "python"
    if not venv_python.exists():
        eprint("[esp] 没有 esptool 环境，先跑 esp setup")
        return 2, ""
    device = device or usb_device_path()
    if not device and wait_device(25):
        device = usb_device_path()
    if not device:
        eprint("[esp] 没有 USB 设备（检查 OTG 线/数据线）")
        return 1, ""

    inner = [str(venv_python), str(HERE / "esp_esptool.py")] + esptool_args
    cmd = [exe, "-r", "-E", "-e", " ".join(shlex_quote(a) for a in inner), device]
    if capture:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    return subprocess.call(cmd), ""


def shlex_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def cmd_flash(args) -> int:
    cfg = load_config()
    project = resolve_project(args)
    meta_path = project / "build" / "build.json"
    if not meta_path.exists():
        eprint(f"[esp] {project} 还没编译过，先 esp build")
        return 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    layout = [(addr, path, label) for addr, path, label in meta["layout"] if path]
    missing = [item for item in layout if not Path(item[1]).exists()]
    if missing:
        eprint(f"[esp] 缺少产物：{missing}")
        return 1

    chip = args.chip or cfg.get("chip") or None   # None → 由 esptool 自动识别

    if not args.no_boot:
        if args.manual:
            eprint("[esp] 手动模式：请按住 BOOT → 点一下 RST/EN → 松开 BOOT")
            input("    完成后按回车继续 …")
        else:
            eprint("[esp] 1/2 让板子进入下载模式 …")
            rc = subprocess.call([sys.executable, str(ESP_SERIAL), "boot", "--usb"])
            if rc != 0:
                # USB-UART 桥在芯片复位后偶尔会短暂掉线，等一下再试一次
                eprint("[esp] 没找到设备，等它重新枚举后再试一次 …")
                if wait_device(25):
                    rc = subprocess.call([sys.executable, str(ESP_SERIAL), "boot", "--usb"])
            if rc != 0:
                eprint("[esp] 进下载模式失败。可以用 esp flash --manual 手动按 BOOT+RST")
                return rc
            time.sleep(1.5)

    esptool_args = []
    if chip:
        esptool_args += ["--chip", chip]
    esptool_args += [
        "--before", "no-reset",      # 复位已经由第 1 步做了
        "--after", "no-reset",       # 收尾复位由 esp_esptool.py 的 hard_reset 负责
        "write-flash",
        "--flash-mode", args.flash_mode or meta.get("flash_mode", "dio"),
        "--flash-freq", args.flash_freq or "80m",
        "--flash-size", args.flash_size or meta.get("flash_size", "detect"),
    ]
    if args.erase:
        esptool_args.append("--erase-all")
    for addr_hex, path, label in layout:
        esptool_args += [addr_hex, path]

    eprint("[esp] 2/2 烧写（USB 直连，无 TCP 中转）：")
    for addr_hex, path, label in layout:
        eprint(f"      {addr_hex:>8}  {label:<10} {Path(path).name} ({Path(path).stat().st_size} 字节)")
    rc, _ = run_esp_esptool(esptool_args)
    if rc != 0 and not args.no_boot:
        eprint("[esp] 第一次没连上，重新进下载模式后重试 …")
        if subprocess.call([sys.executable, str(ESP_SERIAL), "boot", "--usb"]) == 0:
            time.sleep(1.5)
            rc, _ = run_esp_esptool(esptool_args)
    if rc != 0:
        eprint(f"[esp] 烧写失败（esptool 退出码 {rc}）")
        return rc
    eprint("[esp] 烧写完成，芯片已复位并运行新固件")
    return 0


def cmd_boot(args) -> int:
    return subprocess.call([sys.executable, str(ESP_SERIAL), "boot", "--usb"])


def cmd_reset(args) -> int:
    return subprocess.call([sys.executable, str(ESP_SERIAL), "reset", "--usb"])


def cmd_mon(args) -> int:
    cmd = [sys.executable, str(ESP_SERIAL), "mon", "--usb"]
    if args.stamp or getattr(args, "all", False):
        cmd.append("--stamp")
    if args.hex:
        cmd.append("--hex")
    if args.log:
        cmd += ["--log", args.log]
    if args.duration:
        cmd += ["--duration", str(args.duration)]
    return subprocess.call(cmd)


def cmd_all(args) -> int:
    rc = cmd_build(args)
    if rc != 0:
        return rc
    rc = cmd_flash(args)
    if rc != 0:
        return rc
    if args.no_mon:
        return 0
    eprint("[esp] 输出监视中（Ctrl-] 或 Ctrl-C 退出）…")
    return cmd_mon(args)


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #

def add_build_args(sp):
    sp.add_argument("dir", nargs="?", help="工程目录（默认当前目录）")
    sp.add_argument("--board", default=None, help="板子 key，默认取配置")
    sp.add_argument("--menu", action="append", default=[], metavar="MENU=OPTION")
    sp.add_argument("--define", action="append", default=[], metavar="NAME=VALUE",
                    help="额外编译宏，如 --define RGB_PIN=38")
    sp.add_argument("--jobs", type=int, default=None, help="并行编译数")
    sp.add_argument("-v", "--verbose", action="store_true")


def add_flash_args(sp):
    sp.add_argument("--bridge-port", type=int, default=None)
    sp.add_argument("--chip", default=None)
    sp.add_argument("--flash-mode", default=None)
    sp.add_argument("--flash-freq", default=None)
    sp.add_argument("--flash-size", default=None)
    sp.add_argument("--erase", action="store_true", help="整片擦除后再写")
    sp.add_argument("--no-boot", action="store_true", help="不自动进下载模式（板子已在 bootloader）")
    sp.add_argument("--manual", action="store_true", help="手动按 BOOT+RST 进下载模式")


def add_mon_args(sp):
    sp.add_argument("--stamp", action="store_true", help="每行加时间戳")
    sp.add_argument("--hex", action="store_true", help="十六进制显示")
    sp.add_argument("--log", default=None, help="原始字节落盘")
    sp.add_argument("--duration", type=float, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="esp",
        description="Termux 上的 ESP32 工具套件：原生编译 + 烧录 + 串口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
典型用法
--------
  esp setup                      # 第一次：从 APK 自举工具链（~2 分钟）
  esp info                       # 看设备 / 工具链 / 工程
  esp new hello && cd hello      # 建模板工程
  esp build                      # 编译（几千行代码约 10 秒）
  esp flash                      # 自动进下载模式 + 烧写 + 复位
  esp mon --stamp                # 看串口输出
  esp all                        # 编译 + 烧写 + 监视，一条命令搞定
""",
    )
    parser.add_argument("--version", action="version", version=f"esp {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = sub.add_parser("setup", help="从 APK 自举工具链")
    p.add_argument("--apk", default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("info", help="环境/设备/工程状态")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("chip", help="自动识别板子芯片（会复位板子进下载模式）")
    p.add_argument("--no-boot", action="store_true", help="板子已在下载模式，跳过复位")
    p.add_argument("--save", action="store_true", help="把识别结果存为默认芯片/板子")
    p.set_defaults(func=cmd_chip)

    p = sub.add_parser("new", help="新建模板工程")
    p.add_argument("name")
    p.add_argument("--dir", default=None)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("build", help="编译工程")
    add_build_args(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("clean", help="删除 build 目录")
    p.add_argument("dir", nargs="?")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("boards", help="列出可用板子")
    p.add_argument("--arch", default=None,
                   help="芯片架构：esp32s3 / esp32s2 / esp32c3 / esp32 / esp8266")
    p.set_defaults(func=cmd_boards)

    p = sub.add_parser("flash", help="烧录")
    p.add_argument("dir", nargs="?")
    add_flash_args(p)
    p.set_defaults(func=cmd_flash)

    p = sub.add_parser("boot", help="进下载模式")
    p.set_defaults(func=cmd_boot)

    p = sub.add_parser("reset", help="硬复位")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("mon", help="串口监视")
    add_mon_args(p)
    p.set_defaults(func=cmd_mon)

    p = sub.add_parser("all", help="编译 + 烧录 + 监视")
    add_build_args(p)
    add_flash_args(p)
    add_mon_args(p)
    p.add_argument("--no-mon", action="store_true", help="烧完不监视")
    p.set_defaults(func=cmd_all)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        eprint("\n[esp] 已中断")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        eprint(f"[esp] 出错：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
