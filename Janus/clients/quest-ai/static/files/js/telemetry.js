const $ = id => document.getElementById(id);
const rnd = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const hist = [];

// ---- демо-данные (формат ответа сервера должен совпадать) ----
const sim = {
  solar: 3, wind: 0.8, temp: 22, hum: 45, co2: 700, fuel: 68,
  lines: Array(8).fill(0).map(() => rnd(.2, 1)),
  printer: { status: 'printing', kind: 'printing', progress: 42, totalMin: 180 },
  cnc: { status: 'idle', kind: 'milling', progress: 0, totalMin: 60 },
  fSun: Array.from({length: 12}, (_, i) => Math.round(clamp(100 - Math.abs(i - 5) * 16 + rnd(-8, 8), 0, 100))),
  fWind: Array.from({length: 12}, () => +rnd(2, 12).toFixed(1))
};
function step(m) {
  if (m.status === m.kind) {
    m.progress += rnd(.3, 1);
    if (m.progress >= 100) { m.status = 'idle'; m.progress = 0; }
    else if (Math.random() < .02) m.status = 'paused';
  } else if (m.status === 'paused') { if (Math.random() < .1) m.status = m.kind; }
  else if (Math.random() < .03) m.status = m.kind;
  return { status: m.status, progress: +m.progress.toFixed(1), remainMin: Math.round((100 - m.progress) / 100 * m.totalMin) };
}
function mockTelemetry() {
  sim.solar = clamp(sim.solar + rnd(-.3, .3), 0, 6);
  sim.wind = clamp(sim.wind + rnd(-.15, .15), 0, 2);
  sim.temp = clamp(sim.temp + rnd(-.2, .2), 17, 32);
  sim.hum = clamp(sim.hum + rnd(-1, 1), 25, 80);
  sim.co2 = clamp(sim.co2 + rnd(-40, 45), 420, 1800);
  sim.fuel = clamp(sim.fuel - .02, 0, 100);
  sim.lines = sim.lines.map(v => clamp(v + rnd(-.1, .1), 0, 1.5));
  return {
    solarKw: sim.solar, windKw: sim.wind, lines: sim.lines,
    temp: sim.temp, hum: sim.hum, co2: sim.co2,
    printer: step(sim.printer), cnc: step(sim.cnc),
    diesel: { on: sim.solar < 1, kw: sim.solar < 1 ? 2.2 : 0, fuel: sim.fuel },
    fSun: sim.fSun, fWind: sim.fWind
  };
}

async function getTelemetry() {
  if (CONFIG.useMock) return mockTelemetry();
  const r = await fetch(CONFIG.telemetryUrl);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// ---- отрисовка ----
const STATUS = { printing: 'Печатает', milling: 'Фрезерует', paused: 'Пауза', idle: 'Простой', error: 'Авария' };
function machine(id, d) {
  const a = d.status === 'printing' || d.status === 'milling';
  if (d.status === 'error') { $(id).innerHTML = `<span class="badge pause">${STATUS.error}</span>`; return; }
  const h = d.status === 'idle' ? '' : `<div class="bar"><div style="width:${d.progress}%"></div></div>
    <div class="kv"><span>Готово <b>${Math.round(d.progress)}%</b></span><span>Осталось <b>${Math.floor(d.remainMin / 60)} ч ${d.remainMin % 60} мин</b></span></div>`;
  $(id).innerHTML = `<span class="badge ${a ? 'on' : d.status === 'paused' ? 'pause' : ''}">${STATUS[d.status]}</span>${h}`;
}
function bars(id, arr, unit, startHour) {
  // startHour — первый час прогноза по времени купола (от сервера); в демо — по часам браузера
  const max = Math.max(...arr, 1), now = startHour ?? new Date().getHours();
  $(id).innerHTML = arr.map((v, i) => `<div title="${v} ${unit}"><i style="height:${v / max * 80}%"></i>${(now + i) % 24}ч</div>`).join('');
}
function spark(vals) {
  const max = Math.max(...vals, 1), n = vals.length;
  const pts = vals.map((v, i) => `${i / Math.max(n - 1, 1) * 200},${48 - v / max * 44}`);
  $('solarSpark').innerHTML = `<path d="M0,50 L${pts.join(' L')} L200,50Z"/><polyline points="${pts.join(' ')}"/>`;
}
function tile(id, text, warn, bad) {
  $(id).textContent = text;
  $(id).parentElement.className = 'tile' + (bad ? ' bad' : warn ? ' warn' : '');
}
function render(t) {
  const gen = t.solarKw, cons = t.lines.reduce((a, b) => a + b, 0);
  $('solar').textContent = gen.toFixed(1);
  hist.push(gen); if (hist.length > 40) hist.shift(); spark(hist);
  $('cons').textContent = cons.toFixed(1) + ' кВт';
  const bal = gen + t.windKw + t.diesel.kw - cons;
  $('bal').textContent = (bal >= 0 ? '+' : '') + bal.toFixed(1) + ' кВт';
  $('lines').innerHTML = t.lines.map((v, i) => `<div class="ln"><span>${CONFIG.lineNames[i]}</span><div class="track"><div style="width:${v / CONFIG.lineMaxKw * 100}%"></div></div><em>${v.toFixed(2)} кВт</em></div>`).join('');
  tile('temp', t.temp.toFixed(1) + ' °C', t.temp > 27 || t.temp < 19, t.temp > 30);
  tile('hum', Math.round(t.hum) + ' %', t.hum > 65 || t.hum < 30);
  tile('co2', Math.round(t.co2) + ' ppm', t.co2 > 1000, t.co2 > 1500);
  machine('printer', t.printer); machine('cnc', t.cnc);
  $('diesel').innerHTML = `<span class="badge ${t.diesel.on ? 'on' : ''}">${t.diesel.on ? 'Работает' : 'Выключен'}</span>
    <div class="kv"><span>Мощность <b>${t.diesel.kw.toFixed(1)} кВт</b></span><span>Топливо <b>${Math.round(t.diesel.fuel)}%</b></span></div>
    <div class="bar"><div style="width:${t.diesel.fuel}%"></div></div>`;
  const wOn = t.windKw > 0.05;
  $('windgen').innerHTML = `<span class="badge ${wOn ? 'on' : ''}">${wOn ? 'Вырабатывает' : 'Штиль'}</span>
    <div class="kv"><span>Мощность <b>${t.windKw.toFixed(2)} кВт</b></span></div>`;
  bars('fSun', t.fSun, '%', t.forecastStartHour); bars('fWind', t.fWind, 'м/с', t.forecastStartHour);
  $('upd').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
}
async function tick() {
  try { render(await getTelemetry()); }
  catch (e) { $('upd').textContent = 'нет связи с куполом'; }
}
tick(); setInterval(tick, CONFIG.refreshMs);
