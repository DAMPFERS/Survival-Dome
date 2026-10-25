// Настройки. Для подключения реального бекенда поставьте useMock:false и укажите адреса.
const CONFIG = {
  useMock: false,                   // true = демо-данные без сервера
  telemetryUrl: '/api/telemetry',   // GET -> JSON (формат см. mockTelemetry() в telemetry.js)
  chatUrl: '/api/chat',             // POST {message} -> {reply}
  refreshMs: 2000,                  // период обновления телеметрии
  // подписи для демо-режима; с сервером приходят lineNames
  lineNames: ['Мастерская + вытяжка','Теплица ФабЛаб','3D-принтер','ЧПУ-фрезер','Теплица Химлаб','Освещение','Аварийный свет','Резерв'],
  lineMaxKw: 1.5                    // максимум на одну линию (для шкалы)
};
