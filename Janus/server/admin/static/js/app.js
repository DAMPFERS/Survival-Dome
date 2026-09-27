// Админ-панель купола. Браузер общается только с мостом admin_panel.py (ws://<host>/ws):
// мост держит админское соединение с сервером купола и пересылает телеметрию и запросы.
'use strict';

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const REQUEST_TIMEOUT_MS = 12000;

const state = {
  ws: null, connected: false, bridge: {}, t: null, catalog: [], catalogById: {},
  crises: [], pending: new Map(), seq: 1, log: [], nodeCards: {}, tab: 'overview', policy: null,
};

// ---------------------------------------------------------------------------
// Соединение с мостом
// ---------------------------------------------------------------------------

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  state.ws = ws;
  ws.onmessage = ev => onMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    setLive(false, 'нет связи с админ-панелью');
    state.bridge.connected = false;
    for (const [, p] of state.pending) p.reject(new Error('Соединение с панелью закрыто'));
    state.pending.clear();
    setTimeout(connect, 2000);
  };
}

function onMessage(msg) {
  if (msg.type === 'bridge_status') {
    const was = state.bridge.connected;
    state.bridge = msg;
    setLive(msg.connected, msg.connected ? `сервер ${msg.server}` : (msg.error || 'нет связи с сервером'));
    if (msg.connected && !was) loadCatalogs();
    return;
  }
  if (msg.type === 'telemetry') {
    state.t = msg.data || {};
    state.serverTs = msg.timestamp;   // часы симуляции сервера — по ним же ставится updated_at
    state.lastTelemetry = Date.now();
    render();
    return;
  }
  if (msg.type === 'session_revoked') { toast(`Сессия отозвана: ${msg.reason}`, true); return; }
  const p = state.pending.get(msg.request_id);
  if (p) {
    state.pending.delete(msg.request_id);
    clearTimeout(p.timer);
    p.resolve(msg);
  }
}

function setLive(ok, text) {
  $('live').classList.toggle('off', !ok);
  $('upd').textContent = text;
}

function request(payload, {quiet = false, title = null} = {}) {
  return new Promise((resolve, reject) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) { reject(new Error('Нет связи с админ-панелью')); return; }
    const request_id = `b${state.seq++}`;
    const timer = setTimeout(() => {
      state.pending.delete(request_id);
      reject(new Error('Сервер не ответил'));
    }, REQUEST_TIMEOUT_MS);
    state.pending.set(request_id, {resolve, reject, timer});
    state.ws.send(JSON.stringify({...payload, request_id}));
  }).then(resp => {
    if (!quiet || !resp.success) addLog(title || describe(payload), payload, resp);
    if (!resp.success && !quiet) toast(`${title || describe(payload)}: ${resp.error}`, true);
    return resp;
  }).catch(err => {
    addLog(title || describe(payload), payload, {success: false, error: err.message});
    toast(err.message, true);
    return {success: false, error: err.message};
  });
}

function describe(p) {
  if (p.type === 'control') return `${p.node_id}.${p.action}(${p.value === undefined ? '' : short(p.value, 60)})`;
  return p.type;
}

async function control(node_id, action, value) {
  const payload = {type: 'control', node_id, action};
  if (value !== undefined && value !== null && value !== '') payload.value = value;
  const resp = await request(payload);
  if (resp.success) toast(`✓ ${describe(payload)} [${resp.mode || 'virtual'}]`);
  return resp;
}

async function loadCatalogs() {
  const [nodes, crises] = await Promise.all([
    request({type: 'describe_nodes'}, {quiet: true}),
    request({type: 'list_crises'}, {quiet: true}),
  ]);
  if (nodes.success) {
    state.catalog = nodes.nodes;
    state.catalogById = Object.fromEntries(nodes.nodes.map(n => [n.node_id, n]));
    fillPolicyNodes();
  }
  if (crises.success) state.crises = crises.crises.slice().sort((a, b) =>
    (a.name[0] === 'k') - (b.name[0] === 'k') || a.name.localeCompare(b.name));
  renderCatalog();
  render();
}

// ---------------------------------------------------------------------------
// Форматирование
// ---------------------------------------------------------------------------

function fmt(v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'boolean') return v ? '✓ да' : '✗ нет';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
  if (typeof v === 'object') return short(v, 160);
  return String(v);
}
function short(v, n) {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function hms(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
function gameClock(g) {
  if (!g || g.hour === undefined) return '—';
  const h = Math.floor(g.hour), m = Math.floor((g.hour - h) * 60);
  return `д${g.day} ${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}
function badge(text, cls = '') { return `<span class="badge ${cls}">${esc(text)}</span>`; }
const stateClass = s => ({ON: 'on', OK: 'good', ARMED: 'good', READY: 'good', RUNNING: 'on', PRINTING: 'on',
  AVAILABLE: 'good', UP: 'good', RESOLVED: 'good', TRACKING: 'good', NORMAL: 'good', ONLINE: 'good',
  OFF: 'mute', IDLE: 'mute', EXPIRED: 'mute', PENDING: 'mute', WAITING: 'mute', MANUAL: 'warn', PAUSED: 'warn',
  BLOCKED: 'warn', LIMITED: 'warn', UNSTABLE: 'warn', DEGRADED: 'warn', CONTAMINATED: 'warn', WATER: 'warn',
  TRIPPED: 'bad', RCD_TRIP: 'bad', FAULT: 'bad', ALARM: 'bad', ERROR: 'bad', FAILED: 'bad', UNKNOWN: 'bad',
  DOWN: 'bad', UNAVAILABLE: 'bad', OFFLINE: 'bad', NOT_STARTED: 'mute'}[s] || '');
const sb = s => badge(s ?? '—', stateClass(s));

function parseValue(text) {
  text = (text || '').trim();
  if (text === '') return undefined;
  try { return JSON.parse(text); } catch { return text; }
}
function parseJsonField(el) {
  const text = el.value.trim();
  if (!text) return {};
  try { return JSON.parse(text); } catch { toast('Параметры: некорректный JSON', true); return null; }
}

// ---------------------------------------------------------------------------
// Отрисовка
// ---------------------------------------------------------------------------

const T = () => state.t || {};
const truth = id => (T().true_nodes || {})[id] || {};
const agent = id => (T().nodes || {})[id] || {};

function render() {
  const t = T();
  $('gameTime').textContent = gameClock(t.game_time);
  $('simTime').textContent = `${hms(t.sim_time)} ×${t.time_scale ?? '—'}${t.paused ? ' ⏸' : ''}`;
  $('activeTeam').textContent = t.active_team ?? '—';
  $('crisisCount').textContent = (t.active_crises || []).length || '';
  if (state.bridge.connected) setLive(true, `обновлено ${new Date().toLocaleTimeString('ru-RU')}`);
  if (state.tab === 'overview') renderOverview();
  if (state.tab === 'nodes') renderNodes();
  if (state.tab === 'crises') renderCrises();
  if (state.tab === 'env') renderEnv();
}

function kvs(el, pairs) {
  $(el).innerHTML = pairs.map(([k, v]) => `<span>${esc(k)}</span><b>${v}</b>`).join('');
}
function tile(label, value, cls = '') { return `<div class="tile ${cls}"><span>${esc(label)}</span><b>${value}</b></div>`; }

function renderOverview() {
  const t = T(), inv = truth('solar_inverter_01'), bat = truth('battery_01'), dz = truth('dizel_1'),
    tank = truth('fuel_tank_01'), aut = truth('dome_automation_01'), panels = truth('solar_panels_01'),
    wind = truth('wind_turbine_01');
  kvs('timeInfo', [
    ['Время купола', gameClock(t.game_time)], ['Время симуляции', hms(t.sim_time)],
    ['Скорость', `×${t.time_scale ?? '—'}`], ['Пауза', t.paused ? badge('ПАУЗА', 'warn') : badge('идёт', 'good')],
    ['Энергосистема', esc(t.energy_mode || '—')], ['Активная команда', t.active_team ?? '—'],
    ['Смена', t.shift ? `${hms(t.shift.t_s)} ${t.shift.finished ? badge('завершена', 'mute') : badge('идёт', 'on')}` : '—'],
  ]);
  const soc = inv.battery_soc_pct;
  $('energyTiles').innerHTML =
    tile('PV, Вт', fmt(inv.pv_power_w), 'good') + tile('Нагрузка, Вт', fmt(inv.load_power_w)) +
    tile('SOC, %', fmt(soc), soc < 25 ? 'bad' : soc < 40 ? 'warn' : '') +
    tile('Инвертор, °C', fmt(inv.inverter_temp_c), inv.inverter_temp_c > 75 ? 'bad' : inv.inverter_temp_c > 65 ? 'warn' : '');
  kvs('energyInfo', [
    ['Режим инвертора', esc(inv.mode ?? '—')], ['Ошибка', inv.error_code ? badge(inv.error_code, 'bad') : '—'],
    ['Выход, В / предел', `${fmt(inv.output_voltage_v)} / ${fmt(inv.output_limit_pct)} %`],
    ['Сеть', sb(inv.grid_state)], ['AC-вход', esc(inv.ac_source ?? '—')], ['MPPT', sb(inv.mppt_state)],
    ['Вентилятор', `${sb(inv.fan_state)} ${fmt(inv.fan_current_a)} А`], ['Балансировка', sb(inv.balancing_state)],
    ['Дефицит мощности, Вт', fmt(inv.power_deficit_w)],
    ['АКБ: температура / заряд', `${fmt(bat.temperature_c)} °C / ${bat.charge_allowed ? 'разрешён' : badge('ЗАПРЕЩЁН', 'bad')}`],
    ['Панели / ветер, Вт', `${fmt(panels.power_w)} / ${fmt(wind.power_w)}`],
    ['Дизель', `${sb(dz.state)} ${dz.fault_reason ? esc(dz.fault_reason) : ''} ${fmt(dz.power_w)} Вт`],
    ['Топливо', `${fmt(tank.volume_l)} л · ${sb(tank.fuel_quality)} · вода ${fmt(tank.water_content_ppm)} ppm`],
    ['Прогноз автономности', aut.autonomy_forecast_h == null ? '—' : `${aut.autonomy_forecast_h} ч`],
  ]);
  renderShiftMetrics();
  renderPanelLines();
  renderInterlocks();
  renderAutoEvents();
  $('activeCrisesOverview').innerHTML = activeCrisesHtml();
}

function renderShiftMetrics() {
  const s = T().shift;
  if (!s) { $('shiftMetrics').innerHTML = '<span class="mut">Смена не запущена</span><b></b>'; return; }
  const m = s.metrics || {};
  kvs('shiftMetrics', [
    ['Идёт', hms(s.t_s)], ['Простой ЧПУ', hms(m.cnc_downtime_s)], ['Простой принтера', hms(m.printer_downtime_s)],
    ['Простой рабочего места', hms(m.workstation_downtime_s)], ['Пайка без вытяжки', hms(m.soldering_without_extraction_s)],
    ['CO₂ > 1500', hms(m.co2_over_1500_s)], ['VOC > 250', hms(m.voc_over_250_s)],
    ['Блэкауты', `${m.blackouts ?? 0} (${hms(m.blackout_s)})`], ['Ошибки инвертора', esc(JSON.stringify(m.inverter_errors || {}))],
    ['Сбросы нагрузки', m.load_shed_trips ?? 0], ['Пуски пожаротушения', esc((m.fire_suppression_starts || []).join(', ') || '—')],
    ['Брак деталей', esc((m.part_defects || []).join(', ') || '—')],
  ]);
}

function renderPanelLines() {
  const p = truth('smart_panel_01'), a = agent('smart_panel_01');
  const lines = p.lines || [];
  const max = 1000;
  $('panelLines').innerHTML = `<table><thead><tr><th>#</th><th>Линия</th><th>Состояние</th><th>Мощность</th><th></th>
    <th>Ток, А</th><th>U, В</th><th>Утечка, мА</th><th>Защита</th><th>Управление</th></tr></thead><tbody>${
    lines.map((l, i) => {
      const al = (a.lines || [])[i] || {};
      const distorted = ['power_w', 'current_a', 'voltage_v'].filter(k => al[k] !== undefined && al[k] !== l[k]);
      const d = k => distorted.includes(k) ? ` <small class="mut">агент: ${fmt(al[k])}</small>` : '';
      return `<tr><td>${l.line}</td><td>${esc(l.name)}</td><td>${sb(l.state)}</td>
      <td class="num">${fmt(l.power_w)}${d('power_w')}</td><td><div class="track"><div style="width:${Math.min(100, l.power_w / max * 100)}%"></div></div></td>
      <td class="num">${fmt(l.current_a)}${d('current_a')}</td><td class="num">${fmt(l.voltage_v)}${d('voltage_v')}</td>
      <td class="num">${fmt(l.leakage_ma)}</td><td>${sb(l.protection_state)} ${l.alarm_code ? esc(l.alarm_code) : ''}</td>
      <td class="acts"><button class="small" data-line="${l.line}" data-act="line_on">Вкл</button><button class="small ghost" data-line="${l.line}" data-act="line_off">Выкл</button><button class="small ghost" data-line="${l.line}" data-act="reset_protection">Взвести</button><button class="small ghost" data-line="${l.line}" data-act="emulate_protection_trip">Срабатывание</button></td></tr>`;
    }).join('')}</tbody></table>
    <div class="kvs" style="margin-top:8px"><span>Шина</span><b>${p.bus_powered ? badge('под напряжением', 'good') : badge('ОБЕСТОЧЕНА', 'bad')} ${fmt(p.bus_voltage_v)} В · ${fmt(p.total_power_w)} Вт</b>
    <span>Рабочее место Оператора (линия 1)</span><b>${esc(p.control_workstation ?? '—')}</b></div>`;
}

function renderInterlocks() {
  const a = truth('dome_automation_01');
  const ils = a.interlocks || {};
  $('autonomy').textContent = `основной датчик CO₂: ${a.primary_co2_sensor ?? '—'} · вентиляция по CO₂: ${a.ventilation_auto ? 'вкл' : 'выкл'}`;
  const trust = Object.entries(a.data_trust || {});
  $('interlocks').innerHTML = `<table><thead><tr><th>Интерлок</th><th>Условие / действие</th><th>Состояние</th><th>Срабатываний</th>
    <th>Задержка / блок</th><th>Отключил линии</th><th>Управление</th></tr></thead><tbody>${
    Object.entries(ils).map(([id, st]) => `<tr><td><b>${id}</b></td><td>${esc(st.title || '')}</td>
      <td>${sb(st.state)} ${st.state_detail ? badge(st.state_detail, 'warn') : ''}${st.zones ? ' ' + esc(Object.keys(st.zones).join(',')) : ''}</td>
      <td class="num">${st.trips ?? 0}</td>
      <td class="num">${st.state === 'BLOCKED' ? hms(st.blocked_remaining_s) : fmt(st.pending_s)}</td>
      <td>${esc((st.shed_lines || []).join(', ') || '—')}</td>
      <td class="acts"><input type="number" min="1" max="30" value="10" id="blk-${id}" title="минуты">
        <button class="small ghost" data-il="${id}" data-act="block_interlock">Блок</button>
        <button class="small ghost" data-il="${id}" data-act="unblock_interlock">Разблок</button>
        <button class="small" data-il="${id}" data-act="reset_interlock">Снять</button></td></tr>`).join('')}
    </tbody></table>
    <h3>Доверие к данным</h3>${trust.length ? `<table><tbody>${trust.map(([k, v]) => {
      const [node, ...param] = k.split('.');
      return `<tr><td>${esc(k)}</td><td>${sb(v === 'FAILED' ? 'FAULT' : 'DEGRADED')} ${esc(v)}</td><td class="acts">
      <button class="small ghost" data-trust-node="${esc(node)}" data-trust-param="${esc(param.join('.'))}">Вернуть доверие</button></td></tr>`;
    }).join('')}</tbody></table>` : '<span class="mut">Все источники доверенные</span>'}`;
}

function renderAutoEvents() {
  const events = (truth('dome_automation_01').events || []).slice(-30).reverse();
  $('autoEvents').innerHTML = events.map(e => `<div class="ev ${e.type === 'TRIP' ? 'bad' : ''}">
    <time>${hms(e.t_s)}</time><span>${esc(e.interlock)} ${sb(e.type === 'TRIP' ? 'ALARM' : e.type === 'CLEAR' ? 'OK' : 'IDLE')}</span>
    <span>${esc(e.message)}</span><span>${e.acknowledged ? '<span class="mut">квитировано</span>' :
      `<button class="small ghost" data-ack="${e.event_id}">Квитировать</button>`}</span></div>`).join('') || '<span class="mut">Событий нет</span>';
}

function activeCrisesHtml() {
  const active = T().active_crises || [];
  if (!active.length) return '<span class="mut">Нет активных кризисов</span>';
  const reports = Object.fromEntries((T().crisis_reports || []).map(r => [r.name, r]));
  return `<table><thead><tr><th>Кризис</th><th>Класс</th><th>Идёт</th><th>Статус</th><th>Реакция агента</th><th>Заметки</th><th></th></tr></thead><tbody>${
    active.map(name => {
      const r = reports[name] || {};
      const now = (T().environment || {}).sim_time_s || 0;
      const running = r.started_s != null ? hms(now - r.started_s) : 'ожидает';
      return `<tr><td><b>${esc(r.code || '')}</b> ${esc(r.title || name)}</td><td>${esc(r.class || '')} ${esc(r.type || '')}</td>
        <td>${running}</td><td>${sb(r.outcome)}</td><td>${r.reaction_time_s == null ? '—' : r.reaction_time_s + ' с'}</td>
        <td>${esc((r.notes || []).slice(-2).join('; '))}</td>
        <td class="acts"><button class="small danger" data-stop="${esc(name)}">Остановить</button></td></tr>`;
    }).join('')}</tbody></table>`;
}

// ---------------- узлы ----------------

const HINTS = {
  line_on: ['1', 'номер линии 1–8'], line_off: ['8', 'номер линии 1–8'], reset_protection: ['1', 'номер линии'],
  reset_line_error: ['1', 'номер линии'], emulate_protection_trip: ['{"line": 1, "type": "RCD_TRIP"}', 'TRIPPED | RCD_TRIP'],
  inspect_line: ['2', 'номер линии (Оператор)'], set_workstation: ['SOLDERING', 'IDLE | SOLDERING | HOT_AIR | OFF'],
  set_mode: ['HYBRID', 'инвертор: HYBRID | GRID_ONLY | BATTERY_ONLY; вентиляция: RECIRCULATION | FRESH_AIR | MIXED'],
  set_fan: ['AUTO', 'AUTO | ON | OFF'], start_cell_balancing: ['', 'минуты (по умолчанию 45)'],
  start_job: ['{"file_name": "part.gcode", "duration_s": 3600, "filament_g": 50}', 'имя файла или объект'],
  send_gcode: ['G28', 'строка G-code'], set_speed: ['60', '%'], set_power_limit: ['60', '%'],
  set_data_trust: ['{"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"}', 'TRUSTED | UNRELIABLE | FAILED; param "voltage_v" + "line": 2'],
  block_interlock: ['{"id": "IL_SMOKE", "minutes": 10}', 'не больше 30 минут'], unblock_interlock: ['IL_SMOKE', ''],
  reset_interlock: ['IL_CO2', 'снять сработавший интерлок'], acknowledge: ['all', 'event_id или all'],
  set_primary_sensor: ['climate_sensor_02', 'climate_sensor_01..03'], set_ventilation_auto: ['true', ''],
  set_diesel_autostart: ['true', ''], switch_path: ['RESERVE', 'MAIN | RESERVE (Оператор)'],
  restart_service: ['telemetry_collector', 'agent_runtime | telemetry_collector | vision_inference | api_gateway'],
  limit_load: ['50', '%'], set_poll_interval: ['500', 'мс'], set_frequency: ['433.5', 'МГц'],
  set_protocol: ['LORA', 'радио: AX25 | FSK | LORA | DMR'], switch_channel: ['MESH', 'SATELLITE | LORA | MESH'],
  set_traffic_priority: ['EMERGENCY', 'NORMAL | TELEMETRY | EMERGENCY'], manual_start_zone: ['STORAGE', 'FABLAB | LIVING | STORAGE | POWER_ROOM'],
  block_automation: ['true', ''], seal_zone: ['MAIN', 'MAIN | FABLAB | LIVING | STORAGE'],
  lock_door: ['STORAGE', 'MAIN_AIRLOCK | FABLAB | STORAGE | SERVER_ROOM'], unlock_door: ['FABLAB', ''],
  lockdown: ['true', ''], transfer_fuel: ['', 'литры (по умолчанию — до полного)'], set_threshold: ['0.5', 'мм/с'],
  set_thresholds: ['{"warning": 5, "alarm": 20}', ''], set_fan_pwm: ['{"fan": 0, "pwm": 200}', 'fan 0–5, pwm 0–255'],
  enable_mppt: ['2', 'номер канала (панели) / без значения (инвертор)'], disable_mppt: ['2', ''],
};

function renderNodes() {
  const t = T(), filter = $('nodeFilter').value.trim().toLowerCase();
  const ids = Object.keys(t.true_nodes || t.nodes || {});
  const grid = $('nodesGrid');
  for (const id of ids) {
    if (!state.nodeCards[id]) state.nodeCards[id] = createNodeCard(id);
    const card = state.nodeCards[id];
    if (!card.el.isConnected) grid.appendChild(card.el);
    const info = state.catalogById[id] || {};
    const hay = `${id} ${info.title || ''} ${info.system || ''}`.toLowerCase();
    card.el.hidden = !!filter && !hay.includes(filter);
    if (!card.el.hidden) updateNodeCard(id, card);
  }
}

function createNodeCard(id) {
  const el = document.createElement('article');
  el.className = 'card';
  el.innerHTML = `<div class="nodehead"><div class="nt"><b></b><span></span></div><div class="nb"></div></div>
    <div class="nkv"></div>
    <div class="ctl"><select></select><input type="text" placeholder="значение"><button>Выполнить</button><div class="hint"></div></div>`;
  const card = {el, head: el.querySelector('.nt'), badges: el.querySelector('.nb'), kv: el.querySelector('.nkv'),
    select: el.querySelector('select'), input: el.querySelector('input'), hint: el.querySelector('.hint'), actionsKey: ''};
  card.select.addEventListener('change', () => applyHint(card));
  el.querySelector('.ctl button').addEventListener('click', () => {
    const action = card.select.value;
    if (action) control(id, action, parseValue(card.input.value));
  });
  card.input.addEventListener('keydown', e => { if (e.key === 'Enter') el.querySelector('.ctl button').click(); });
  return card;
}

function applyHint(card) {
  const [example, hint] = HINTS[card.select.value] || ['', ''];
  card.input.placeholder = example || 'значение (необязательно)';
  const op = card.select.selectedOptions[0]?.dataset.op;
  card.hint.textContent = [hint, op ? 'физическое действие Оператора' : ''].filter(Boolean).join(' · ');
}

function updateNodeCard(id, card) {
  const info = state.catalogById[id] || {};
  const t = T(), view = (t.nodes || {})[id], tr = (t.true_nodes || {})[id] || {};
  const mode = (t.node_modes || {})[id] || 'virtual';
  card.head.querySelector('b').textContent = info.title || id;
  card.head.querySelector('span').textContent = `${id} · ${info.system || ''}`;
  const status = view ? view.status : undefined;
  card.el.classList.toggle('frozen', status === 'UNKNOWN');
  card.el.classList.toggle('alarm', !!(tr.alarm || tr.error || tr.alarm_state === 'ALARM' || tr.state === 'FAULT'));
  const real = info.real_device || (t.real_devices || {})[id];
  card.badges.innerHTML = `${badge(mode, mode === 'real' ? 'real' : 'mute')} ${status ? sb(status) : ''}
    ${real ? `<button class="small ghost" data-switch="${id}" data-mode="${mode === 'real' ? 'virtual' : 'real'}">→ ${mode === 'real' ? 'virtual' : 'real'}</button>` : ''}`;

  const actions = [...(info.controls || []), ...(info.real_only_controls || [])];
  const key = actions.join(',') + '|' + (info.operator_controls || []).join(',');
  if (key !== card.actionsKey) {
    card.actionsKey = key;
    const ops = new Set(info.operator_controls || []);
    card.select.innerHTML = '<option value="">— действие —</option>' +
      actions.map(a => `<option value="${a}" ${ops.has(a) ? 'data-op="1"' : ''}>${a}${ops.has(a) ? ' 👷' : ''}</option>`).join('') +
      (info.service_actions || []).map(a => `<option value="${a}">${a} (служебное)</option>`).join('');
    applyHint(card);
  }

  const showTruth = $('showTruth').checked, showHidden = $('showHidden').checked;
  const keys = [...new Set([...Object.keys(view || {}), ...Object.keys(tr)])]
    .filter(k => k !== 'control_override_source' && (showHidden || !k.startsWith('control_')));
  card.kv.innerHTML = keys.map(k => {
    const inView = view && k in view;
    const v = inView ? view[k] : tr[k];
    const differs = showTruth && inView && k in tr && JSON.stringify(view[k]) !== JSON.stringify(tr[k])
      && k !== 'updated_at' && k !== 'status';
    const cls = differs ? 'dist' : (v === null ? 'null' : '');
    const age = state.serverTs && typeof v === 'number' ? Math.round(state.serverTs - v) : null;
    const label = k === 'updated_at' ? `${new Date(v * 1000).toLocaleTimeString('ru-RU')} (${age == null ? '?' : age + ' с назад'})` : fmt(v);
    return `<span>${esc(k)}${inView ? '' : ' 🔒'}</span><b class="${cls}" title="${esc(JSON.stringify(v))}">${esc(label)}${
      differs ? `<small>истина: ${esc(fmt(tr[k]))}</small>` : ''}</b>`;
  }).join('');
}

// ---------------- кризисы ----------------

function renderCatalog() {
  const filter = $('crisisFilter').value.trim().toLowerCase();
  const active = new Set(T().active_crises || []);
  $('crisisCatalog').innerHTML = state.crises
    .filter(c => !filter || `${c.name} ${c.code} ${c.title} ${c.class}`.toLowerCase().includes(filter))
    .map(c => `<div class="crisis ${active.has(c.name) ? 'active' : ''}"><div class="ct"><b>${esc(c.code || '')} ${esc(c.title || c.name)}</b>
      <span>${esc(c.name)} · ${esc(c.class || '')} ${esc(c.type || '')}${c.duration_s ? ` · ${Math.round(c.duration_s / 60)} мин` : ''}</span></div>
      ${active.has(c.name) ? `<button class="small danger" data-stop="${esc(c.name)}">Стоп</button>`
        : `<button class="small" data-trigger="${esc(c.name)}">Запуск</button>`}</div>`).join('');
}

function renderCrises() {
  $('activeCrises').innerHTML = activeCrisesHtml();
  renderCatalog();
  const reports = (T().crisis_reports || []).slice().reverse();
  $('crisisReports').innerHTML = reports.length ? `<table><thead><tr><th>Кризис</th><th>Класс</th><th>Старт</th><th>Конец</th>
    <th>Исход</th><th>Реакция</th><th>Заметки</th><th>Штрафы</th><th>Оператор</th></tr></thead><tbody>${
    reports.map(r => `<tr><td><b>${esc(r.code)}</b> ${esc(r.title)}</td><td>${esc(r.class)} ${esc(r.type)}</td>
      <td>${r.started_s == null ? '—' : hms(r.started_s)}</td><td>${r.ended_s == null ? '—' : hms(r.ended_s)}</td>
      <td>${sb(r.outcome)}</td><td>${r.reaction_time_s == null ? '—' : r.reaction_time_s + ' с'}</td>
      <td>${esc((r.notes || []).join('; '))}</td><td>${esc((r.penalties || []).join('; '))}</td>
      <td>${esc((r.operator_actions || []).join(', '))}</td></tr>`).join('')}</tbody></table>` : '<span class="mut">Отчётов пока нет</span>';
  const layer = T().telemetry_layer || {};
  const d = layer.distortions || [];
  const fr = Object.entries(layer.freezes || {});
  $('telemetryLayer').innerHTML = `${layer.link_down ? badge('ОСНОВНОЙ КАНАЛ КУПОЛА НЕДОСТУПЕН', 'bad') : ''}
    ${d.length ? `<table><thead><tr><th>Узел.параметр</th><th>Режим</th><th>Параметры</th><th>Кризис</th></tr></thead><tbody>${
      d.map(x => `<tr><td>${esc(x.node_id)}.${esc(x.path)}</td><td>${esc(x.mode)}</td><td>${esc(short(x.params, 120))}</td><td>${esc(x.owner)}</td></tr>`).join('')
    }</tbody></table>` : '<span class="mut">Подмен нет</span>'}
    <h3>Заморозки</h3>${fr.length ? fr.map(([n, o]) => `<div>${esc(n)} ← ${esc(o.join(', '))}</div>`).join('') : '<span class="mut">Нет</span>'}`;
}

// ---------------- окружение ----------------

function renderEnv() {
  const t = T(), env = t.environment || {};
  const card = (title, obj) => `<article class="card"><h2>${esc(title)}</h2><div class="kvs">${
    Object.entries(obj || {}).map(([k, v]) => `<span>${esc(k)}</span><b>${esc(fmt(v))}</b>`).join('') || '<span class="mut">—</span>'}</div></article>`;
  const zones = env.zones || {};
  $('envGrid').innerHTML = card('Снаружи', env.outdoor) + card('Воздух купола (FabLab)', env.indoor) +
    Object.entries(zones).map(([z, v]) => card(`Зона ${z}`, v)).join('') + card('Внешние угрозы и сеть', env.external) +
    card('Драйверы физики воздуха', env.drivers) + card('Подменено кризисами', {...(env.forced || {}), ...Object.fromEntries(
      Object.entries(env.offsets || {}).map(([k, v]) => [`${k} (смещение)`, v]))}) +
    card('Реальный инвертор (показания)', t.real_inverter) + card('Режимы узлов', t.node_modes) +
    card('Реальные устройства', Object.fromEntries(Object.entries(t.real_devices || {}).map(([k, v]) => [k, short(v, 80)]))) +
    card('Время', {game_time_s: env.game_time_s, sim_time_s: env.sim_time_s, time_factor: env.time_factor,
      device_time_factor: env.device_time_factor});
}

// ---------------- доступ ----------------

function fillPolicyNodes() {
  $('policyNode').innerHTML = state.catalog.map(n => `<option value="${n.node_id}">${n.node_id}</option>`).join('');
}

// ---------------- журнал ----------------

function addLog(title, payload, resp) {
  state.log.unshift({t: new Date(), title, payload, resp});
  if (state.log.length > 300) state.log.pop();
  $('logCount').textContent = state.log.length || '';
  if (state.tab === 'log') renderLog();
}
function renderLog() {
  $('log').innerHTML = state.log.map(e => `<div class="ev ${e.resp.success ? 'ok' : 'bad'}">
    <time>${e.t.toLocaleTimeString('ru-RU')}</time><span>${esc(e.title)}</span>
    <code>${esc(e.resp.success ? short(stripMeta(e.resp), 400) : e.resp.error)}</code>
    <span>${e.resp.success ? badge('OK', 'good') : badge('ошибка', 'bad')}</span></div>`).join('') || '<span class="mut">Пусто</span>';
}
function stripMeta(r) { const {type, request_id, success, ...rest} = r; return rest; }

// ---------------- модальное окно и уведомления ----------------

function showModal(title, obj) {
  $('modalTitle').textContent = title;
  $('modalBody').textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
  $('modal').hidden = false;
}
let toastTimer;
function toast(text, bad = false) {
  const el = $('toast');
  el.textContent = text;
  el.className = `toast show${bad ? ' bad' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ---------------------------------------------------------------------------
// Обработчики
// ---------------------------------------------------------------------------

document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === `page-${btn.dataset.tab}`));
  state.tab = btn.dataset.tab;
  if (state.tab === 'log') renderLog(); else render();
}));

document.body.addEventListener('click', async e => {
  const b = e.target.closest('button');
  if (!b) return;
  const d = b.dataset;
  if (d.line) {
    const value = d.act === 'emulate_protection_trip' ? {line: +d.line, type: 'TRIPPED'} : +d.line;
    await control('smart_panel_01', d.act, value);
  } else if (d.il) {
    const value = d.act === 'block_interlock' ? {id: d.il, minutes: +($(`blk-${d.il}`).value || 10)} : d.il;
    await control('dome_automation_01', d.act, value);
  } else if (d.trustNode) {
    await control('dome_automation_01', 'set_data_trust', {node: d.trustNode, param: d.trustParam, trust: 'TRUSTED'});
  } else if (d.ack) {
    await control('dome_automation_01', 'acknowledge', {event_id: d.ack});
  } else if (d.stop) {
    const r = await request({type: 'stop_crisis', crisis_name: d.stop});
    if (r.success) toast(`Кризис ${d.stop} остановлен`);
  } else if (d.trigger) {
    const params = parseJsonField($('crisisParams'));
    if (params === null) return;
    const r = await request({type: 'trigger_crisis', crisis_name: d.trigger, params});
    if (r.success) toast(`Кризис ${d.trigger} запущен`);
  } else if (d.switch) {
    const r = await request({type: 'switch_mode', node_id: d.switch, mode: d.mode});
    if (r.success) { toast(`${d.switch} → ${d.mode}`); loadCatalogs(); }
  }
});

$('btnPause').onclick = () => request({type: 'time_control', action: 'pause'});
$('btnResume').onclick = () => request({type: 'time_control', action: 'resume'});
$('btnScale').onclick = () => request({type: 'time_control', action: 'set_scale', value: +$('scaleInput').value});
$('btnTeam').onclick = async () => {
  const r = await request({type: 'set_active_team', team: +$('teamSelect').value});
  if (r.success) toast(`Активная команда: ${r.team}, отключено сессий: ${r.revoked_sessions}`);
};
$('btnStartShift').onclick = async () => {
  const params = parseJsonField($('shiftParams'));
  if (params === null) return;
  params.simulate_operator = $('simOperator').checked;
  if (!confirm('Сбросить мир купола и начать 90-минутную смену? Текущие кризисы будут остановлены.')) return;
  const r = await request({type: 'start_shift', params});
  if (r.success) toast('Смена запущена');
};
$('btnShiftReport').onclick = async () => {
  const r = await request({type: 'shift_report'}, {quiet: true});
  if (r.success) showModal('Отчёт смены', r.report);
};
$('btnAckAll').onclick = () => control('dome_automation_01', 'acknowledge', 'all');
$('nodeFilter').oninput = renderNodes;
$('showTruth').onchange = renderNodes;
$('showHidden').onchange = renderNodes;
$('crisisFilter').oninput = renderCatalog;
$('btnGetPolicy').onclick = async () => {
  const r = await request({type: 'get_policy'}, {quiet: true});
  if (r.success) { state.policy = r.policy; $('policyView').textContent = JSON.stringify(r.policy, null, 2); fillPolicyForm(); }
};
$('btnReloadPolicy').onclick = async () => { const r = await request({type: 'reload_policy'}); if (r.success) $('btnGetPolicy').click(); };
$('policyNode').onchange = fillPolicyForm;
function fillPolicyForm() {
  const nodes = state.policy?.participant?.nodes || state.policy?.nodes || {};
  const rule = nodes[$('policyNode').value] || {};
  const actions = rule.actions;
  $('policyActions').value = actions === '*' ? '*' : (actions || []).join(', ');
  $('policyVisible').checked = rule.visible !== false;
}
$('btnSetPolicy').onclick = async () => {
  const raw = $('policyActions').value.trim();
  const actions = raw === '*' ? '*' : raw.split(',').map(s => s.trim()).filter(Boolean);
  const r = await request({type: 'set_participant_access', node_id: $('policyNode').value, actions,
    visible: $('policyVisible').checked});
  if (r.success) { toast('Права участника сохранены'); $('btnGetPolicy').click(); }
};
$('btnClearLog').onclick = () => { state.log = []; $('logCount').textContent = ''; renderLog(); };
$('modalClose').onclick = () => { $('modal').hidden = true; };
$('modal').onclick = e => { if (e.target === $('modal')) $('modal').hidden = true; };
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('modal').hidden = true; });

setInterval(() => {  // телеметрия перестала приходить — предупредить
  if (state.bridge.connected && state.lastTelemetry && Date.now() - state.lastTelemetry > 8000) {
    setLive(false, 'телеметрия не поступает');
  }
}, 2000);

connect();
