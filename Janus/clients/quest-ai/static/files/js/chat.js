const msgs = $('msgs'), input = $('input'), sendBtn = $('send');

function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = 'm ' + role;
  d.textContent = text;
  msgs.appendChild(d);
  msgs.scrollTop = msgs.scrollHeight;
  return d;
}

async function askAssistant(text) {
  if (CONFIG.useMock) {
    await new Promise(r => setTimeout(r, 900));
    return 'Демо-ответ: подключите бекенд в js/config.js (useMock: false), и здесь будет ответ языковой модели.';
  }
  const r = await fetch(CONFIG.chatUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text })
  });
  if (!r.ok) throw new Error(r.status);
  return (await r.json()).reply;
}

async function send() {
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  input.value = '';
  addMsg('me', text);
  sendBtn.disabled = true;
  const wait = addMsg('bot typing', 'Печатает…');
  try { const a = await askAssistant(text); wait.className = 'm bot'; wait.textContent = a; }
  catch (e) { wait.className = 'm bot'; wait.textContent = 'Не удалось получить ответ. Проверьте соединение с сервером.'; }
  msgs.scrollTop = msgs.scrollHeight;
  sendBtn.disabled = false; input.focus();
}
sendBtn.addEventListener('click', send);
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

addMsg('bot', 'Здравствуйте! Я слежу за телеметрией купола. Спросите про энергию, микроклимат или оборудование.');
if (CONFIG.useMock) {
  addMsg('me', 'Хватит ли энергии до вечера?');
  addMsg('bot', 'Панели дают около 3 кВт, потребление ниже выработки. По прогнозу солнце ослабнет через 5–6 часов, тогда стоит перевести 3D-принтер в экономичный режим или подключить ветрогенератор.');
}
