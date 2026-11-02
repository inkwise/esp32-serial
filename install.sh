#!/usr/bin/env bash
# install.sh — 把 esp 工具套件装进 PATH 并写进 ~/.bashrc
#
#   bash install.sh              # 安装 / 更新（幂等）
#   bash install.sh --uninstall  # 卸载包装器和 bashrc 段（工具链 ~/.esp 保留）
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.esp/bin"
BASHRC="$HOME/.bashrc"
MARK_BEGIN="# >>> esp32 toolchain >>>"
MARK_END="# <<< esp32 toolchain <<<"
COMMANDS="setup info new build clean boards flash boot reset mon all"
SH="$(command -v sh)"

strip_bashrc() {
    [ -f "$BASHRC" ] || return 0
    grep -qF "$MARK_BEGIN" "$BASHRC" || return 0
    python3 - "$BASHRC" "$MARK_BEGIN" "$MARK_END" <<'PY'
import sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out, skip = [], False
for line in lines:
    if line.strip() == begin:
        skip = True
        continue
    if line.strip() == end:
        skip = False
        continue
    if not skip:
        out.append(line)
open(path, "w", encoding="utf-8").write("".join(out))
PY
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$BIN_DIR"
    strip_bashrc
    echo "已卸载：删掉了 $BIN_DIR 和 $BASHRC 里的 esp32 段"
    echo "工具链和 SDK 仍在 $HOME/.esp（想彻底清理：rm -rf ~/.esp）"
    exit 0
fi

chmod +x "$ROOT/esp_cli.py" "$ROOT/esp_serial.py" "$ROOT/espbuild.py" 2>/dev/null || true
mkdir -p "$BIN_DIR"

write_wrapper() {  # $1=文件名 $2=脚本(esp_cli|esp_serial) $3=子命令（可空）
    local name="$1" script="$2" sub="${3:-}"
    local target="$ROOT/esp_cli.py"
    [ "$script" = "esp_serial" ] && target="$ROOT/esp_serial.py"
    cat > "$BIN_DIR/$name" <<EOF
#!$SH
exec python3 "$target" $sub "\$@"
EOF
    chmod +x "$BIN_DIR/$name"
}

write_wrapper esp esp_cli ""
for cmd in $COMMANDS; do
    write_wrapper "esp-$cmd" esp_cli "$cmd"
done
# 串口工具箱（mon/repl/send/exec/pty/usb/bridge/boot/reset ...）用独立名字
write_wrapper esp-serial esp_serial ""

strip_bashrc
{
    echo ""
    echo "$MARK_BEGIN"
    echo "# ESP32 工具套件：原生 xtensa 工具链（来自 ArduinoDroid APK）+ 烧录 + 串口"
    echo 'export ESP_ROOT="${ESP_ROOT:-$HOME/.esp}"'
    echo 'export ESP_TOOLCHAIN="$ESP_ROOT/tc/bin"'
    echo 'export PATH="$ESP_ROOT/bin:$ESP_TOOLCHAIN:$PATH"'
    echo "alias esp-build='esp build'"
    echo "alias esp-flash='esp flash'"
    echo "alias esp-mon='esp mon --stamp'"
    echo "alias esp-all='esp all'"
    echo "alias esp-boot='esp boot'"
    echo "alias esp-info='esp info'"
    echo "$MARK_END"
} >> "$BASHRC"

echo "已安装：$BIN_DIR（$(ls "$BIN_DIR" | wc -l) 个命令）+ $BASHRC"
echo
echo "source ~/.bashrc 之后可用："
echo "  esp info / esp setup / esp new NAME / esp build / esp flash / esp mon --stamp / esp all"

# 顺手把串口工具箱的入口也指到老位置，方便脚本引用
[ -e "$ROOT/esp-serial" ] || ln -sf "$ROOT/esp_serial.py" "$ROOT/esp-serial" 2>/dev/null || true
