// Service worker: keeps the latest report from each tab's content script in
// session storage, and opens the dashboard when the toolbar button is clicked.
import { TAB_KEY_PREFIX, CONTENT_SCRIPT_FILES, canInject } from './lib/common.js';

chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL('dashboard.html');
  const [existing] = await chrome.tabs.query({ url });
  if (existing) {
    await chrome.tabs.update(existing.id, { active: true });
    await chrome.windows.update(existing.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url });
  }
});

// Tabs that were open before the extension was installed have no content script yet.
chrome.runtime.onInstalled.addListener(async () => {
  const tabs = await chrome.tabs.query({});
  await Promise.all(tabs.filter((t) => canInject(t)).map((t) =>
    chrome.scripting.executeScript({ target: { tabId: t.id }, files: CONTENT_SCRIPT_FILES }).catch(() => {})));
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === 'tab-ledger:update' && sender.tab?.id != null && sender.frameId === 0) {
    chrome.storage.session.set({ [TAB_KEY_PREFIX + sender.tab.id]: message.record });
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(TAB_KEY_PREFIX + tabId);
});

// Chrome swaps tab ids when it prerenders or discards a tab.
chrome.tabs.onReplaced.addListener(async (addedId, removedId) => {
  const key = TAB_KEY_PREFIX + removedId;
  const { [key]: record } = await chrome.storage.session.get(key);
  if (record) await chrome.storage.session.set({ [TAB_KEY_PREFIX + addedId]: record });
  await chrome.storage.session.remove(key);
});
