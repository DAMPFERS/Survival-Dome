const $ = id => document.getElementById(id);
const rnd = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const NS = 'http://www.w3.org/2000/svg';
const N = CONFIG.lineCount;
const SOLAR_MAX_KW = 8;

function fmtHM(min) {
  min = ((Math.round(min) % 1440) + 1440) % 1440;
  return String(Math.floor(min / 60)).padStart(2, '0') + ':' + String(min % 60).padStart(2, '0');
}
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * p;
}
function lineStyle(idx) {
  return { color: CONFIG.lineHues[idx % 8], dash: idx < 8 ? 'dashed' : 'dotted' };
}
function svgEl(name, attrs) {
  const el = document.createElementNS(NS, name);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}
function path(pts) {
  return pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
}

// ---------------- симуляция ----------------
function solarBase(min) {
  const sunrise = 360, sunset = 1200;
  if (min <= sunrise || min >= sunset) return 0;
  return Math.sin(Math.PI * (min - sunrise) / (sunset - sunrise));
}
const state = {
  simMin: rnd(7 * 60, 10 * 60),
  cloud: rnd(0.05, 0.3),
  lines: Array.from({ length: N }, () => ({ on: true, base: rnd(0.3, 2.4), variability: rnd(0.05, 0.25), kw: 0 })),
  history: [],
  forecast: null,
  activeLines: new Set()
};

function computeForecast() {
  const steps = Math.round(CONFIG.forecastHorizonMin / CONFIG.forecastStepMin);
  const t = [], mean = [], sd = [];
  for (let i = 0; i <= steps; i++) {
    const tm = state.simMin + i * CONFIG.forecastStepMin;
    const base = solarBase(((tm % 1440) + 1440) % 1440);
    const m = clamp(SOLAR_MAX_KW * base * (1 - state.cloud * 0.85), 0, SOLAR_MAX_KW);
    t.push(tm); mean.push(m);
    sd.push((0.12 + 0.3 * (i / steps)) * SOLAR_MAX_KW * (0.25 + 0.75 * base));
  }
  state.forecast = { t, mean, sd };
}

function sampleNow() {
  const base = solarBase(((state.simMin % 1440) + 1440) % 1440);
  const gen = clamp(SOLAR_MAX_KW * base * (1 - state.cloud * 0.85) + rnd(-0.15, 0.15), 0, SOLAR_MAX_KW);
  let cons = 0; const per = [];
  state.lines.forEach((l, i) => {
    if (l.on) {
      const tf = 1 + 0.2 * Math.sin(2 * Math.PI * (state.simMin / 1440) + i * 0.4);
      l.kw = clamp(l.base * tf + rnd(-l.variability, l.variability), 0, CONFIG.lineMaxKw);
    } else l.kw = 0;
    per.push(l.kw); cons += l.kw;
  });
  return { t: state.simMin, gen, cons, per };
}

function tick() {
  state.simMin += CONFIG.simSpeedMinPerTick;
  state.cloud = clamp(state.cloud + rnd(-0.03, 0.03), 0, 0.7);
  state.history.push(sampleNow());
  const cutoff = state.simMin - CONFIG.historyWindowMin;
  while (state.history.length > 1 && state.history[0].t < cutoff) state.history.shift();

  computeForecast();
  render();
  $('upd').textContent = 'время купола ' + fmtHM(state.simMin);
}

// ---------------- кнопки линий ----------------
const btnEls = [];
function buildButtons() {
  const wrap = $('lineButtons');
  state.lines.forEach((l, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'line-btn';
    b.style.setProperty('--lc', CONFIG.lineHues[i % 8]);
    b.innerHTML = '<span class="lb-top"><span class="lb-dot"></span><span class="lb-name"></span><span class="lb-badge"></span></span><span class="lb-kw"></span>';
    b.querySelector('.lb-name').textContent = CONFIG.lineNames[i];
    b.addEventListener('click', () => {
      l.on = !l.on;
      if (!l.on) l.kw = 0;
      updateButton(i); updateChips();
    });
    wrap.appendChild(b);
    btnEls.push(b);
  });
}
function updateButton(i) {
  const l = state.lines[i], b = btnEls[i];
  b.classList.toggle('is-on', l.on);
  b.classList.toggle('is-off', !l.on);
  b.querySelector('.lb-badge').textContent = l.on ? 'ВКЛ' : 'ВЫКЛ';
  b.querySelector('.lb-kw').innerHTML = (l.on ? l.kw.toFixed(2) : '—') + ' <small>кВт</small>';
}

// ---------------- чекбоксы (показать линию на графике) ----------------
const chipEls = [];
function buildChips() {
  const wrap = $('lineToggles');
  state.lines.forEach((l, i) => {
    const chip = document.createElement('label');
    chip.className = 'chip';
    const st = lineStyle(i);
    chip.style.setProperty('--lc', st.color);
    chip.innerHTML = '<input type="checkbox"><span class="sw' + (st.dash === 'dotted' ? ' dotted' : '') + '"></span><span class="chip-txt"></span>';
    const cb = chip.querySelector('input');
    cb.addEventListener('change', () => {
      if (cb.checked) state.activeLines.add(i); else state.activeLines.delete(i);
      chip.classList.toggle('active', cb.checked);
      renderPowerChart();
    });
    wrap.appendChild(chip);
    chipEls.push(chip);
  });
}
function updateChips() {
  state.lines.forEach((l, i) => {
    const txt = chipEls[i].querySelector('.chip-txt');
    txt.textContent = CONFIG.lineNames[i] + ' · ' + (l.on ? l.kw.toFixed(2) : '0.00') + ' кВт';
  });
}

function buildLegend() {
  const wrap = $('mainLegend');
  wrap.innerHTML =
    '<span class="legend-item" style="--lc:var(--or)"><i></i>Генерация</span>' +
    '<span class="legend-item" style="--lc:var(--bl2)"><i></i>Потребление, всего</span>';
}

// ---------------- график: потребление и генерация ----------------
const W = 640, H = 220, PAD = { l: 38, r: 10, t: 10, b: 22 };
let powerScale = null, solarScale = null;

function axisAndGrid(svg, xDomain, yMax, xTickFmt) {
  const g = svgEl('g', { class: 'grid' });
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const v = yMax * i / yTicks;
    const y = H - PAD.b - (v / yMax) * (H - PAD.t - PAD.b);
    g.appendChild(svgEl('line', { x1: PAD.l, x2: W - PAD.r, y1: y.toFixed(1), y2: y.toFixed(1) }));
    const t = svgEl('text', { class: 'axis', x: PAD.l - 6, y: (y + 3).toFixed(1), 'text-anchor': 'end' });
    t.textContent = v.toFixed(v < 2 ? 1 : 0);
    g.appendChild(t);
  }
  svg.appendChild(g);
  const xTicks = 5;
  const ga = svgEl('g', { class: 'axis' });
  for (let i = 0; i <= xTicks; i++) {
    const tv = xDomain[0] + (xDomain[1] - xDomain[0]) * i / xTicks;
    const x = PAD.l + (i / xTicks) * (W - PAD.l - PAD.r);
    const t = svgEl('text', { x: x.toFixed(1), y: H - 6, 'text-anchor': i === 0 ? 'start' : i === xTicks ? 'end' : 'middle' });
    t.textContent = xTickFmt(tv);
    ga.appendChild(t);
  }
  svg.appendChild(ga);
}

function renderPowerChart() {
  const svg = $('powerChart');
  svg.innerHTML = '';
  const h = state.history;
  if (h.length < 2) return;
  const xDomain = [h[0].t, h[h.length - 1].t];
  let yMax = 0;
  h.forEach(s => { yMax = Math.max(yMax, s.gen, s.cons); state.activeLines.forEach(i => yMax = Math.max(yMax, s.per[i])); });
  yMax = niceMax(yMax * 1.15);
  const X = t => PAD.l + (t - xDomain[0]) / (xDomain[1] - xDomain[0] || 1) * (W - PAD.l - PAD.r);
  const Y = v => H - PAD.b - v / yMax * (H - PAD.t - PAD.b);

  axisAndGrid(svg, xDomain, yMax, fmtHM);

  function addSeries(getter, color, extraClass, endLabel) {
    const pts = h.map(s => [X(s.t), Y(getter(s))]);
    svg.appendChild(svgEl('path', { class: 'series' + (extraClass ? ' ' + extraClass : ''), d: path(pts), stroke: color }));
    const last = pts[pts.length - 1];
    svg.appendChild(svgEl('circle', { class: 'dot-end', cx: last[0].toFixed(1), cy: last[1].toFixed(1), r: 4, fill: color }));
    if (endLabel) {
      const t = svgEl('text', { class: 'axis', x: (last[0] - 6).toFixed(1), y: (last[1] - 8).toFixed(1), 'text-anchor': 'end', fill: '#f1f0f5' });
      t.textContent = endLabel;
      svg.appendChild(t);
    }
  }
  addSeries(s => s.gen, '#ff5c01', '', h[h.length - 1].gen.toFixed(1) + ' кВт');
  addSeries(s => s.cons, '#3f86ff', '', h[h.length - 1].cons.toFixed(1) + ' кВт');
  state.activeLines.forEach(i => {
    const st = lineStyle(i);
    addSeries(s => s.per[i], st.color, st.dash);
  });

  powerScale = { xDomain, yMax, X, Y };
  buildTable();
}

function renderSolarChart() {
  const svg = $('solarChart');
  svg.innerHTML = '';
  const f = state.forecast;
  if (!f) return;
  const xDomain = [f.t[0], f.t[f.t.length - 1]];
  let yMax = 0;
  f.mean.forEach((m, i) => yMax = Math.max(yMax, m + f.sd[i]));
  yMax = niceMax(yMax * 1.15);
  const X = t => PAD.l + (t - xDomain[0]) / (xDomain[1] - xDomain[0] || 1) * (W - PAD.l - PAD.r);
  const Y = v => H - PAD.b - clamp(v, 0, yMax) / yMax * (H - PAD.t - PAD.b);

  axisAndGrid(svg, xDomain, yMax, fmtHM);

  const upper = f.t.map((t, i) => [X(t), Y(f.mean[i] + f.sd[i])]);
  const lower = f.t.map((t, i) => [X(t), Y(Math.max(0, f.mean[i] - f.sd[i]))]).reverse();
  svg.appendChild(svgEl('path', { class: 'band', d: path(upper) + ' ' + path(lower).replace('M', 'L') + ' Z', fill: '#ff5c01', stroke: 'none' }));
  const meanPts = f.t.map((t, i) => [X(t), Y(f.mean[i])]);
  svg.appendChild(svgEl('path', { class: 'series', d: path(meanPts), stroke: '#ff5c01' }));

  solarScale = { xDomain, yMax, X, Y };
  buildSolarTable();
}

// ---------------- всплывающие подсказки ----------------
function nearestIndex(arr, tVal) {
  let lo = 0, hi = arr.length - 1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < tVal) lo = mid + 1; else hi = mid; }
  if (lo > 0 && Math.abs(arr[lo - 1] - tVal) < Math.abs(arr[lo] - tVal)) lo--;
  return lo;
}
function pxToData(wrap, evt, scale) {
  const rect = wrap.getBoundingClientRect();
  const px = evt.clientX - rect.left;
  const py = evt.clientY - rect.top;
  const xSvg = px / rect.width * W;
  const t = scale.xDomain[0] + (xSvg - PAD.l) / (W - PAD.l - PAD.r) * (scale.xDomain[1] - scale.xDomain[0]);
  return { t, px, py, rect };
}
function placeTip(tip, wrapRect, px, py) {
  tip.style.left = clamp(px, 60, wrapRect.width - 10) + 'px';
  tip.style.top = Math.max(py - 10, 10) + 'px';
}
function rowHtml(colorVar, dashClass, label, value) {
  return '<div class="tip-row"><span class="k"><i class="' + dashClass + '" style="--lc:' + colorVar + '"></i>' + label + '</span><b>' + value + '</b></div>';
}

function setupHover(wrapId, svgId, tipId, getScale, onNearest) {
  const wrap = $(wrapId), tip = $(tipId);
  wrap.addEventListener('pointermove', e => {
    const scale = getScale();
    if (!scale) return;
    const { t, px, py, rect } = pxToData(wrap, e, scale);
    const html = onNearest(t);
    if (!html) { tip.hidden = true; return; }
    tip.hidden = false;
    tip.innerHTML = html;
    placeTip(tip, rect, px, py);
  });
  wrap.addEventListener('pointerleave', () => { tip.hidden = true; });
}

function initHover() {
  setupHover('powerWrap', 'powerChart', 'powerTip', () => powerScale, t => {
    const h = state.history;
    if (!h.length) return '';
    const times = h.map(s => s.t);
    const idx = nearestIndex(times, t);
    const s = h[idx];
    let html = '<div class="tip-time">' + fmtHM(s.t) + '</div>';
    html += rowHtml('#ff5c01', '', 'Генерация', s.gen.toFixed(2) + ' кВт');
    html += rowHtml('#3f86ff', '', 'Потребление', s.cons.toFixed(2) + ' кВт');
    state.activeLines.forEach(i => {
      const st = lineStyle(i);
      html += rowHtml(st.color, st.dash, CONFIG.lineNames[i], s.per[i].toFixed(2) + ' кВт');
    });
    return html;
  });
  setupHover('solarWrap', 'solarChart', 'solarTip', () => solarScale, t => {
    const f = state.forecast;
    if (!f) return '';
    const idx = nearestIndex(f.t, t);
    let html = '<div class="tip-time">' + fmtHM(f.t[idx]) + '</div>';
    html += rowHtml('#ff5c01', '', 'Прогноз', f.mean[idx].toFixed(2) + ' кВт');
    html += rowHtml('#ff5c01', '', '± σ', f.sd[idx].toFixed(2) + ' кВт');
    return html;
  });
}

// ---------------- табличное представление ----------------
function buildTable() {
  const h = state.history;
  const cols = ['Время', 'Генерация, кВт', 'Потребление, кВт'];
  state.activeLines.forEach(i => cols.push(CONFIG.lineNames[i] + ', кВт'));
  let html = '<thead><tr>' + cols.map(c => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>';
  h.forEach(s => {
    html += '<tr><td>' + fmtHM(s.t) + '</td><td>' + s.gen.toFixed(2) + '</td><td>' + s.cons.toFixed(2) + '</td>';
    state.activeLines.forEach(i => html += '<td>' + s.per[i].toFixed(2) + '</td>');
    html += '</tr>';
  });
  html += '</tbody>';
  $('powerTable').innerHTML = html;
}
function buildSolarTable() {
  const f = state.forecast;
  let html = '<thead><tr><th>Время</th><th>Прогноз, кВт</th><th>σ, кВт</th><th>Диапазон, кВт</th></tr></thead><tbody>';
  f.t.forEach((t, i) => {
    const lo = Math.max(0, f.mean[i] - f.sd[i]).toFixed(2), hiv = (f.mean[i] + f.sd[i]).toFixed(2);
    html += '<tr><td>' + fmtHM(t) + '</td><td>' + f.mean[i].toFixed(2) + '</td><td>' + f.sd[i].toFixed(2) + '</td><td>' + lo + '–' + hiv + '</td></tr>';
  });
  html += '</tbody>';
  $('solarTable').innerHTML = html;
}

function render() {
  state.lines.forEach((_, i) => updateButton(i));
  updateChips();
  renderPowerChart();
  renderSolarChart();
}

buildButtons(); buildChips(); buildLegend();
state.history.push(sampleNow());
state.simMin += CONFIG.simSpeedMinPerTick;
state.history.push(sampleNow());
computeForecast();
initHover();
render();
$('upd').textContent = 'время купола ' + fmtHM(state.simMin);
setInterval(tick, CONFIG.refreshMs);
