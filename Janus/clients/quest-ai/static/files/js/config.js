// Настройки. Для подключения реального бекенда поставьте useMock:false и укажите адреса.
const CONFIG = {
  useMock: false,                   // true = демо-данные без сервера
  telemetryUrl: '/api/telemetry',   // GET -> JSON (формат см. mockTelemetry() в telemetry.js)
  chatUrl: '/api/chat',             // POST {message} -> {reply}
  refreshMs: 2000,                  // период обновления телеметрии
  lineNames: ['Линия 1','Линия 2','Линия 3','Линия 4','Линия 5','Линия 6','Линия 7','Линия 8'],
  lineMaxKw: 1.5                    // максимум на одну линию (для шкалы)
};
