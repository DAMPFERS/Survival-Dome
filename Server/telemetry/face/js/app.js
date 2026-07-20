/******************************************************************************
 * DOME TELEMETRY HUD
 * Version: 0.1
 * 
 * @description Главный модуль HUD-интерфейса для системы телеметрии купола.
 *              Отвечает за отображение климатических данных, состояния питания
 *              и управление каналами нагрузки.
 * @todo Перейти на реальные данные через WebSocket (см. секцию FUTURE WEBSOCKET API)
 * @todo Добавить обработку ошибок и состояния загрузки
 * @todo Вынести конфигурацию (HISTORY_LENGTH, CHANNEL_NAMES) в отдельный файл
 ******************************************************************************/

/**
 * Глобальный объект состояния приложения.
 * @property {Object} climate - Показатели климата
 * @property {number} climate.temperature - Текущая температура (в °C)
 * @property {number} climate.humidity - Текущая влажность (в %)
 * @property {number} climate.co2 - Уровень CO2 (в ppm)
 * @property {number[]} climate.*History - Исторические значения для спарклайнов
 * @property {Object} power - Секция питания
 * @property {number} power.solarGeneration - Мощность солнечных панелей (в kW)
 * @property {number} power.battery - Уровень заряда батарей (в %)
 * @property {Object[]} power.channels - Массив каналов нагрузки
 * @property {number} missionStart - Timestamp начала миссии (для таймера)
 * 
 * @note В будущем state может пополняться другими разделами (например, 'alerts', 'systems')
 */
const state = {

    climate: {

        temperature: 22.4,
        humidity: 48,
        co2: 620,

        temperatureHistory: [],
        humidityHistory: [],
        co2History: []
    },

    power: {

        solarGeneration: 3.8,
        battery: 87,

        channels: []
    },

    missionStart: Date.now()
};

/******************************************************************************
 * CONFIG
 * 
 * @description Конфигурационные константы, вынесенные для легкой настройки
 *              без изменения логики. Являются точкой входа для настройки HUD.
 * @see HISTORY_LENGTH - влияет на количество точек на графиках и память
 * @see CHANNEL_NAMES - определяет количество и названия переключаемых каналов
 ******************************************************************************/

/** @constant {number} - Количество точек в истории для спарклайнов (30 ≈ 1 минута при обновлении раз в 2с) */
const HISTORY_LENGTH = 30;

/** 
 * @constant {string[]} - Имена каналов нагрузки. Порядок важен: определяет ID каналов.
 * @note При добавлении/удалении каналов нужно обновить и интерфейс (CSS-сетка)
 */
const CHANNEL_NAMES = [

    "LIGHTING",
    "WATER PUMP",
    "GREENHOUSE",
    "WORKSHOP",

    "VENTILATION",
    "COMPUTERS",
    "REACTOR",
    "RESERVE"
];

/******************************************************************************
 * INIT
 * 
 * @description Инициализация приложения после загрузки DOM.
 *              Важно: все функции инициализации синхронны, т.к. данные симулируются.
 * @warn Если перейти на реальный API, инициализацию нужно сделать асинхронной
 *       с обработкой состояний загрузки/ошибок.
 ******************************************************************************/

window.addEventListener("DOMContentLoaded", () => {

    initChannels();        // Создаем каналы с случайными начальными параметрами
    initHistory();         // Заполняем историю начальными данными (чтобы спарклайны не были пустыми)

    buildPowerGrid();      // Рендерим сетку каналов в DOM

    updateAll();           // Первичный рендеринг всех виджетов

    // Таймеры обновления UI
    setInterval(updateClock, 1000);        // Каждую секунду обновляем часы
    setInterval(updateMissionTimer, 1000); // Каждую секунду обновляем таймер миссии
    setInterval(simulateTelemetry, 2000);  // Каждые 2 секунды симулируем новые данные
});

/******************************************************************************
 * CHANNELS
 * 
 * @description Инициализация каналов нагрузки. Генерирует случайные параметры
 *              для демонстрации UI. Ключевой момент: здесь закладывается бизнес-логика,
 *              что канал может быть включен/выключен и потреблять мощность.
 * @see CHANNEL_NAMES - источник данных для имен
 ******************************************************************************/

function initChannels() {

    state.power.channels = CHANNEL_NAMES.map((name, index) => ({
        id: index + 1,                    // ID начинается с 1 для соответствия L1, L2...
        name,                             // Название канала (используется в UI)
        enabled: Math.random() > 0.2,     // 80% каналов включены (реалистичный сценарий)
        power: Math.floor(50 + Math.random() * 450) // Мощность в ваттах (50-500 Вт)
    }));
}

/******************************************************************************
 * HISTORY
 * 
 * @description Заполнение начальными данными для спарклайнов.
 *              Без этого графики будут пустыми при первом рендере.
 * @note Используются те же диапазоны, что и в симуляции (для согласованности)
 ******************************************************************************/

function initHistory() {

    for (let i = 0; i < HISTORY_LENGTH; i++) {
        state.climate.temperatureHistory.push(22 + Math.random() * 3);   // 22-25°C
        state.climate.humidityHistory.push(40 + Math.random() * 15);    // 40-55%
        state.climate.co2History.push(550 + Math.random() * 120);       // 550-670 ppm
    }
}

/******************************************************************************
 * POWER GRID
 * 
 * @description Построение сетки переключателей каналов питания.
 *              Реализует паттерн "рендеринг по состоянию": каждый вызов
 *              полностью перестраивает DOM-элементы каналов.
 * @performance При 8 каналах это приемлемо, но при большом количестве (50+)
 *              стоит перейти на обновление существующих элементов.
 * @see buildPowerGrid - вызывается при каждом изменении состояния каналов
 ******************************************************************************/

function buildPowerGrid() {

    const container = document.getElementById("powerGrid");
    container.innerHTML = ""; // Очищаем контейнер перед перестроением

    state.power.channels.forEach(channel => {
        const element = document.createElement("div");
        // Используем BEM-подобную нотацию для классов: блок, элемент, модификатор
        element.className = channel.enabled
            ? "hud-toggle hud-toggle--on"
            : "hud-toggle";

        element.dataset.id = channel.id; // Храним ID для возможных обработчиков

        element.innerHTML = `
            <div class="hud-toggle__name">L${channel.id}</div>
            <div class="hud-toggle__power">${channel.power.toFixed(1)} W</div>
            <div class="hud-toggle__state">${channel.enabled ? "ON" : "OFF"}</div>
        `;

        // Обработчик клика — переключение состояния канала
        element.addEventListener("click", () => {
            channel.enabled = !channel.enabled;

            // Бизнес-правило: при выключении мощность сбрасывается, при включении — генерируется заново
            if (!channel.enabled) {
                channel.power = 0;
            } else {
                channel.power = randomInt(80, 500);
            }

            // Перерисовываем сетку и обновляем суммарные показатели
            buildPowerGrid();
            updatePowerSummary();
        });

        container.appendChild(element);
    });
}

/******************************************************************************
 * CLOCK
 * 
 * @description Отображение системного времени в формате 24-часового времени (HH:MM:SS).
 *              Использует локаль en-GB для единообразия формата независимо от системных настроек.
 * @see updateClock - вызывается по таймеру каждую секунду
 ******************************************************************************/

function updateClock() {
    const now = new Date();
    const time = now.toLocaleTimeString("en-GB", { hour12: false });
    document.getElementById("clock").textContent = time;
}

/******************************************************************************
 * MISSION TIMER
 * 
 * @description Отображение времени с начала миссии (в формате ЧЧ:ММ).
 *              В отличие от часов, этот таймер показывает нарастающий счетчик.
 * @note Использует state.missionStart как точку отсчета.
 *       При перезагрузке страницы счетчик сбросится — возможно, стоит хранить
 *       это значение в sessionStorage для сохранения сессии.
 ******************************************************************************/

function updateMissionTimer() {
    const elapsed = Math.floor((Date.now() - state.missionStart) / 1000);
    const hours = String(Math.floor(elapsed / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");

    document.getElementById("missionTime").textContent = `${hours}:${minutes}`;
}

/******************************************************************************
 * TELEMETRY SIMULATION
 * 
 * @description Генерация новых телеметрических данных с использованием
 *              алгоритма случайного блуждания (drift).
 *              Это временное решение до подключения реального источника данных.
 * @warn Симуляция не учитывает корреляции между параметрами (например, 
 *       рост температуры должен влиять на влажность) — для демо-версии достаточно.
 * @see drift - функция, обеспечивающая плавное изменение значений
 ******************************************************************************/

function simulateTelemetry() {

    // Климатические показатели дрейфуют в заданных пределах
    state.climate.temperature = drift(state.climate.temperature, 18, 28, 0.4);
    state.climate.humidity = drift(state.climate.humidity, 30, 70, 2);
    state.climate.co2 = drift(state.climate.co2, 450, 1200, 20);

    // Сохраняем новые значения в историю
    pushHistory(state.climate.temperatureHistory, state.climate.temperature);
    pushHistory(state.climate.humidityHistory, state.climate.humidity);
    pushHistory(state.climate.co2History, state.climate.co2);

    // Обновляем мощность включенных каналов (выключенные остаются на 0)
    state.power.channels.forEach(channel => {
        if (!channel.enabled) return;
        channel.power = drift(channel.power, 50, 500, 30);
    });

    // Солнечная генерация и заряд батареи тоже дрейфуют
    state.power.solarGeneration = drift(state.power.solarGeneration, 0.2, 5.5, 0.3);
    state.power.battery = drift(state.power.battery, 10, 100, 1);

    // Обновляем весь UI
    updateAll();
}

/******************************************************************************
 * UPDATE UI
 * 
 * @description Централизованная точка обновления всех компонентов интерфейса.
 *              Разбита на отдельные функции для ясности и возможности
 *              частичного обновления (если потребуется оптимизация).
 ******************************************************************************/

function updateAll() {
    updateClimate();
    updatePowerSummary();
    buildPowerGrid(); // Перерисовка сетки при каждом обновлении — избыточно, но безопасно
}

/**
 * Обновляет все виджеты климатической секции:
 * - Текущие значения (температура, влажность, CO2)
 * - Спарклайны для каждого показателя
 * - Проверка на критические значения (алерты)
 */
function updateClimate() {
    document.getElementById("tempValue").textContent = `${state.climate.temperature.toFixed(1)}°C`;
    document.getElementById("humValue").textContent = `${Math.round(state.climate.humidity)}%`;
    document.getElementById("co2Value").textContent = `${Math.round(state.climate.co2)} ppm`;

    drawSparkline("tempSpark", state.climate.temperatureHistory);
    drawSparkline("humSpark", state.climate.humidityHistory);
    drawSparkline("co2Spark", state.climate.co2History);

    checkClimateAlerts();
}

/**
 * Обновляет суммарные показатели секции питания:
 * - Генерация солнечных панелей
 * - Общая нагрузка (сумма мощностей включенных каналов)
 * - Уровень заряда батареи
 * @note Нагрузка пересчитывается каждый раз, а не хранится в state
 *       — компромисс между простотой и производительностью
 */
function updatePowerSummary() {
    const totalLoad = state.power.channels
        .filter(c => c.enabled)
        .reduce((sum, c) => sum + c.power, 0);

    document.getElementById("solarValue").textContent = `${state.power.solarGeneration.toFixed(1)} kW`;
    document.getElementById("loadValue").textContent = `${(totalLoad / 1000).toFixed(1)} kW`;
    document.getElementById("batteryValue").textContent = `${Math.round(state.power.battery)}%`;
}

/******************************************************************************
 * ALERTS
 * 
 * @description Система оповещения о критических значениях.
 *              Сейчас реализован только один порог (CO2 > 1000 ppm).
 * @todo Расширить систему алертов: добавить пороги для температуры и влажности,
 *       добавить звуковые оповещения или всплывающие уведомления.
 * @see value--danger - CSS-класс для стилизации опасных значений
 ******************************************************************************/

function checkClimateAlerts() {
    const co2 = document.getElementById("co2Value");
    if (state.climate.co2 > 1000) {
        co2.classList.add("value--danger");
    } else {
        co2.classList.remove("value--danger");
    }
}

/******************************************************************************
 * SPARKLINES
 * 
 * @description Отрисовка мини-графиков (спарклайнов) с использованием SVG.
 *              Принцип работы:
 *              1. Находим минимальное и максимальное значения в данных
 *              2. Нормализуем каждое значение в диапазон [0, 40] (высота SVG)
 *              3. Формируем строку координат для полилинии
 * @note SVG-элемент уже должен существовать в DOM с viewBox="0 0 100 40"
 * @performance При большом количестве точек или частых обновлениях стоит 
 *              рассмотреть использование Canvas или мемоизацию.
 ******************************************************************************/

function drawSparkline(id, data) {
    const svg = document.getElementById(id);
    if (!svg) return;

    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1; // Защита от деления на ноль

    const points = data.map((value, index) => {
        const x = (index / (data.length - 1)) * 100; // X: 0-100
        const y = 40 - ((value - min) / range) * 35; // Y: 5-40 (оставляем отступы сверху/снизу)
        return `${x},${y}`;
    });

    svg.setAttribute("points", points.join(" "));
}

/******************************************************************************
 * HELPERS
 * 
 * @description Вспомогательные утилиты для работы с данными.
 *              Вынесены в отдельный блок для переиспользования и тестирования.
 ******************************************************************************/

/**
 * Добавляет новое значение в массив-историю, сохраняя заданную длину.
 * @param {Array} arr - Массив истории (мутируется)
 * @param {number} value - Новое значение
 * @example pushHistory([1,2,3], 4) => [2,3,4] (при HISTORY_LENGTH=3)
 */
function pushHistory(arr, value) {
    arr.push(value);
    if (arr.length > HISTORY_LENGTH) {
        arr.shift();
    }
}

/**
 * Генерирует случайное целое число в заданном диапазоне [min, max)
 * @param {number} min - Нижняя граница (включительно)
 * @param {number} max - Верхняя граница (исключительно)
 * @returns {number} Случайное целое число
 */
function randomInt(min, max) {
    return Math.floor(min + Math.random() * (max - min));
}

/**
 * Реализует случайное блуждание (Random Walk) для плавного изменения значения.
 * Шаг изменения (step) определяет максимальное отклонение за одну итерацию.
 * @param {number} value - Текущее значение (мутируется)
 * @param {number} min - Минимально допустимое значение (clamp)
 * @param {number} max - Максимально допустимое значение (clamp)
 * @param {number} step - Максимальный шаг изменения (в ту или иную сторону)
 * @returns {number} Новое значение
 * @note Функция чистая: возвращает новое значение, не изменяя входное
 */
function drift(value, min, max, step) {
    value += (Math.random() * 2 - 1) * step; // Случайное приращение в диапазоне [-step, step]
    if (value < min) value = min;
    if (value > max) value = max;
    return value;
}

/******************************************************************************
 * FUTURE WEBSOCKET API
 * 
 * @description Точка интеграции с реальным источником данных.
 *              При переходе на WebSocket или REST API, этот метод будет
 *              основным входом для обновления состояния.
 * @param {Object} payload - Объект с новыми данными (структура соответствует state)
 * @example updateTelemetry({ climate: { temperature: 23.5 } })
 * @todo Добавить валидацию входящих данных (проверка типов, диапазонов)
 * @todo Реализовать частичное обновление (merge глубокое, а не поверхностное)
 ******************************************************************************/

function updateTelemetry(payload) {
    // ВНИМАНИЕ: Object.assign делает поверхностное копирование.
    // Если payload содержит вложенные объекты (channels), они заменят существующие целиком.
    // Для глубокого слияния нужна рекурсивная функция или библиотека (например, Lodash.merge).
    if (payload.climate) {
        Object.assign(state.climate, payload.climate);
    }

    if (payload.power) {
        Object.assign(state.power, payload.power);
    }

    updateAll();
}