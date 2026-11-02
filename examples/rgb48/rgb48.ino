// rgb48 — GPIO48 上的 WS2812 RGB 呼吸/渐变，用 ESP-IDF 的 RMT 定时器驱动
//
// 不需要 NeoPixel 库：RMT 以 10MHz 分辨率把 0/1 码元发出去，
// 0 码 = 0.3us 高 + 0.9us 低，1 码 = 0.9us 高 + 0.3us 低（WS2812/WS2812B 时序）。
#include <Arduino.h>
#include "driver/rmt_tx.h"

#ifndef RGB_PIN
#define RGB_PIN     48          // 板载 RGB LED 引脚（可用 esp build --define RGB_PIN=38 覆盖）
#endif
#ifndef RGB_BRIGHT_DIV
#define RGB_BRIGHT_DIV 4        // 亮度除数：越大越暗
#endif
#define RESOLUTION  10000000    // 10MHz → 每个 tick = 0.1us

static rmt_channel_handle_t s_chan = NULL;
static rmt_encoder_handle_t s_encoder = NULL;

static bool rgb_init() {
  rmt_tx_channel_config_t chan_cfg = {
      .gpio_num = (gpio_num_t)RGB_PIN,
      .clk_src = RMT_CLK_SRC_DEFAULT,
      .resolution_hz = RESOLUTION,
      .mem_block_symbols = 48,
      .trans_queue_depth = 4,
  };
  if (rmt_new_tx_channel(&chan_cfg, &s_chan) != ESP_OK) {
    return false;
  }
  rmt_bytes_encoder_config_t enc_cfg = {
      // 注意字段声明顺序是 duration0/level0/duration1/level1（C++ 指定初始化要求顺序一致）
      .bit0 = {.duration0 = 3, .level0 = 1, .duration1 = 9, .level1 = 0},   // 0.3us 高 / 0.9us 低
      .bit1 = {.duration0 = 9, .level0 = 1, .duration1 = 3, .level1 = 0},   // 0.9us 高 / 0.3us 低
      .flags = {.msb_first = 1},
  };
  if (rmt_new_bytes_encoder(&enc_cfg, &s_encoder) != ESP_OK) {
    return false;
  }
  return rmt_enable(s_chan) == ESP_OK;
}

static void rgb_set(uint8_t r, uint8_t g, uint8_t b) {
  uint8_t grb[3] = {g, r, b};          // WS2812 的线序是 GRB
  rmt_transmit_config_t tx_cfg = {.loop_count = 0};
  if (rmt_transmit(s_chan, s_encoder, grb, sizeof(grb), &tx_cfg) != ESP_OK) {
    return;
  }
  rmt_tx_wait_all_done(s_chan, 100);
  delayMicroseconds(80);               // >50us 低电平让灯锁存
}

// 色轮：hue 0..1535，每 256 是一整段
static void hue_to_rgb(uint16_t hue, uint8_t &r, uint8_t &g, uint8_t &b) {
  uint8_t seg = hue >> 8;              // 0..5
  uint8_t pos = hue & 0xFF;            // 段内 0..255
  uint8_t up = pos, down = 255 - pos;
  switch (seg) {
    case 0: r = 255; g = up;   b = 0;   break;   // 红 → 黄
    case 1: r = down; g = 255; b = 0;   break;   // 黄 → 绿
    case 2: r = 0;   g = 255;  b = up;  break;   // 绿 → 青
    case 3: r = 0;   g = down; b = 255; break;   // 青 → 蓝
    case 4: r = up;  g = 0;    b = 255; break;   // 蓝 → 洋红
    default: r = 255; g = 0;   b = down; break;  // 洋红 → 红
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("=== rgb48: GPIO48 WS2812 渐变色 ===");
  if (!rgb_init()) {
    Serial.println("[rgb48] RMT 初始化失败！");
    return;
  }
  Serial.println("[rgb48] RMT 就绪，开始转色轮");
  // 上电先来一遍红绿蓝，确认灯是好的
  rgb_set(255, 0, 0); delay(300);
  rgb_set(0, 255, 0); delay(300);
  rgb_set(0, 0, 255); delay(300);
}

void loop() {
  static uint16_t hue = 0;

  uint8_t r, g, b;
  hue_to_rgb(hue, r, g, b);

  // 亮度压到 1/4 左右，板载灯直吹眼睛太亮
  rgb_set(r / RGB_BRIGHT_DIV, g / RGB_BRIGHT_DIV, b / RGB_BRIGHT_DIV);

  if ((hue % 256) == 0) {
    Serial.printf("[rgb48] hue=%4u  R=%3u G=%3u B=%3u\n", hue, r / RGB_BRIGHT_DIV, g / RGB_BRIGHT_DIV, b / RGB_BRIGHT_DIV);
  }
  hue = (hue + 8) % 1536;
  delay(25);
}
