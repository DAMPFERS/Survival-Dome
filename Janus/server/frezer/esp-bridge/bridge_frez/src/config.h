// config: ////////////////////////////////////////////////////////////

#define OTA_HANDLER 
//#define MODE_AP          // отключено
#define MODE_STA           // ESP подключается к вашей точке доступа

#define PROTOCOL_TCP

bool debug = true;

#define VERSION "1.20-GRBL"

// --- Настройки вашей Wi-Fi сети ---
const char *ssid = "HONOR X7b";           // ← укажите SSID
const char *pw   = "89243536319";         // ← укажите пароль

// Для Station mode статический IP не нужен

#define NUM_COM   3
#define DEBUG_COM 0                 // отладка идёт в UART0

/*************************  COM Port 0 (отладка) *******************************/
#define UART_BAUD0 115200
#define SERIAL_PARAM0 SERIAL_8N1
#define SERIAL0_RXPIN 3
#define SERIAL0_TXPIN 1
#define SERIAL0_TCP_PORT 8880

/*************************  COM Port 1 *******************************/
#define UART_BAUD1 115200
#define SERIAL_PARAM1 SERIAL_8N1
#define SERIAL1_RXPIN 16
#define SERIAL1_TXPIN 17
#define SERIAL1_TCP_PORT 8881

/*************************  COM Port 2 (GRBL) *******************************/
#define UART_BAUD2 115200            // ← скорость GRBL
#define SERIAL_PARAM2 SERIAL_8N1
#define SERIAL2_RXPIN 15
#define SERIAL2_TXPIN 4
#define SERIAL2_TCP_PORT 8882       // ← основной порт для G-code

#define bufferSize 1024

// Индекс COM-порта, который используется для GRBL
#define GRBL_COM  1