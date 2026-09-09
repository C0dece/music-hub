import { send, status, supported } from './bridge.js';

const now = document.getElementById('now');
const msg = document.getElementById('msg');
let pageUrl = '';
let pageTitle = '';

function say(text) {
  msg.textContent = text;
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  pageUrl = (tab && tab.url) || '';
  pageTitle = (tab && tab.title) || '';
  const ok = supported(pageUrl);
  for (const id of ['play', 'enqueue', 'add_to_vk', 'open']) {
    document.getElementById(id).disabled = !ok;
  }
  if (!ok) {
    say('Откройте страницу YouTube или VK');
  }
  try {
    const data = await status();
    const track = data.track;
    now.textContent = track
      ? `${data.playing ? '▶' : '❚❚'} ${track.artist ? track.artist + ' - ' : ''}${track.title || ''}`
      : 'Приложение на связи, ничего не играет';
  } catch (error) {
    now.textContent = 'Приложение не отвечает';
    say('Проверьте, запущено ли приложение и верен ли ключ в настройках расширения.');
  }
}

for (const id of ['play', 'enqueue', 'add_to_vk', 'open']) {
  document.getElementById(id).addEventListener('click', async () => {
    try {
      await send(id, pageUrl, pageTitle);
      say('Отправлено в приложение');
    } catch (error) {
      say(String(error.message || error));
    }
  });
}

init();
