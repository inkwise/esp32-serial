// redblue — GPIO48 板载 WS2812 红蓝交替
//
// 用 ESP-IDF 的 RMT 外设直接发 WS2812 码元（不需要 NeoPixel 库）：
//   0 码 = 0.3us 高 + 0.9us 低，1 码 = 0.9us 高 + 0.3us 低，线序 GRB，帧尾拉低 80us 锁存。
#include <Arduino.h>
#include "driver/rmt_tx.h"

#ifndef RGB_PIN
#define RGB_PIN 48              // 板载 RGB LED（esp build --define RGB_PIN=38 可覆盖）
#endif
#ifndef BLINK_MS
#define BLINK_MS 500            // 红/蓝各亮多久
#endif
#ifndef RGB_BRIGHT_DIV
#define RGB_BRIGHT_DIV 2        // 亮度除数，越大越暗
#endif

#define RMT_RESOLUTION 10000000 // 10MHz → 1 tick = 0.1us

static rmt_channel_handle_t s_chan = NULL;
static rmt_encoder_handle_t s_enc = NULL;

static bool led_init() {
  rmt_tx_channel_config_t chan_cfg = {
      .gpio_num = (gpio_num_t)RGB_PIN,
      .clk_src = RMT_CLK_SRC_DEFAULT,
      .resolution_hz = RMT_RESOLUTION,
      .mem_block_symbols = 48,
      .trans_queue_depth = 4,
  };
  if (rmt_new_tx_channel(&chan_cfg, &s_chan) != ESP_OK) {
    return false;
  }
  rmt_bytes_encoder_config_t enc_cfg = {
      // 字段顺序必须与 rmt_symbol_word_t 声明一致：duration0/level0/duration1/level1
      .bit0 = {.duration0 = 3, .level0 = 1, .duration1 = 9, .level1 = 0},
      .bit1 = {.duration0 = 9, .level0 = 1, .duration1 = 3, .level1 = 0},
      .flags = {.msb_first = 1},
  };
  if (rmt_new_bytes_encoder(&enc_cfg, &s_enc) != ESP_OK) {
    return false;
  }
  return rmt_enable(s_chan) == ESP_OK;
}

static void led_set(uint8_t r, uint8_t g, uint8_t b) {
  uint8_t grb[3] = {g, r, b};                 // WS2812 线序是 GRB
  rmt_transmit_config_t tx_cfg = {.loop_count = 0};
  if (rmt_transmit(s_chan, s_enc, grb, sizeof(grb), &tx_cfg) != ESP_OK) {
    return;
  }
  rmt_tx_wait_all_done(s_chan, 100);
  delayMicroseconds(80);                      // >50us 低电平锁存
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.printf("=== redblue: GPIO%d 红蓝交替，每 %d ms 一换 ===\n", RGB_PIN, BLINK_MS);
  if (!led_init()) {
    Serial.println("[redblue] RMT 初始化失败，检查引脚定义");
    return;
  }
  Serial.println("[redblue] RMT 就绪");
  // 上电自检：红→蓝→红，确认灯是好的
  led_set(255 / RGB_BRIGHT_DIV, 0, 0); delay(150);
  led_set(0, 0, 255 / RGB_BRIGHT_DIV); delay(150);
  led_set(255 / RGB_BRIGHT_DIV, 0, 0); delay(150);
}

void loop() {
  static bool is_red = true;
  static uint32_t count = 0;

  if (is_red) {
    led_set(255 / RGB_BRIGHT_DIV, 0, 0);
  } else {
    led_set(0, 0, 255 / RGB_BRIGHT_DIV);
  }
  count++;
  Serial.printf("[redblue] #%lu %s\n", (unsigned long)count, is_red ? "RED" : "BLUE");

  is_red = !is_red;
  delay(BLINK_MS);
}
