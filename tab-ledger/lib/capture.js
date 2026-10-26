// Captures every window and tab with its page report, and saves, restores or
// closes them. Used by the dashboard.
import { TAB_KEY_PREFIX, CONTENT_SCRIPT_FILES, canInject, withoutHash } from './common.js';

const COLLECT_TIMEOUT_MS = 2000;
const MAX_SAVED = 50;

// live: ask each tab's content script for a fresh report (injecting it where
// missing). Otherwise use the last report the tab sent, which is instant.
export async function captureSession({ live = false } = {}) {
  const dashboardUrl = chrome.runtime.getURL('');
  const [windows, stored] = await Promise.all([
    chrome.windows.getAll({ populate: true, windowTypes: ['normal', 'popup', 'app'] }),
    chrome.storage.session.get(null),
  ]);
  const snapshot = { capturedAt: new Date().toISOString(), windows: [] };
  for (const w of windows) {
    const tabs = await Promise.all(w.tabs
      .filter((t) => !(t.url || t.pendingUrl || '').startsWith(dashboardUrl))
      .map((t) => describeTab(t, stored[TAB_KEY_PREFIX + t.id], live)));
    if (!tabs.length) continue;
    snapshot.windows.push({
      id: w.id,
      type: w.type,
      state: w.state,
      focused: w.focused,
      incognito: w.incognito,
      left: w.left,
      top: w.top,
      width: w.width,
      height: w.height,
      tabs,
    });
  }
  return snapshot;
}

async function describeTab(tab, stored, live) {
  let report = live ? await collect(tab) : null;
  // Ignore a stored report left over from a page the tab has since navigated away from.
  if (!report && stored && withoutHash(stored.url) === withoutHash(tab.url)) report = stored;
  return {
    id: tab.id,
    index: tab.index,
    title: tab.title || '',
    url: tab.url || '',
    pendingUrl: tab.pendingUrl,
    favIconUrl: tab.favIconUrl || '',
    active: tab.active,
    pinned: tab.pinned,
    audible: !!tab.audible,
    muted: !!tab.mutedInfo?.muted,
    discarded: !!tab.discarded,
    groupId: tab.groupId,
    status: tab.status,
    lastAccessed: tab.lastAccessed ? new Date(tab.lastAccessed).toISOString() : null,
    page: report?.page || null,
    activity: report?.activity || null,
  };
}

async function collect(tab) {
  if (!canInject(tab)) return null;
  const ask = () => withTimeout(chrome.tabs.sendMessage(tab.id, { type: 'tab-ledger:collect' }), COLLECT_TIMEOUT_MS);
  try {
    return await ask();
  } catch {
    // No content script yet (e.g. the tab was open before the extension was installed).
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: CONTENT_SCRIPT_FILES });
      return await ask();
    } catch {
      return null;
    }
  }
}

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))]);
}

// ---- saved snapshots -------------------------------------------------------

// Saved snapshots and exports leave out the raw page text kept for AI summaries.
export function forSaving(snapshot) {
  return {
    ...snapshot,
    windows: snapshot.windows.map((w) => ({
      ...w,
      tabs: w.tabs.map((t) => (t.page ? { ...t, page: { ...t.page, excerpt: undefined } } : t)),
    })),
  };
}

export async function listSaved() {
  const { snapshots = [] } = await chrome.storage.local.get('snapshots');
  return snapshots;
}

export async function saveSnapshot(snapshot, name) {
  const snapshots = await listSaved();
  const saved = { ...forSaving(snapshot), savedId: crypto.randomUUID(), name: name || null };
  snapshots.unshift(saved);
  snapshots.length = Math.min(snapshots.length, MAX_SAVED);
  await chrome.storage.local.set({ snapshots });
  return saved;
}

export async function deleteSaved(savedId) {
  const snapshots = (await listSaved()).filter((s) => s.savedId !== savedId);
  await chrome.storage.local.set({ snapshots });
}

// ---- restore and close ----------------------------------------------------

// Reopens a snapshot's windows from inside Chrome, keeping pinned tabs and the
// window's size and position. Returns how many tabs could not be opened.
export async function restoreSnapshot(snapshot) {
  let failed = 0;
  for (const w of snapshot.windows) {
    const create = { focused: false, incognito: !!w.incognito };
    if (w.state === 'normal' && w.width) Object.assign(create, { left: w.left, top: w.top, width: w.width, height: w.height });
    let win;
    try {
      win = await chrome.windows.create(create);
    } catch {
      win = await chrome.windows.create({ focused: false });
    }
    const placeholder = win.tabs?.[0]?.id;
    let opened = 0;
    for (const t of w.tabs) {
      const url = t.url || t.pendingUrl;
      if (!url) continue;
      try {
        await chrome.tabs.create({ windowId: win.id, url, pinned: !!t.pinned, active: !!t.active });
        opened++;
      } catch {
        failed++;  // e.g. file:// without file access, or a chrome:// page extensions may not open
      }
    }
    if (opened && placeholder != null) await chrome.tabs.remove(placeholder).catch(() => {});
    if (w.state === 'maximized' || w.state === 'fullscreen') await chrome.windows.update(win.id, { state: w.state }).catch(() => {});
  }
  return failed;
}

// Closes the captured windows and tabs. The window holding the dashboard stays
// open with only the dashboard left in it.
export async function closeCaptured(snapshot) {
  const self = await chrome.tabs.getCurrent();
  for (const w of snapshot.windows) {
    if (w.id === self?.windowId) {
      await chrome.tabs.remove(w.tabs.map((t) => t.id)).catch(() => {});
    } else {
      await chrome.windows.remove(w.id).catch(() => {});
    }
  }
}
