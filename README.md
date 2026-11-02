# ESP32 工具套件 —— Termux 原生，不需要 proot

在 Android 手机上完成 ESP32 的**编译 → 烧录 → 串口调试**，全部命令装进 `~/.bashrc`，一条 `esp all` 走完。

```
  ┌─ 编译 ──────────────────────────────────────────────────────┐
  │  bionic 版 xtensa-esp32s3-elf-gcc 14.2 + arduino-esp32 3.3.5 │
  │  .ino → .cpp(补原型) → 56 个 .o → link → elf2image → app.bin │
  │  实测：全新编译 6.5 秒、增量 3.5 秒（8 核并行）               │
  └─────────────────────────────────────────────────────────────┘
  ┌─ 烧录 ──────────────────────────────────────────────────────┐
  │  esp boot   → USB-JTAG DTR/RTS 时序让板子进 ROM 下载模式     │
  │  esptool    → 直接读写 CDC（espport.py 提供 pyserial 兼容层）│
  │  hard_reset → 退出下载模式，跑新固件                         │
  │  实测：写 325KB 用时 1.5 秒，esp all 全程 13 秒              │
  └─────────────────────────────────────────────────────────────┘
  ┌─ 调试 ──────────────────────────────────────────────────────┐
  │  esp mon --stamp / esp-serial repl / send / exec / pty       │
  └─────────────────────────────────────────────────────────────┘
```

---

## 0. 30 秒上手

```bash
cd ~/project/esp32-serial
bash install.sh                 # 装命令 + 写 bashrc
source ~/.bashrc

esp setup                       # 第一次：从 ArduinoDroid APK 自举工具链（约 1 分钟）
esp new hello && cd ~/esp-projects/hello
esp build                       # 编译
esp all                         # 编译 + 烧录 + 监视
```

### 两个开箱示例

```
examples/blink/    点灯 + 串口心跳（最小工程）
examples/rgb48/    GPIO48 板载 WS2812 渐变（RMT 驱动，不依赖 NeoPixel 库）
```

```bash
esp new myapp && cd ~/esp-projects/myapp     # 从模板起一个新工程
# 跑仓库里的示例：
esp build ~/project/esp32-serial/examples/rgb48 && esp flash ~/project/esp32-serial/examples/rgb48
# 换引脚 / 调亮度（编译宏直接传）：
esp build --define RGB_PIN=38 --define RGB_BRIGHT_DIV=1
```

`rgb48` 用的是 ESP-IDF 的 RMT 外设：10MHz 分辨率下发 WS2812 的 0/1 码元
（0 码 = 0.3µs 高 + 0.9µs 低，1 码 = 0.9µs 高 + 0.3µs 低），线序 GRB，
每帧后拉低 80µs 锁存。色轮 1536 步循环，约 2.4 秒转一圈。

## 1. 命令一览

| 命令 | 作用 |
|---|---|
| `esp setup [--apk PATH] [--force]` | 从 `.apks` 里抽出工具链、SDK、boards.txt，补齐 crosstool-NG 目录布局，装 esptool |
| `esp chip [--save]` | **自动识别芯片**：复位进下载模式，让 esptool 直接问 ROM，报型号/revision/flash/MAC，并说明能否编译 |
| `esp info` | 环境 / 工具链版本 / USB 设备 / 工程列表 一眼看清 |
| `esp new NAME` | 建一个最小工程（`~/.esp/config.json` 里的 `srcdir`） |
| `esp build [dir] [--board K] [--menu M=O] [--define N=V] [--jobs N]` | 编译，产物在 `<dir>/build/` |
| `esp flash [dir] [--manual] [--erase] [--no-boot]` | 进下载模式 → 桥接 → esptool 烧写 → 复位 |
| `esp boot` / `esp reset` | 单独触发下载模式 / 硬复位 |
| `esp mon [--stamp] [--hex] [--log F] [--duration S]` | 串口监视 |
| `esp all` | build + flash + mon，一条命令 |
| `esp clean` / `esp boards` | 清 build 目录 / 列出 131 块可用板子 |
| `esp-serial <子命令>` | 串口工具箱（详见 §5） |

`esp-build`、`esp-flash`、`esp-mon`、`esp-all`、`esp-boot`、`esp-info` 也都是独立命令；
别名 `esp-build/esp-flash/esp-mon/esp-all/esp-boot/esp-info` 已写进 bashrc。

## 2. 为什么能在手机上原生编译（这是整件事的关键）

ESP32-S3 是 xtensa 核，编译器只有 Espressif 的 `xtensa-esp-elf-gcc`。官方只发
**x86_64 / aarch64 的 glibc 版**，Termux 是 bionic，动态链接器对不上——所以社区方案一律是 proot。

但**手机里已经有一套 bionic 版的 xtensa 工具链**：ArduinoDroid 把 `gcc / g++ / as / ld / cc1 /
cc1plus / collect2` 全部交叉编译成了 Android aarch64 可执行文件，塞在 APK 的 `lib/arm64-v8a/`
里（用 `lib*.so` 的名字骗过打包器），配套的 sysroot、头文件、IDF 预编译库则在
`split_esp32s3_sdk.apk` 的 `res/raw/*.mp3`（其实是 7z）里。

`esp setup` 做的事就是把它们拼回一套可用的工具链：

| 包里 | 内容 | 落点 |
|---|---|---|
| `lib/arm64-v8a/*.so` | 98 个可执行文件（改名还原） | `~/.esp/ad/bin/` |
| `res/raw/f5.mp3` | 7z：arduino core 3.3.5 + IDF 5.5 预编译库 + sysroot | `~/.esp/ad/sdk/` |
| `res/raw/esp32s3_boards.txt` | 131 块板子的定义与菜单项 | `~/.esp/boards_esp32s3.txt` |

然后补两处 crosstool-NG 的布局细节（这两步不做就是 `cannot execute 'cc1'` / `'as'`）：

1. 驱动**复制**（不是软链）到 `~/.esp/tc/bin/`——gcc 用 `/proc/self/exe` 推 prefix，软链会推错；
2. `lib/gcc/<triple>/14.2.0/` 必须是**真目录**，里面逐项软链 SDK 的 multilib——
   因为 gcc 会从它做 `../../../../<triple>/bin/` 找 `as`/`ld`，软链目录会让相对路径跑到别的树里。

实测（8 核、无 proot、无网络）：

```
[espbuild] blink: 编译 56 个源文件（core 55 / 库 0 / sketch 1） 并行 8
[espbuild] 编译完成，用时 3.5s
[espbuild] 链接完成 → build/blink.elf
[espbuild] 应用大小 331472 字节 / 上限 1310720（25%）
```

产品级校验：`esptool image-info` 认出 `ESP32-S3 / DIO / 80MHz / 4MB`，入口地址正常。

## 3. 编译细节

```bash
esp build                       # 当前目录
esp build ~/esp-projects/hello  # 指定工程
esp boards                      # 看板子 key，如 esp32s3 / esp32s3box / xiao_esp32s3 ...
esp build --board esp32s3 --menu FlashSize=16M --menu PSRAM=enabled
esp build --jobs 4 -v           # 限制并行 / 显示编译告警
```

`espbuild.py` 的流水线：

1. **`.ino` 预处理**：所有 `.ino` 拼成一个 `.cpp`，前面插 `#include <Arduino.h>` 和自动生成的函数原型
   （Arduino IDE 用 ctags，这里用启发式扫描：跳过注释/字符串/预处理行/控制语句）。
2. **收集源文件**：sketch + `cores/esp32`（53 个） + `variants/<board>` + 按 `#include` 用到的库。
3. **并行编译**：flags 直接取自 SDK 的 `flags/{defines,c_flags,cpp_flags,S_flags,includes}`，
   加上 `boards.txt` 里这块板的 define（`ARDUINO_USB_MODE` 之类）。libstdc++ 的头文件路径
   需要显式 `-isystem`（工具链目录名被我们改成 `xtensa-esp32s3-elf`，gcc 自己猜不到 multilib）。
4. **打包 `core.a`** → **链接**：`ld_flags` + `ld_scripts`（`esp32s3.peripherals.ld`、`memory.ld`、
   `sections.ld`…）+ 152 个 IDF 静态库 + 各 flash 模式变体库（`qio_qspi/` 等）。
5. **elf2image** → `app.bin`，同时把预编译 bootloader、分区表、`boot_app0.bin` 都转成 bin，
   在 `build/build.json` 里记下完整 flash 布局。

产物：

```
build/
├── blink.elf / blink.ino.cpp / blink.map
├── blink.bin         331KB  应用（0x10000）
├── bootloader.bin     19KB  二级引导（0x0）
├── partitions.bin    305B   分区表（0x8000）
└── build.json        布局、体积、时间
```

## 4. 烧录：两段式，esptool 直接跑在 USB 上

USB-Serial-JTAG 进下载模式靠 **DTR/RTS 时序**（就是 esptool 的 `usb_jtag_bootloader_reset`：
IO0 拉低 → EN 脉冲 → 释放）。而 esptool 通过 `socket://` 连 TCP 时拿不到这两根线，
所以本工具把「复位」和「协议」拆开，中间不再有 TCP 中转：

```
esp flash
  ├─ 1/2 esp-serial boot --usb        ← 我们发 DTR/RTS 时序，芯片复位进 ROM 下载模式
  └─ 2/2 esp_esptool.py write-flash … ← esptool 直接读写 CDC（pyserial 兼容层 espport.py）
        0x0 bootloader.bin  0x8000 partitions.bin  0xe000 boot_app0.bin  0x10000 app.bin
        收尾脉冲 EN → 芯片重启跑新固件（不需要你按复位键）
```

`esp_esptool.py` 做的事很薄：把 `serial.serial_for_url("esp-usb://0")` 指向
`espport.UsbPort`（底层就是 libusb + CDC），其余全交给 esptool 自己——所以
`write-flash / read-flash / erase-flash / flash-id / merge-bin` 这些命令都能用，
`esptool` 的 stub、压缩、校验一个不少。

实测（本机 ESP32-S3，16MB flash）：

```
$ esp all
[espbuild] 编译完成，用时 3.5s          ← 56 个源文件
Writing at 0x0005f6a0 ... 100.0% 169.36kB/169.36kB [1s]
Wrote 325280 bytes (173428 compressed) at 0x00010000 in 1.5 seconds (1691.7 kbit/s)
Hash of data verified.
[esp] 烧写完成，芯片已复位并运行新固件
real 0m13.4s                            ← 编译+烧录+复位，一条命令
```

三个必须知道的细节（都踩过坑）：

1. **分区表要带 MD5**。IDF 5.x 的 bootloader 会校验；SDK 里自带的 `default.bin` 是旧格式，
   直接把 CSV 烧进去更不行（magic 变成 `# `）。`espbuild.py` 自己按 CSV 生成二进制，
   MD5 记录的字节布局是 `EB EB` + 14 个 `0xFF` + 16 字节摘要（摘要位于条目第 16..31 字节，
   不是 label 所在的 12..27）——这是对着原厂分区表逐字节比出来的。
2. **打开 CDC 时不能拉 DTR/RTS**。ESP32-S3 把 RTS 接 EN、DTR 接 IO0，主机一打开端口就置位
   等于把芯片按在复位里，esptool 会一直 `No serial data received`。所以 `_configure_port`
   显式下发 `SET_CONTROL_LINE_STATE(0x0000)`，复位只在 `boot`/`reset` 里显式做。
3. **`Serial` 默认走 UART0**。要在 USB 上看到 `Serial.print`，构建时打开
   `--menu CDCOnBoot=cdc`（本工具的默认配置已经帮你打开了）。

其他说明：

* 每次 `--usb` 动作都要过一次 `termux-usb -r`，手机上可能要再点一次「允许」（Android 通常记住授权）。
* 不想自动进下载模式：`esp flash --manual`（提示按住 BOOT → 点 RST → 松开，然后回车）。
* 整片擦除：`esp flash --erase`。
* 只想用 esptool 的其它命令（读 flash、看 MAC）：
  `esp-serial boot --usb` 之后手工跑 `esp_esptool.py`，见 §5 末尾。
* **刷机前先备份**：本机原厂 16MB 固件已备份在 `~/worldplug-backup.bin`，
  恢复：`esp-serial boot --usb` 然后
  `esp_esptool.py write-flash 0x0 ~/worldplug-backup.bin`（整片 16MB 一次写回）。


## 4.5 双 Type-C 口的板子怎么分辨

很多 ESP32-S3 开发板有两个 Type-C：一个接芯片自带的 USB，一个接板上焊的 USB-UART 桥。
插哪个口，`termux-usb -l` 报出来的 VID:PID 就不一样：

```bash
esp info                      # 先看有没有设备
esp-serial usb identify       # 再看芯片（会弹一次授权框）
```

| VID:PID | 是什么 | 典型丝印 | 连到芯片的什么 |
|---|---|---|---|
| `303a:1001` | ESP32-S3 **原生 USB**（USB-Serial-JTAG） | `USB` / `OTG` / `JTAG` | 芯片内部 USB 外设 |
| `1a86:55d3` | **CH343**（CH340 家族） | `UART` / `COM` / `232` | UART0（GPIO43/44） |
| `1a86:7523` | CH340/CH341 | 同上 | UART0 |
| `10c4:ea60` | CP2102 | 同上 | UART0 |
| `0403:6001` | FT232R | 同上 | UART0 |

两个口本工具都能用，复位时序会自动区分（原生 USB 走 USB-JTAG 时序，桥接芯片走经典
DTR/RTS 时序），实测都能编译→烧录→复位→监视走通。

**但日志走哪根线，取决于构建选项**：

| 构建选项 | `Serial.print` 去哪 | 哪个口能看到 |
|---|---|---|
| `--menu CDCOnBoot=cdc`（本工具默认） | USB-Serial-JTAG | **原生 USB 口** |
| `--menu CDCOnBoot=default` | UART0 | **CH343/UART 口** |

所以"插了 A 口却看不到日志"通常不是坏了，是构建时选了另一个口。
两边都想要的话可以同时插两根线，`Serial0`（UART0）和 `Serial` 各打各的。

经验之谈：

* 日常开发用**原生 USB 口**：少一层桥、能 JTAG 调试、吞吐也高（写 flash 约 1.7 Mbit/s）。
* **CH343 口**更适合救砖——它的 EN/IO0 复位电路比 USB-JTAG 的软复位可靠，
  ROM 的原始启动日志（`rst:0x...`、`SPIWP`）也只从这里出来。

## 4.6 芯片支持矩阵（能识别 ≠ 能编译）

`esp chip` 靠 **esptool 问 ROM** 来判断芯片，所以任何 ESP 芯片都认得出来；
但能不能**编译**，取决于本机装了哪些 SDK 包：

```bash
$ esp chip
  型号        ESP32-S3 (QFN56) (revision v0.2)
  特性        Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)
  Flash       16MB
  MAC         7c:4f:ad:1c:c5:b8

  架构标识    esp32s3
  可编译 SDK  ✓ esp32s3
  板子定义    ✓ boards_esp32s3.txt
```

| 芯片 | 编译器（APK 里有） | SDK（本机已装） | 识别 | 编译 | 烧录 |
|---|---|---|---|---|---|
| ESP32-S3 | ✅ `esp32s3_*` | ✅ esp32s3 | ✅ | ✅ | ✅ |
| ESP32-S2 | ✅ `esp32s2_*` | ❌ | ✅ | ❌ | ✅（现成 bin） |
| ESP32-C3 / C6 | ✅ `esp32c3_*` / `esp32c6_*` | ❌ | ✅ | ❌ | ✅ |
| ESP32 经典款 | ✅ `esp32_*` | ❌ | ✅ | ❌ | ✅ |
| ESP8266 | ✅ `esp8266_*`（xtensa-lx106） | ❌ | ✅ | ❌ | ✅ |
| AVR（Uno 等） | ✅ `avr_*` | ❌ | — | ❌ | ❌（要 avrdude 通道） |
| **STM32 / SAM** | ✅ `arm-m3_*` / `arm-m4_*`（arm-none-eabi） | ❌ | — | ❌ | ❌（要 DFU/ST-Link） |

**为什么只有 esp32s3 能编译**：ArduinoDroid 的 `.apks` 是按设备拆分的，你这台手机只带了
`split_esp32s3_sdk.apk`（里面是 arduino-esp32 3.3.5 的核心 + IDF 预编译库）。
其他芯片的编译器二进制全都在 `lib/arm64-v8a/` 里（一堆 `esp32c3_*`、`esp8266_*`、`arm-m3_*`），
缺的是各自的核心与 IDF 库。

补齐办法：拿一份**包含全部 SDK split 的 .apks**（APKMirror 之类的“通用包/universal”通常带），
然后 `esp setup --apk <新包> --force`，`esp info` 里 `已装 SDK` 就会多出几项。
`espflash` 的烧录侧不用改——`esptool` 本来就能自动识别并烧任意 ESP 芯片。

## 5. 串口工具箱（`esp-serial`）

数据通路有两条，命令一样：

| | 路线 A：桥接 App + TCP | 路线 B：原生 USB 直连（`--usb`） |
|---|---|---|
| 适用 | CH340 / CP2102 / FT232 的板子 | ESP32-S2/S3/C3 自带 USB（`303a:xxxx`） |
| 依赖 | 装 TCPUART，建 TCP Server 在 8080 | `pkg install libusb` + termux-api |
| 命令 | `esp-serial mon` | `esp-serial mon --usb` |

裸命令会**自动选路**：没写端点时，先看 `127.0.0.1:8080` 有没有桥接服务，
没有但插着 USB 设备就自动按 `--usb` 直连（会打一行提示）。
所以你不用记着加 `--usb`，`esp-serial mon` / `esp-serial repl` 直接敲就行；
显式写了 `--port` / `--endpoint` 时才完全按你说的走。

```bash
esp-serial usb identify          # 先认板子：303a:1001 = 原生 USB，10c4:ea60 = CP2102
esp-serial list                  # termux-usb 设备 + 本地桥接端口
esp-serial mon --usb --stamp     # 时间戳监视
esp-serial mon --hex --log a.log # 十六进制 + 落盘
esp-serial repl --usb            # 交互；Ctrl-C 或 Ctrl-] 退出
esp-serial repl --forward-ctrl-c # 反过来：把 Ctrl-C 转发给板子（中断 micropython 程序）
esp-serial exec --usb "help" --until ">>>"   # 脚本化：发一条等回显，退出码 0/3
esp-serial send --usb --hex '01 03 00 00' --append crlf
esp-serial pty --usb --link-file ~/esp32 &   # 供 esptool/mpremote/minicom 用（socat 替代）
esp-serial bridge --usb --listen 127.0.0.1:18080
esp-serial doctor                # 环境体检
```

esptool 任意子命令都能直接在 USB 上跑（不用桥接、不用 socat）：

```bash
esp-serial boot --usb                       # 先进下载模式
termux-usb -r -E -e "$HOME/.esp/venv/bin/python \
  $HOME/project/esp32-serial/esp_esptool.py \
    --chip esp32s3 --before no-reset --after no-reset read-mac" \
  /dev/bus/usb/001/002                      # 读 MAC（收尾会自动复位）
```

`--usb` 模式下有个坑已处理：`termux-usb -e` 会把子进程 stdout 换成管道并**等子进程退出才转发**，
所以实时输出走外层建的命名管道回来（`ESP_SERIAL_OUT_FIFO`），否则 `mon`/`pty` 会"卡住不出字"。

## 6. 自举与环境（`esp setup`）

```bash
esp setup                                   # 用配置里的 APK 路径
esp setup --apk /sdcard/Download/xxx.apks   # 指定 APK
esp setup --force                           # 重新解包
```

目录布局：

```
~/.esp/
├── ad/                     从 APK 解出来的原始内容
│   ├── bin/                98 个 bionic 可执行文件
│   └── sdk/                arduino core + IDF 库 + 工具链 sysroot
├── tc/bin/xtensa-esp32s3-elf-{gcc,g++,ar,size}   ← 补齐布局后的入口
├── venv/                   esptool + pyserial（已打 Android 补丁）
├── boards_esp32s3.txt      131 块板子
├── config.json             apk 路径 / 默认板子 / 工程目录 / 桥接端口
└── bin/                    esp、esp-build、esp-flash … 包装脚本
```

工程放 `~/esp-projects/<name>/`，`esp build` 会把产物写在工程内的 `build/`。

## 7. 和 proot 方案对比

| | 本方案（bionic 工具链） | proot-distro Debian + ESP-IDF |
|---|---|---|
| 体积 | ~500MB（全在 `~/.esp`） | 3–4GB |
| 首次搭建 | ~1 分钟（解包现成的工具链） | 20–40 分钟（联网装依赖 + 工具链） |
| 全新编译 | **6.5 秒** | 分钟级（proot 有 syscall 转换开销） |
| 增量编译 | 3.5 秒 | 更慢 |
| 依赖网络 | 只有 pip 装 esptool 那次 | 全程联网 |
| 烧录 | 直接走 USB | proot 里拿不到 termux-usb 的 fd，得回宿主侧 |
| 局限 | 只支持 Arduino 框架，板子限 APK 里带的 esp32s3/s3 系列 | 什么都能编（IDF、PlatformIO、多芯片） |

需要 ESP-IDF（`idf.py`）或其它芯片（esp32 经典款、C3/C6）时，proot 仍然是更好的选择；
日常写 Arduino 工程，这套快得多。

## 8. 排错

| 现象 | 处理 |
|---|---|
| `esp setup` 报找不到 APK | `esp setup --apk <路径>`；APK 要在手机存储里且 Termux 有存储权限（`termux-setup-storage`） |
| 编译报 `cannot execute 'cc1'` / `'as'` | 重新 `esp setup --force`；确认 `~/.esp/tc/libexec/gcc/xtensa-esp32s3-elf/14.2.0/cc1` 存在 |
| 编译报 `bits/c++config.h not found` | 同上；`espbuild` 会自己加 `-isystem` 到 SDK 的 multilib 头文件目录 |
| 编译报 `sdkconfig.h not found` | 板子的 memory 变体目录没选对（`build.memory_type`），用 `--menu FlashMode=...` 换一下 |
| 链接报 `undefined reference` 到某个库 | 该库没在 `ld_libs` 里，或在 sketch 里用了某个 Arduino 库但没被 `#include` 扫描到 |
| 烧录卡在「桥接没起来」 | 手机又弹授权框没点；或板子没进下载模式（用 `--manual` 手动按 BOOT+RST） |
| `LIBUSB_ERROR_BUSY` | 有 App / 上一个进程占着串口，`pkill -f _usb-link` 后重试 |
| 烧完串口没输出 | 构建时加 `--menu CDCOnBoot=cdc`（`Serial` 默认走 UART0）；或按一下 RST |
| `esp build` 报「本机没有 xxx 的 SDK」 | 该芯片的核心没装（见 §4.6），换 esp32s3 或补 SDK 包；烧现成 bin 不受影响 |
| `LIBUSB_ERROR_BUSY` 一直出现 | 内核 `cdc_acm` 抢先绑了 CDC 接口；本工具现在会自动 `libusb_detach_kernel_driver`。仍失败就拔插 OTG |
| 启动日志 `Incorrect MD5 checksum` / `invalid magic number` | 分区表格式不对：必须用 `espbuild` 生成的 `partitions.bin`（带 MD5），别把 CSV 或 SDK 里的旧 `default.bin` 直接烧 |
| esptool `No serial data received` | 芯片没进下载模式；或主机把 RTS 拉着（会按住 EN）。重跑 `esp flash`，或用 `--manual` |
| 烧完不复位、要手动按 RST | 已修（`espport.UsbPort.close()` 不再关底层链路，收尾才能发复位时序）；老版本可 `esp-serial reset --usb` |

## 9. 测试

```bash
cd ~/project/esp32-serial
python3 -m unittest discover -s tests -v     # 90 个用例
```

- `tests/test_espbuild.py`：boards.txt 解析与菜单默认值、`{build.x}` 变量展开、`.ino` 原型生成、
  注释/字符串剥离、库发现、**真实编译一次 blink**（无工具链时自动跳过）。
- `tests/test_esp_serial.py`：假 TCP 桥接服务器驱动的 mon/send/exec/pty、USB CDC 描述符与
  `SET_LINE_CODING` 打包、`--usb` 经 stub `termux-usb` 的参数串联、FIFO 实时输出等。

## 10. 目录与参考

```
esp32-serial/
├── esp_cli.py          esp 主入口（setup/info/new/build/flash/mon/all/...）
├── espbuild.py         原生构建引擎（.ino 预处理、并行编译、链接、elf2image）
├── esp_serial.py       串口工具箱（mon/repl/send/exec/pty/bridge/boot/reset/usb）
├── usb_cdc.py          CDC-ACM 直连 + DTR/RTS（进下载模式/复位）
├── espport.py          pyserial 兼容层：让 esptool 直接跑在 USB 上
├── esp_esptool.py      被 termux-usb 调起的 esptool 包装器（自动收尾复位）
├── libusb_probe.py     ctypes 绑 libusb：描述符解析、控制/批传输
├── install.sh          装命令 + 写 bashrc（支持 --uninstall）
├── examples/blink/     最小示例工程
└── tests/              84 个离线单测
```

- ArduinoDroid（bionic 工具链的来源）：[arduinodroid.app](https://www.arduinodroid.app/)，[Play Store](https://play.google.com/store/apps/details?id=name.antonsmirnov.android.arduinodroid2)
- arduino-esp32 3.3.5 / IDF 5.5 构建布局（`flags/`、multilib）：[espressif/arduino-esp32](https://github.com/espressif/arduino-esp32)
- USB-Serial-JTAG 复位时序（本工具 `bootloader_reset` 的依据）：[esptool `esp_pylib/serial_reset.py`](https://github.com/espressif/esptool)
- `termux-usb` 语义（`-l/-r/-e/-E`、fd 传递）：[Termux Wiki](https://wiki.termux.dev/wiki/Termux-usb)
- 桥接 App 路线（CH340 板子用）：[TCPUART transparent Bridge](https://play.google.com/store/apps/details?id=com.hardcodedjoy.tcpuart)，工作流实录见 [ESP32 and Termux](https://blog.gavide.dev/blog/esp32-and-termux)

## 仓库结构

```
esp32-serial/
├── esp-serial          命令行入口（设备扫描、串口交互、烧写）
├── espbuild.py         构建与烧写辅助
├── esp_cli.py          CLI 参数解析
├── esp_esptool.py      esptool 相关封装
├── esp_serial.py       串口协议实现（不依赖 proot / 原生 Termux）
├── espport.py          端口枚举与权限处理
├── examples/           示例工程（blink、redblue、rgb48）
├── tests/              单元测试
├── install.sh          安装脚本
└── libusb_probe.py     USB 设备探测工具
```

## 使用

```bash
./install.sh                 # 安装依赖（python 包 + Termux 组件）
python3 esp-serial scan      # 扫描串口设备
python3 esp-serial monitor   # 串口监视
python3 esp-serial flash ... # 烧写固件
```

`examples/*/build/` 与 `*.bin`、`*.elf` 都是编译产物，不入库。
