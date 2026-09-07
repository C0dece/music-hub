import { getConfig, setConfig, status } from './bridge.js';

const tokenInput = document.getElementById('token');
const portInput = document.getElementById('port');
const msg = document.getElementById('msg');

getConfig().then(({ token, port }) => {
  tokenInput.value = token || '';
  portInput.value = port || 48211;
});

document.getElementById('save').addEventListener('click', async () => {
  const port = Number(portInput.value) || 48211;
  await setConfig({ token: tokenInput.value.trim(), port });
  msg.textContent = 'Сохранено';
});

document.getElementById('check').addEventListener('click', async () => {
  msg.textContent = 'Проверяю…';
  try {
    await status();
    const { port } = await getConfig();
    msg.textContent = `Приложение отвечает на порту ${port}`;
  } catch (error) {
    msg.textContent = `Связи нет: ${error.message || error}`;
  }
});
