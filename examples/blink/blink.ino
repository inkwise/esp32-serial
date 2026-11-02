// ESP32-S3 点灯 + 串口打印（espbuild 原生编译示例）
#include <Arduino.h>

#ifndef LED_BUILTIN
#define LED_BUILTIN 21
#endif

static unsigned long last = 0;
static bool on = false;

void blinkOnce() {
  on = !on;
  digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
  Serial.printf("[blink] LED %s, uptime %lu ms\n", on ? "ON" : "OFF", millis());
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  delay(300);
  Serial.println();
  Serial.println("=== espbuild native toolchain demo ===");
  Serial.println("compiled on Android/Termux, no proot");
}

void loop() {
  if (millis() - last > 1000) {
    last = millis();
    blinkOnce();
  }
}
