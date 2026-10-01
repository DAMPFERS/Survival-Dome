#include <Arduino.h>
#include "config.h"
#include <esp_wifi.h>
#include <WiFi.h>

#ifdef OTA_HANDLER
#include <ArduinoOTA.h>
#endif

// ESP32 <-> GRBL: ПРОЗРАЧНЫЙ мост TCP <-> UART.
// Протокол ?OK / !CLR удалён: Python читает ok/error/статус прямо из потока.

HardwareSerial Serial_one(1);
HardwareSerial Serial_two(2);
HardwareSerial* COM[NUM_COM] = {&Serial, &Serial_one, &Serial_two};

#ifdef PROTOCOL_TCP
#include <WiFiClient.h>
WiFiServer server_0(SERIAL0_TCP_PORT);
WiFiServer server_1(SERIAL1_TCP_PORT);
WiFiServer server_2(SERIAL2_TCP_PORT);
WiFiServer* server[NUM_COM] = {&server_0, &server_1, &server_2};
WiFiClient TCPClient[NUM_COM];
#endif

static uint8_t buf[bufferSize];

// ===== Wi-Fi reconnect (неблокирующий) =====
unsigned long lastWifiCheck = 0;
unsigned long wifiLostSince = 0;
const unsigned long WIFI_CHECK_INTERVAL = 2000;
const unsigned long WIFI_RESTART_AFTER = 20000;   // полный WiFi.begin() раз в 20 c
bool wifiWasConnected = false;

static uint16_t tcpPort(int num) {
  return num == 0 ? SERIAL0_TCP_PORT : num == 1 ? SERIAL1_TCP_PORT : SERIAL2_TCP_PORT;
}

void setup() {
  delay(500);

  // Размер приёмного буфера UART нужно задать ДО begin().
  // По умолчанию 256 байт - при задержках в loop() вывод GRBL терялся бы.
  for (int i = 0; i < NUM_COM; i++) COM[i]->setRxBufferSize(1024);

  COM[0]->begin(UART_BAUD0, SERIAL_PARAM0, SERIAL0_RXPIN, SERIAL0_TXPIN);
  COM[1]->begin(UART_BAUD1, SERIAL_PARAM1, SERIAL1_RXPIN, SERIAL1_TXPIN);
  COM[2]->begin(UART_BAUD2, SERIAL_PARAM2, SERIAL2_RXPIN, SERIAL2_TXPIN);

  if (debug) COM[DEBUG_COM]->println("\n\nESP32 GRBL WiFi Bridge " VERSION);

#ifdef MODE_STA
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // modem-sleep даёт задержки 100-300 мс на каждый ответ
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, pw);

  if (debug) {
    COM[DEBUG_COM]->print("Connecting to: ");
    COM[DEBUG_COM]->println(ssid);
  }
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    if (debug) COM[DEBUG_COM]->print(".");
  }
  wifiWasConnected = true;
  if (debug) {
    COM[DEBUG_COM]->print("\nWiFi connected, IP: ");
    COM[DEBUG_COM]->println(WiFi.localIP());
  }
#endif

#ifdef OTA_HANDLER
  // ArduinoOTA.setPassword("change-me");   // без пароля прошить может любой в сети
  ArduinoOTA
    .onStart([]() { Serial.println("OTA start"); })
    .onEnd([]() { Serial.println("\nOTA end"); })
    .onError([](ota_error_t error) { Serial.printf("OTA error[%u]\n", error); });
  ArduinoOTA.begin();
#endif

#ifdef PROTOCOL_TCP
  for (int num = 0; num < NUM_COM; num++) {
    server[num]->begin();
    server[num]->setNoDelay(true);
    if (debug) {
      COM[DEBUG_COM]->print("TCP server on port ");
      COM[DEBUG_COM]->println(tcpPort(num));
    }
  }
#endif
}

#ifdef MODE_STA
static void handleWifi() {
  if (millis() - lastWifiCheck < WIFI_CHECK_INTERVAL) return;
  lastWifiCheck = millis();

  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      if (debug) {
        COM[DEBUG_COM]->print("[WiFi] Reconnected, IP: ");
        COM[DEBUG_COM]->println(WiFi.localIP());
      }
    }
    return;
  }

  if (wifiWasConnected) {
    wifiWasConnected = false;
    wifiLostSince = millis();
    if (debug) COM[DEBUG_COM]->println("[WiFi] Connection lost");
  }

  // Ничего не ждём в цикле: автопереподключение работает в фоне,
  // а если оно "залипло" - перезапускаем подключение.
  if (millis() - wifiLostSince > WIFI_RESTART_AFTER) {
    wifiLostSince = millis();
    if (debug) COM[DEBUG_COM]->println("[WiFi] Restarting connection");
    WiFi.disconnect();
    WiFi.begin(ssid, pw);
  }
}
#endif

void loop() {
#ifdef OTA_HANDLER
  ArduinoOTA.handle();
#endif

#ifdef MODE_STA
  handleWifi();
#endif

#ifdef PROTOCOL_TCP
  // Новый клиент ВЫТЕСНЯЕТ старого. После обрыва Wi-Fi старое соединение
  // может долго считаться "connected" (без keepalive), и при логике
  // "отклонить нового" ПК не смог бы переподключиться.
  for (int num = 0; num < NUM_COM; num++) {
    if (server[num]->hasClient()) {
      WiFiClient newClient = server[num]->available();
      if (TCPClient[num]) {
        TCPClient[num].stop();
        if (debug) COM[DEBUG_COM]->println("Old client replaced");
      }
      TCPClient[num] = newClient;
      TCPClient[num].setNoDelay(true);
      if (debug) {
        COM[DEBUG_COM]->print("New client on COM");
        COM[DEBUG_COM]->println(num);
      }
    }
  }
#endif

  for (int num = 0; num < NUM_COM; num++) {
    // ----- TCP -> UART (блоками, без потери байт при переполнении) -----
    if (TCPClient[num] && TCPClient[num].connected()) {
      int n;
      while ((n = TCPClient[num].available()) > 0) {
        n = TCPClient[num].read(buf, min(n, (int)bufferSize));
        if (n <= 0) break;
        COM[num]->write(buf, n);
      }
    }

    // ----- UART -> TCP -----
    int n;
    while ((n = COM[num]->available()) > 0) {
      n = COM[num]->read(buf, min(n, (int)bufferSize));
      if (n <= 0) break;
      if (TCPClient[num] && TCPClient[num].connected()) {
        TCPClient[num].write(buf, n);
      }
    }
  }
}
