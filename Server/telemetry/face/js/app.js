/******************************************************************************
 * DOME TELEMETRY HUD
 * Version: 0.1
 ******************************************************************************/

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
 ******************************************************************************/

const HISTORY_LENGTH = 30;

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
 ******************************************************************************/

window.addEventListener("DOMContentLoaded", () => {

    initChannels();

    initHistory();

    buildPowerGrid();

    updateAll();

    setInterval(updateClock, 1000);

    setInterval(updateMissionTimer, 1000);

    setInterval(simulateTelemetry, 2000);
});

/******************************************************************************
 * CHANNELS
 ******************************************************************************/

function initChannels() {

    state.power.channels = CHANNEL_NAMES.map((name, index) => ({

        id: index + 1,

        name,

        enabled: Math.random() > 0.2,

        power:
            Math.floor(
                50 + Math.random() * 450
            )
    }));
}

/******************************************************************************
 * HISTORY
 ******************************************************************************/

function initHistory() {

    for (let i = 0; i < HISTORY_LENGTH; i++) {

        state.climate.temperatureHistory.push(
            22 + Math.random() * 3
        );

        state.climate.humidityHistory.push(
            40 + Math.random() * 15
        );

        state.climate.co2History.push(
            550 + Math.random() * 120
        );
    }
}

/******************************************************************************
 * POWER GRID
 ******************************************************************************/

function buildPowerGrid() {

    const container =
        document.getElementById("powerGrid");

    container.innerHTML = "";

    state.power.channels.forEach(channel => {

        const element =
            document.createElement("div");

        element.className = channel.enabled
            ? "hud-toggle hud-toggle--on"
            : "hud-toggle";

        element.dataset.id = channel.id;

        element.innerHTML = `
            <div class="hud-toggle__name">
                L${channel.id}
            </div>

            <div class="hud-toggle__power">
                ${channel.power} W
            </div>

            <div class="hud-toggle__state">
                ${channel.enabled ? "ON" : "OFF"}
            </div>
        `;

        element.addEventListener("click", () => {

            channel.enabled =
                !channel.enabled;

            if (!channel.enabled) {
                channel.power = 0;
            } else {
                channel.power =
                    randomInt(80, 500);
            }

            buildPowerGrid();
            updatePowerSummary();
        });

        container.appendChild(element);
    });
}

/******************************************************************************
 * CLOCK
 ******************************************************************************/

function updateClock() {

    const now = new Date();

    const time =
        now.toLocaleTimeString(
            "en-GB",
            {
                hour12: false
            }
        );

    document.getElementById("clock")
        .textContent = time;
}

/******************************************************************************
 * MISSION TIMER
 ******************************************************************************/

function updateMissionTimer() {

    const elapsed =
        Math.floor(
            (Date.now() -
             state.missionStart) / 1000
        );

    const hours =
        String(
            Math.floor(elapsed / 3600)
        ).padStart(2, "0");

    const minutes =
        String(
            Math.floor(
                (elapsed % 3600) / 60
            )
        ).padStart(2, "0");

    document
        .getElementById("missionTime")
        .textContent =
            `${hours}:${minutes}`;
}

/******************************************************************************
 * TELEMETRY SIMULATION
 ******************************************************************************/

function simulateTelemetry() {

    state.climate.temperature =
        drift(
            state.climate.temperature,
            18,
            28,
            0.4
        );

    state.climate.humidity =
        drift(
            state.climate.humidity,
            30,
            70,
            2
        );

    state.climate.co2 =
        drift(
            state.climate.co2,
            450,
            1200,
            20
        );

    pushHistory(
        state.climate.temperatureHistory,
        state.climate.temperature
    );

    pushHistory(
        state.climate.humidityHistory,
        state.climate.humidity
    );

    pushHistory(
        state.climate.co2History,
        state.climate.co2
    );

    state.power.channels.forEach(channel => {

        if (!channel.enabled) return;

        channel.power =
            drift(
                channel.power,
                50,
                500,
                30
            );
    });

    state.power.solarGeneration =
        drift(
            state.power.solarGeneration,
            0.2,
            5.5,
            0.3
        );

    state.power.battery =
        drift(
            state.power.battery,
            10,
            100,
            1
        );

    updateAll();
}

/******************************************************************************
 * UPDATE UI
 ******************************************************************************/

function updateAll() {

    updateClimate();

    updatePowerSummary();

    buildPowerGrid();
}

function updateClimate() {

    document.getElementById("tempValue")
        .textContent =
        `${state.climate.temperature.toFixed(1)}°C`;

    document.getElementById("humValue")
        .textContent =
        `${Math.round(state.climate.humidity)}%`;

    document.getElementById("co2Value")
        .textContent =
        `${Math.round(state.climate.co2)} ppm`;

    drawSparkline(
        "tempSpark",
        state.climate.temperatureHistory
    );

    drawSparkline(
        "humSpark",
        state.climate.humidityHistory
    );

    drawSparkline(
        "co2Spark",
        state.climate.co2History
    );

    checkClimateAlerts();
}

function updatePowerSummary() {

    const totalLoad =
        state.power.channels
            .filter(c => c.enabled)
            .reduce(
                (sum, c) =>
                    sum + c.power,
                0
            );

    document
        .getElementById("solarValue")
        .textContent =
            `${state.power.solarGeneration.toFixed(1)} kW`;

    document
        .getElementById("loadValue")
        .textContent =
            `${(totalLoad / 1000).toFixed(1)} kW`;

    document
        .getElementById("batteryValue")
        .textContent =
            `${Math.round(state.power.battery)}%`;
}

/******************************************************************************
 * ALERTS
 ******************************************************************************/

function checkClimateAlerts() {

    const co2 =
        document.getElementById("co2Value");

    if (state.climate.co2 > 1000) {

        co2.classList.add(
            "value--danger"
        );

    } else {

        co2.classList.remove(
            "value--danger"
        );
    }
}

/******************************************************************************
 * SPARKLINES
 ******************************************************************************/

function drawSparkline(id, data) {

    const svg =
        document.getElementById(id);

    if (!svg) return;

    const min =
        Math.min(...data);

    const max =
        Math.max(...data);

    const range =
        max - min || 1;

    const points =
        data.map((value, index) => {

            const x =
                (index /
                 (data.length - 1)) * 100;

            const y =
                40 -
                ((value - min) / range) * 35;

            return `${x},${y}`;
        });

    svg.setAttribute(
        "points",
        points.join(" ")
    );
}

/******************************************************************************
 * HELPERS
 ******************************************************************************/

function pushHistory(arr, value) {

    arr.push(value);

    if (arr.length > HISTORY_LENGTH) {

        arr.shift();
    }
}

function randomInt(min, max) {

    return Math.floor(
        min +
        Math.random() *
        (max - min)
    );
}

function drift(
    value,
    min,
    max,
    step
) {

    value +=
        (Math.random() * 2 - 1)
        * step;

    if (value < min) value = min;
    if (value > max) value = max;

    return value;
}

/******************************************************************************
 * FUTURE WEBSOCKET API
 ******************************************************************************/

function updateTelemetry(payload) {

    if (payload.climate) {

        Object.assign(
            state.climate,
            payload.climate
        );
    }

    if (payload.power) {

        Object.assign(
            state.power,
            payload.power
        );
    }

    updateAll();
}