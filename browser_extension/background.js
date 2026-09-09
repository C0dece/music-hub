// Контекстное меню на страницах YouTube и VK: один клик - и ссылка уходит в приложение.
import { send, supported } from './bridge.js';

const ITEMS = [
  { id: 'play', title: 'Играть в Music Hub' },
  { id: 'enqueue', title: 'В очередь Music Hub' },
  { id: 'add_to_vk', title: 'Добавить в мою музыку VK' },
  { id: 'open', title: 'Открыть в Music Hub' },
];

const PATTERNS = [
  'https://*.youtube.com/*',
  'https://youtu.be/*',
  'https://*.vk.com/*',
  'https://*.vk.ru/*',
];

function buildMenu() {
  chrome.contextMenus.removeAll(() => {
    for (const item of ITEMS) {
      chrome.contextMenus.create({
        id: item.id,
        title: item.title,
        contexts: ['page', 'link'],
        documentUrlPatterns: PATTERNS,
        targetUrlPatterns: PATTERNS,
      });
    }
  });
}

chrome.runtime.onInstalled.addListener(buildMenu);
chrome.runtime.onStartup.addListener(buildMenu);

function notify(message) {
  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'icon128.png',
    title: 'Music Hub',
    message,
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.pageUrl || (tab && tab.url) || '';
  if (!supported(url)) {
    notify('Эта ссылка приложению не подходит');
    return;
  }
  try {
    await send(info.menuItemId, url, (tab && tab.title) || '');
  } catch (error) {
    notify(String(error.message || error));
  }
});
