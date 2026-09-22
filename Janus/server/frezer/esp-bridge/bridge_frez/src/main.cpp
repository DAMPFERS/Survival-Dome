#include <Arduino.h>
#include "config.h"
#include <esp_wifi.h>
#include <WiFi.h>

#ifdef OTA_HANDLER  
#include <ArduinoOTA.h> 
#endif

HardwareSerial Serial_one(1);
HardwareSerial Serial_two(2);
HardwareSerial* COM[NUM_COM] = {&Serial, &Serial_one, &Serial_two};

#define MAX_CLIENTS 1               // только один клиент

#ifdef PROTOCOL_TCP
#include <WiFiClient.h>
WiFiServer server_0(SERIAL0_TCP_PORT);
WiFiServer server_1(SERIAL1_TCP_PORT);
WiFiServer server_2(SERIAL2_TCP_PORT);
WiFiServer *server[NUM_COM] = {&server_0, &server_1, &server_2};
WiFiClient TCPClient[NUM_COM][MAX_CLIENTS];
#endif

uint8_t buf1[NUM_COM][bufferSize];
uint16_t i1[NUM_COM] = {0};

uint8_t buf2[NUM_COM][bufferSize];
uint16_t i2[NUM_COM] = {0};

// ===== Флаг подтверждения от GRBL =====
volatile bool ok_flag = false;

// ===== Переменные для Wi-Fi reconnect =====
unsigned long lastWifiCheck = 0;
const unsigned long WIFI_CHECK_INTERVAL = 5000;  // проверка каждые 5 секунд
bool wifiWasConnected = false;



bool checkForOk(const uint8_t* data, uint16_t len); // Проверка, пришло ли "ok" в буфере

bool handleSpecialCommand(int num, const uint8_t* data, uint16_t len, WiFiClient& client); // Обработка специальных команд от PC


void setup() {
  delay(500);
  
  COM[0]->begin(UART_BAUD0, SERIAL_PARAM0, SERIAL0_RXPIN, SERIAL0_TXPIN);
  COM[1]->begin(UART_BAUD1, SERIAL_PARAM1, SERIAL1_RXPIN, SERIAL1_TXPIN);
  COM[2]->begin(UART_BAUD2, SERIAL_PARAM2, SERIAL2_RXPIN, SERIAL2_TXPIN);
  
  if (debug) COM[DEBUG_COM]->println("\n\nESP32 GRBL WiFi Bridge " VERSION);

#ifdef MODE_STA
  if (debug) COM[DEBUG_COM]->println("Station mode");
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pw);
  
  if (debug) {
    COM[DEBUG_COM]->print("Connecting to: ");
    COM[DEBUG_COM]->println(ssid);
  }
  
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    if (debug) COM[DEBUG_COM]->print(".");
  }
  
  if (debug) {
    COM[DEBUG_COM]->println("\nWiFi connected");
    COM[DEBUG_COM]->print("IP address: ");
    COM[DEBUG_COM]->println(WiFi.localIP());
  }
#endif

#ifdef OTA_HANDLER  
  ArduinoOTA
    .onStart([]() {
      Serial.println("Start updating");
    })
    .onEnd([]() {
      Serial.println("\nEnd");
    })
    .onProgress([](unsigned int progress, unsigned int total) {
      Serial.printf("Progress: %u%%\r", (progress / (total / 100)));
    })
    .onError([](ota_error_t error) {
      Serial.printf("Error[%u]: ", error);
      if (error == OTA_AUTH_ERROR) Serial.println("Auth Failed");
      else if (error == OTA_BEGIN_ERROR) Serial.println("Begin Failed");
      else if (error == OTA_CONNECT_ERROR) Serial.println("Connect Failed");
      else if (error == OTA_RECEIVE_ERROR) Serial.println("Receive Failed");
      else if (error == OTA_END_ERROR) Serial.println("End Failed");
    });
  ArduinoOTA.begin();
#endif

#ifdef PROTOCOL_TCP
  for (int num = 0; num < NUM_COM; num++) {
    server[num]->begin();
    server[num]->setNoDelay(true);
    if (debug) {
      COM[DEBUG_COM]->print("TCP Server started on port ");
      COM[DEBUG_COM]->println(num == 0 ? SERIAL0_TCP_PORT : 
                              num == 1 ? SERIAL1_TCP_PORT : SERIAL2_TCP_PORT);
    }
  }
#endif

  // Немного снижаем мощность WiFi (по желанию)
  // esp_wifi_set_max_tx_power(50);
}

void loop() {  
#ifdef OTA_HANDLER  
  ArduinoOTA.handle();
#endif

  // ========== Автоматический reconnect Wi-Fi ==========
#ifdef MODE_STA
  if (millis() - lastWifiCheck >= WIFI_CHECK_INTERVAL) {
    lastWifiCheck = millis();

    if (WiFi.status() != WL_CONNECTED) {
      if (wifiWasConnected) {
        if (debug) COM[DEBUG_COM]->println("\n[WiFi] Connection lost! Reconnecting...");
        wifiWasConnected = false;
      }

      WiFi.disconnect();
      WiFi.begin(ssid, pw);

      // Ждём до 10 секунд (не блокируем полностью)
      unsigned long startAttempt = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 10000) {
        delay(200);
        if (debug) COM[DEBUG_COM]->print(".");
      }

      if (WiFi.status() == WL_CONNECTED) {
        wifiWasConnected = true;
        if (debug) {
          COM[DEBUG_COM]->println("\n[WiFi] Reconnected!");
          COM[DEBUG_COM]->print("[WiFi] IP: ");
          COM[DEBUG_COM]->println(WiFi.localIP());
        }
      } else {
        if (debug) COM[DEBUG_COM]->println("\n[WiFi] Reconnect failed, will try again later");
      }
    } else {
      // Соединение в порядке
      if (!wifiWasConnected) {
        wifiWasConnected = true;
        if (debug) {
          COM[DEBUG_COM]->println("\n[WiFi] Connected");
          COM[DEBUG_COM]->print("[WiFi] IP: ");
          COM[DEBUG_COM]->println(WiFi.localIP());
        }
      }
    }
  }
#endif
  // ====================================================

#ifdef PROTOCOL_TCP
  // Приём новых клиентов (максимум 1 на порт)
  for (int num = 0; num < NUM_COM; num++) {
    if (server[num]->hasClient()) {
      if (TCPClient[num][0] && TCPClient[num][0].connected()) {
        WiFiClient reject = server[num]->available();
        reject.stop();
        if (debug) COM[DEBUG_COM]->println("Client rejected (already connected)");
      } else {
        if (TCPClient[num][0]) TCPClient[num][0].stop();
        TCPClient[num][0] = server[num]->available();
        if (debug) {
          COM[DEBUG_COM]->print("New client on COM");
          COM[DEBUG_COM]->println(num);
        }
      }
    }
  }
#endif

  // Основной обмен данными
  for (int num = 0; num < NUM_COM; num++) {
    if (COM[num] == NULL) continue;

    // ----- Данные от TCP-клиента → UART -----
    if (TCPClient[num][0] && TCPClient[num][0].connected()) {
      while (TCPClient[num][0].available()) {
        buf1[num][i1[num]] = TCPClient[num][0].read();
        if (i1[num] < bufferSize - 1) i1[num]++;
      }

      if (i1[num] > 0) {
        if (!handleSpecialCommand(num, buf1[num], i1[num], TCPClient[num][0])) {
          COM[num]->write(buf1[num], i1[num]);
        }
        i1[num] = 0;
      }
    }

    // ----- Данные от UART → TCP-клиент -----
    if (COM[num]->available()) {
      while (COM[num]->available()) {
        buf2[num][i2[num]] = COM[num]->read();
        if (i2[num] < bufferSize - 1) i2[num]++;
      }

      if (i2[num] > 0) {
        if (num == GRBL_COM) {
          if (checkForOk(buf2[num], i2[num])) {
            ok_flag = true;
            if (debug) COM[DEBUG_COM]->println(">>> ok received, flag=1");
          }
        }

        if (TCPClient[num][0] && TCPClient[num][0].connected()) {
          TCPClient[num][0].write(buf2[num], i2[num]);
        }
        i2[num] = 0;
      }
    }
  }
}


// Проверка, пришло ли "ok" в буфере
bool checkForOk(const uint8_t* data, uint16_t len) {
  // Ищем последовательность 'o','k' (без учёта регистра можно расширить)
  for (uint16_t i = 0; i + 1 < len; i++) {
    if (data[i] == 'o' && data[i+1] == 'k') {
      return true;
    }
  }
  return false;
}

// Обработка специальных команд от PC
// Возвращает true, если команда была обработана (не нужно слать в UART)
bool handleSpecialCommand(int num, const uint8_t* data, uint16_t len, WiFiClient& client) {
  if (num != GRBL_COM) return false;   // спецкоманды только на порту GRBL

  // Простейший разбор (ожидаем короткие команды)
  if (len >= 3 && data[0] == '?' && data[1] == 'O' && data[2] == 'K') {
    client.print(ok_flag ? "OK:1\n" : "OK:0\n");
    return true;
  }
  if (len >= 4 && data[0] == '!' && data[1] == 'C' && data[2] == 'L' && data[3] == 'R') {
    ok_flag = false;
    client.print("CLR:OK\n");
    return true;
  }
  return false;
}
