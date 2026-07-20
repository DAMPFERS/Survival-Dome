/*******************************************************************************
 * DOME TELEMETRY HUD
 * Version: 0.2
 *
 * @description Главный модуль HUD-интерфейса для системы телеметрии купола.
 *              Отвечает за отображение климатических данных, состояния систем
 *              питания и управления каналами нагрузки.
 * @note Теперь работает с WebSocket-сервером для получения реальных данных.
 ******************************************************************************/

/**
 * Глобальный объект состояния.
 * @property {Object} climate - Показатели климата
 * @property {number} climate.temperature - Текущая температура (в °C)
 * @property {number} climate.humidity - Текущая влажность (в %)
 * @property {number} climate.co2 - Уровень CO2 (в ppm)
 * @property {number[]} climate.*History - История значений для спарклайнов
 * @property {Object} power - Секция питания
 * @property {number} power.solarGeneration - Мощность солнечных панелей (в kW)
 * @property {number} power.battery - Уровень заряда батареи (в %)
 * @property {Object[]} power.channels - Массив каналов нагрузки
 * @property {number} missionStart - Timestamp начала миссии (для таймера)
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

/*******************************************************************************
 * CONFIG
 *
 * @description Конфигурационные константы, вынесенные для легкой настройки
 *              без изменения логики. Являются точкой входа для настройки HUD.
 * @see HISTORY_LENGTH - влияет на количество точек на графиках
 * @see CHANNEL_NAMES - определяет имена и количество каналов нагрузки
 ******************************************************************************/

/** @constant {number} - Количество точек в истории для спарклайнов (30 ≈ 1 минута при обновлении раз в 2с) */
const HISTORY_LENGTH = 30;

/**
 * @constant {string[]} - Имена каналов нагрузки. Порядок важен: определяет ID каналов.
 * @note При добавлении/удалении каналов нужно обновить и серверную часть
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

/*******************************************************************************
 * WEBSOCKET CONNECTION
 *
 * @description Подключение к WebSocket-серверу для получения реальных данных.
 *              Сервер отправляет обновления телеметрии и подтверждения управления.
 ******************************************************************************/

let socket;

/**
 * Подключаемся к WebSocket-серверу.
 */
function connectWebSocket() {
    socket = new WebSocket("ws://localhost:8765");

    socket.onopen = () => {
        console.log("Connected to WebSocket server");
    };

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);

        if (data.type === "telemetry") {
            // Обновляем состояние из серверных данных
            if (data.data.climate) {
                Object.assign(state.climate, data.data.climate);
                // Обновляем историю для графиков
                pushHistory(state.climate.temperatureHistory, state.climate.temperature);
                pushHistory(state.climate.humidityHistory, state.climate.humidity);
                pushHistory(state.climate.co2History, state.climate.co2);
            }
            if (data.data.power) {
                Object.assign(state.power, data.data.power);
            }
            updateAll();
        }

        if (data.type === "control_response") {
            // Обновляем состояние канала после подтверждения от сервера
            if (data.success) {
                const channel = state.power.channels.find(ch => ch.id === data.channel_id);
                if (channel) {
                    channel.enabled = data.enabled;
                    channel.power = data.power;
                }
                buildPowerGrid(); // Обновляем UI каналов
                updatePowerSummary();
            }
        }
    };

    socket.onclose = () => {
        console.log("Disconnected from WebSocket server");
        // Попытка переподключения через 5 секунд
        setTimeout(connectWebSocket, 5000);
    };

    socket.onerror = (error) => {
        console.error("WebSocket error:", error);
    };
}

/*******************************************************************************
 * INIT
 *
 * @description Инициализация после загрузки DOM.
 ******************************************************************************/

window.addEventListener("DOMContentLoaded", () => {

    initChannels();        // Создаем каналы с начальными данными
    initHistory();         // Заполняем историю начальными данными

    buildPowerGrid();      // Рендерим сетку каналов в DOM

    updateAll();           // Первичный рендер всех виджетов

    // Подключаемся к WebSocket-серверу
    connectWebSocket();

    // Таймеры для обновления UI
    setInterval(updateClock, 1000);        // Обновляем часы каждую секунду
    setInterval(updateMissionTimer, 1000); // Обновляем таймер миссии каждую секунду
});

/*******************************************************************************
 * CHANNELS
 ******************************************************************************/

function initChannels() {

    state.power.channels = CHANNEL_NAMES.map((name, index) => ({
        id: index + 1,
        name,
        enabled: Math.random() > 0.2,
        power: Math.floor(50 + Math.random() * 450)
    }));
}

/*******************************************************************************
 * HISTORY
 ******************************************************************************/

function initHistory() {

    for (let i = 0; i < HISTORY_LENGTH; i++) {
        state.climate.temperatureHistory.push(22 + Math.random() * 3);
        state.climate.humidityHistory.push(40 + Math.random() * 15);
        state.climate.co2History.push(550 + Math.random() * 120);
    }
}

/*******************************************************************************
 * POWER GRID
 ******************************************************************************/

function buildPowerGrid() {

    const container = document.getElementById("powerGrid");
    container.innerHTML = "";

    state.power.channels.forEach(channel => {
        const element = document.createElement("div");
        element.className = channel.enabled
            ? "hud-toggle hud-toggle--on"
            : "hud-toggle";

        element.dataset.id = channel.id;

        element.innerHTML = `
            <div class="hud-toggle__name">L${channel.id}</div>
            <div class="hud-toggle__power">${channel.power.toFixed(1)} W</div>
            <div class="hud-toggle__state">${channel.enabled ? "ON" : "OFF"}</div>
        `;

        // Обработчик клика по каналу — отправляем команду на сервер
        element.addEventListener("click", () => {
            if (socket && socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({
                    type: "control",
                    action: "toggle",
                    channel_id: channel.id
                }));
            }
        });

        container.appendChild(element);
    });
}

/*******************************************************************************
 * CLOCK
 ******************************************************************************/

function updateClock() {
    const now = new Date();
    const time = now.toLocaleTimeString("en-GB", { hour12: false });
    document.getElementById("clock").textContent = time;
}

/*******************************************************************************
 * MISSION TIMER
 ******************************************************************************/

function updateMissionTimer() {
    const elapsed = Math.floor((Date.now() - state.missionStart) / 1000);
    const hours = String(Math.floor(elapsed / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");

    document.getElementById("missionTime").textContent = `${hours}:${minutes}`;
}

/*******************************************************************************
 * UPDATE UI
 ******************************************************************************/

function updateAll() {
    updateClimate();
    updatePowerSummary();
    buildPowerGrid();
}

function updateClimate() {
    document.getElementById("tempValue").textContent = `${state.climate.temperature.toFixed(1)}°C`;
    document.getElementById("humValue").textContent = `${Math.round(state.climate.humidity)}%`;
    document.getElementById("co2Value").textContent = `${Math.round(state.climate.co2)} ppm`;

    drawSparkline("tempSpark", state.climate.temperatureHistory);
    drawSparkline("humSpark", state.climate.humidityHistory);
    drawSparkline("co2Spark", state.climate.co2History);

    checkClimateAlerts();
}

function updatePowerSummary() {
    const totalLoad = state.power.channels
        .filter(c => c.enabled)
        .reduce((sum, c) => sum + c.power, 0);

    document.getElementById("solarValue").textContent = `${state.power.solarGeneration.toFixed(1)} kW`;
    document.getElementById("loadValue").textContent = `${(totalLoad / 1000).toFixed(1)} kW`;
    document.getElementById("batteryValue").textContent = `${Math.round(state.power.battery)}%`;
}

/*******************************************************************************
 * ALERTS
 ******************************************************************************/

function checkClimateAlerts() {
    const co2 = document.getElementById("co2Value");
    if (state.climate.co2 > 1000) {
        co2.classList.add("value--danger");
    } else {
        co2.classList.remove("value--danger");
    }
}

/*******************************************************************************
 * SPARKLINES
 ******************************************************************************/

function drawSparkline(id, data) {
    const svg = document.getElementById(id);
    if (!svg) return;

    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;

    const points = data.map((value, index) => {
        const x = (index / (data.length - 1)) * 100;
        const y = 40 - ((value - min) / range) * 35;
        return `${x},${y}`;
    });

    svg.setAttribute("points", points.join(" "));
}

/*******************************************************************************
 * HELPERS
 ******************************************************************************/

function pushHistory(arr, value) {
    arr.push(value);
    if (arr.length > HISTORY_LENGTH) {
        arr.shift();
    }
}

function randomInt(min, max) {
    return Math.floor(min + Math.random() * (max - min));
}

function drift(value, min, max, step) {
    value += (Math.random() * 2 - 1) * step;
    if (value < min) value = min;
    if (value > max) value = max;
    return value;
}