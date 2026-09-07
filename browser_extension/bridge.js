// Связь с приложением. Расширение не знает ни о VK, ни о токенах сервисов:
// оно умеет только передать ссылку в приложение на этом же компьютере.
const HOST = '127.0.0.1';
const PORTS = [48211, 48212, 48213, 48214, 48215];

export async function getConfig() {
  const data = await chrome.storage.local.get({ token: '', port: 0 });
  return data;
}

export async function setConfig(patch) {
  await chrome.storage.local.set(patch);
}

async function request(port, path, token, body) {
  const response = await fetch(`http://${HOST}:${port}${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

// Приложение занимает первый свободный порт из диапазона, поэтому удачный
// запоминаем: перебирать пять портов на каждый клик незачем.
async function callOnce(path, token, port, body) {
  return request(port, path, token, body);
}

export async function call(path, body) {
  const { token, port } = await getConfig();
  if (!token) {
    throw new Error('Не задан ключ. Откройте настройки расширения.');
  }
  const candidates = port ? [port, ...PORTS.filter((p) => p !== port)] : PORTS;
  let lastError = null;
  for (const candidate of candidates) {
    try {
      const data = await callOnce(path, token, candidate, body);
      if (candidate !== port) {
        await setConfig({ port: candidate });
      }
      return data;
    } catch (error) {
      lastError = error;
      // Неверный ключ или отказ — перебирать остальные порты бессмысленно
      if (/token|origin/i.test(String(error.message))) {
        break;
      }
    }
  }
  throw lastError || new Error('Приложение не отвечает');
}

export async function status() {
  return call('/status', null);
}

export async function send(action, url, title) {
  return call('/command', { action, url, title: title || '' });
}

export function supported(url) {
  return /^https?:\/\/([\w-]+\.)?(youtube\.com|youtu\.be|vk\.com|vk\.ru)\//.test(url || '');
}
