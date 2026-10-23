import {
  captureSession, saveSnapshot, listSaved, deleteSaved, restoreSnapshot, closeCaptured, forSaving,
} from './lib/capture.js';
import { PLATFORMS, buildRelaunchScript } from './lib/relaunch.js';
import { TAB_KEY_PREFIX } from './lib/common.js';

const $ = (id) => document.getElementById(id);

const state = {
  live: null,          // snapshot of the open windows
  viewing: null,       // a saved snapshot being looked at, or null
  filter: 'all',
  query: '',
  settings: {},
  aiSummaries: new Map(),  // url -> summary from Chrome's built-in model
  openDetails: new Set(),  // tab keys whose "More details" is expanded
};

const shown = () => state.viewing || state.live;

// ---- helpers -------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c);
  return node;
}

function setStatus(text) {
  $('status').textContent = text;
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString() : '';
}

function download(filename, content, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  el('a', { href: url, download: filename }).click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function allTabs(snapshot) {
  return snapshot ? snapshot.windows.flatMap((w) => w.tabs) : [];
}

function usedClipboard(t) {
  const a = t.activity;
  return !!a && (a.copies + a.cuts + a.pastes) > 0;
}

function hostAndPath(url) {
  try {
    const u = new URL(url);
    return u.protocol.startsWith('http') ? u.host + (u.pathname === '/' ? '' : u.pathname) : url;
  } catch {
    return url;
  }
}

function defaultPlatform() {
  const p = (navigator.userAgentData?.platform || navigator.platform || '').toLowerCase();
  return p.startsWith('win') ? 'bat' : 'sh';
}

// ---- capture -------------------------------------------------------------

async function refresh({ live = false } = {}) {
  const snapshot = await captureSession({ live });
  for (const t of allTabs(snapshot)) {
    const ai = state.aiSummaries.get(t.url);
    if (ai && t.page) t.page.aiSummary = ai;
  }
  state.live = snapshot;
  if (!state.viewing) render();
}

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refresh().catch(console.error), 700);
}

// ---- rendering -----------------------------------------------------------

function render() {
  const snapshot = shown();
  if (!snapshot) return;
  $('captured-at').textContent = (state.viewing ? 'Saved ' : 'Updated ') + when(snapshot.capturedAt);
  $('viewing-banner').hidden = !state.viewing;
  if (state.viewing) {
    $('viewing-text').textContent = `Viewing a snapshot saved ${when(state.viewing.capturedAt)}` +
      (state.viewing.name ? ` (${state.viewing.name})` : '') + '.';
  }
  $('close-after').disabled = !!state.viewing;
  $('refresh').disabled = !!state.viewing;
  renderStats(snapshot);
  renderWindows(snapshot);
}

function renderStats(snapshot) {
  const tabs = allTabs(snapshot);
  const sum = (key) => tabs.reduce((n, t) => n + (t.activity?.[key] || 0), 0);
  const items = [
    [snapshot.windows.length, 'windows', 'windows'],
    [tabs.length, 'tabs', 'tabs'],
    [tabs.filter((t) => t.activity?.scrolled).length, 'scrolled', 'scrolled'],
    [tabs.filter((t) => t.activity && !t.activity.scrolled).length, 'not scrolled', 'unscrolled'],
    [sum('copies') + sum('cuts'), 'copies', 'copies'],
    [sum('pastes'), 'pastes', 'pastes'],
  ];
  $('stats').replaceChildren(...items.map(([n, label, ui]) =>
    el('div', { class: 'stat', 'data-ui': `R3.${ui}` }, el('b', {}, String(n)), el('span', {}, label))));
}

function matches(t) {
  const a = t.activity;
  if (state.filter === 'scrolled' && !a?.scrolled) return false;
  if (state.filter === 'unscrolled' && (!a || a.scrolled)) return false;
  if (state.filter === 'clipboard' && !usedClipboard(t)) return false;
  if (!state.query) return true;
  const hay = [t.title, t.url, t.page?.description, ...(t.page?.summary || []), t.page?.aiSummary]
    .filter(Boolean).join(' ').toLowerCase();
  return state.query.split(/\s+/).every((q) => hay.includes(q));
}

function renderWindows(snapshot) {
  const cards = snapshot.windows.map((w, i) => {
    const tabs = w.tabs.filter(matches);
    if (!tabs.length) return null;
    const facts = [
      `${w.tabs.length} tab${w.tabs.length === 1 ? '' : 's'}`,
      w.focused && 'focused',
      w.incognito && 'incognito',
      w.type !== 'normal' && w.type,
      w.state !== 'normal' ? w.state : w.width && `${w.width}×${w.height}`,
    ].filter(Boolean).join(' · ');
    return el('section', { class: 'window', 'data-ui': 'R6' },
      el('header', { 'data-ui': 'R6.header' }, el('h3', { 'data-ui': 'R6.title' }, `Window ${i + 1}`),
        el('span', { class: 'muted', 'data-ui': 'R6.facts' }, facts)),
      tabs.map((t) => renderTab(t)));
  }).filter(Boolean);
  $('windows').replaceChildren(...(cards.length ? cards : [el('div', { class: 'window empty' }, 'No tabs match.')]));
}

function renderTab(t) {
  const a = t.activity;
  const p = t.page;
  const badges = [];
  if (a) {
    badges.push(a.scrolled
      ? el('span', { class: 'badge good', 'data-ui': 'R7.badge-scroll' }, `Scrolled · reached ${a.maxScrollPercent}%`)
      : el('span', { class: 'badge warn', 'data-ui': 'R7.badge-scroll' }, 'Not scrolled'));
    if (a.copies || a.cuts) badges.push(el('span', { class: 'badge info', 'data-ui': 'R7.badge-copied' }, `Copied ${a.copies + a.cuts}×`));
    if (a.pastes) badges.push(el('span', { class: 'badge info', 'data-ui': 'R7.badge-pasted' }, `Pasted ${a.pastes}×`));
  } else {
    badges.push(el('span', { class: 'badge', 'data-ui': 'R7.badge-scroll' }, 'No page access'));
  }
  if (t.active) badges.push(el('span', { class: 'badge', 'data-ui': 'R7.badge-active' }, 'Active tab'));
  if (t.pinned) badges.push(el('span', { class: 'badge', 'data-ui': 'R7.badge-pinned' }, 'Pinned'));
  if (t.audible) badges.push(el('span', { class: 'badge', 'data-ui': 'R7.badge-audio' }, t.muted ? 'Muted' : 'Playing audio'));
  if (t.discarded) badges.push(el('span', { class: 'badge', 'data-ui': 'R7.badge-sleeping' }, 'Sleeping'));
  if (p?.readingMinutes) badges.push(el('span', { class: 'badge', 'data-ui': 'R7.badge-words' }, `${p.wordCount.toLocaleString()} words · ${p.readingMinutes} min`));

  const summary = [];
  if (p?.aiSummary) summary.push(el('div', { class: 'label' }, 'AI summary'), el('p', {}, p.aiSummary));
  if (p?.description) summary.push(el('div', { class: 'label' }, 'Description'), el('p', {}, p.description));
  if (p?.summary?.length) summary.push(el('div', { class: 'label' }, 'Key sentences'), el('p', {}, p.summary.join(' ')));

  const icon = t.favIconUrl && /^(https?|data):/.test(t.favIconUrl)
    ? el('img', { class: 'favicon', 'data-ui': 'R7.favicon', src: t.favIconUrl, alt: '', onerror: (e) => e.target.replaceWith(el('div', { class: 'fallback-icon', 'data-ui': 'R7.favicon' })) })
    : el('div', { class: 'fallback-icon', 'data-ui': 'R7.favicon' });

  const key = `${t.id}|${t.url}`;
  const details = el('details', { open: state.openDetails.has(key), ontoggle: (e) => {
    if (e.target.open) state.openDetails.add(key); else state.openDetails.delete(key);
  } }, el('summary', { 'data-ui': 'R7.details' }, 'More details'), detailList(t));

  return el('article', { class: 'tab', 'data-ui': 'R7' },
    icon,
    el('div', { class: 'body' },
      el('button', { class: 'link title', 'data-ui': 'R7.title', title: state.viewing ? 'Open this page' : 'Go to this tab', onclick: () => goTo(t) },
        t.title || t.url || '(untitled)'),
      el('div', { class: 'url', 'data-ui': 'R7.address' }, hostAndPath(t.url || t.pendingUrl || '')),
      el('div', { class: 'badges', 'data-ui': 'R7.badges' }, badges),
      summary.length ? el('div', { class: 'summary', 'data-ui': 'R7.summary' }, summary) : null,
      details));
}

function detailList(t) {
  const a = t.activity || {};
  const p = t.page || {};
  const rows = [
    ['Address', t.url],
    ['Site', p.siteName],
    ['Author', p.author],
    ['Published', p.published && when(p.published)],
    ['Language', p.language],
    ['Type', p.type],
    ['Keywords', p.keywords],
    ['Canonical', p.canonical !== t.url && p.canonical],
    ['Headings', p.headings?.join(' · ')],
    ['Last viewed', when(t.lastAccessed)],
    ['First scroll', when(a.firstScrollAt)],
    ['Last scroll', when(a.lastScrollAt)],
    ['Last copy', when(a.lastCopyAt)],
    ['Last paste', when(a.lastPasteAt)],
    ...(a.clipboard || []).map((c) => [`${c.kind[0].toUpperCase()}${c.kind.slice(1)} ${new Date(c.at).toLocaleTimeString()}`, c.text ? `“${c.text}”` : '(empty)']),
  ].filter(([, v]) => v);
  return el('dl', { class: 'meta' }, rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, String(v))]));
}

async function goTo(t) {
  if (state.viewing) {
    await chrome.tabs.create({ url: t.url || t.pendingUrl }).catch(() => setStatus('Chrome would not open that page.'));
    return;
  }
  try {
    await chrome.tabs.update(t.id, { active: true });
    const tab = await chrome.tabs.get(t.id);
    await chrome.windows.update(tab.windowId, { focused: true });
  } catch {
    setStatus('That tab is no longer open.');
    refreshSoon();
  }
}

// ---- saved snapshots -----------------------------------------------------

async function renderSaved() {
  const saved = await listSaved();
  if (!saved.length) {
    $('saved').replaceChildren(el('li', { class: 'muted' }, 'Nothing saved yet. Snapshots are also saved automatically when you close windows from here.'));
    return;
  }
  $('saved').replaceChildren(...saved.map((s) => {
    const tabs = allTabs(s).length;
    return el('li', { 'data-ui': 'R8.item' },
      el('div', {}, el('b', {}, s.name || when(s.capturedAt)),
        el('div', { class: 'muted' }, `${s.windows.length} window${s.windows.length === 1 ? '' : 's'}, ${tabs} tab${tabs === 1 ? '' : 's'}` +
          (s.name ? ` · ${when(s.capturedAt)}` : ''))),
      el('div', { class: 'buttons', 'data-ui': 'R8.buttons' },
        el('button', { 'data-ui': 'R8.view', onclick: () => { state.viewing = s; render(); window.scrollTo({ top: 0 }); } }, 'View'),
        el('button', { 'data-ui': 'R8.reopen', onclick: () => restore(s) }, 'Reopen in Chrome'),
        el('button', { 'data-ui': 'R8.script', onclick: () => makeScript(s, false) }, 'Script'),
        el('button', { 'data-ui': 'R8.json', onclick: () => exportJson(s) }, 'JSON'),
        el('button', { 'data-ui': 'R8.delete', onclick: async () => {
          if (!confirm('Delete this snapshot?')) return;
          await deleteSaved(s.savedId);
          if (state.viewing?.savedId === s.savedId) { state.viewing = null; render(); }
          renderSaved();
        } }, 'Delete')));
  }));
}

async function restore(snapshot) {
  setStatus('Reopening…');
  const failed = await restoreSnapshot(snapshot);
  setStatus(failed ? `Reopened, but ${failed} tab(s) could not be opened by an extension.` : 'Reopened.');
}

function exportJson(snapshot) {
  const name = `tab-ledger-${snapshot.capturedAt.slice(0, 16).replace(/[:T]/g, '-')}.json`;
  download(name, JSON.stringify(forSaving(snapshot), null, 2), 'application/json');
}

// ---- relaunch script -------------------------------------------------------

async function makeScript(snapshot, closeAfter) {
  const platform = $('platform').value;
  const script = buildRelaunchScript(snapshot, platform);
  const count = allTabs(snapshot).length;
  if (closeAfter && !confirm(`Create the script, then close ${snapshot.windows.length} window(s) and ${count} tab(s)? ` +
    'A snapshot is saved first so you can reopen them from here too.')) return;
  download(script.filename, script.content, script.mime);
  if (!closeAfter) {
    setStatus(`Downloaded ${script.filename}.`);
    return;
  }
  await saveSnapshot(snapshot, `Before closing, ${when(snapshot.capturedAt)}`);
  renderSaved();
  // Let the download start before the windows go away.
  await new Promise((r) => setTimeout(r, 800));
  await closeCaptured(snapshot);
  setStatus(`Downloaded ${script.filename}, saved a snapshot, and closed the windows.`);
  refreshSoon();
}

// ---- Chrome's built-in summarizer (Chrome 138+, when the device supports it) --

async function setUpAi() {
  if (!('Summarizer' in self)) return;
  try {
    if (await self.Summarizer.availability() === 'unavailable') return;
  } catch {
    return;
  }
  $('ai-summarize').hidden = false;
}

async function summarizeWithAi() {
  const button = $('ai-summarize');
  button.disabled = true;
  try {
    // Fresh reports carry the page text the model needs.
    await refresh({ live: true });
    const summarizer = await self.Summarizer.create({
      type: 'tldr',
      format: 'plain-text',
      length: 'short',
      sharedContext: 'A web page someone has open in a browser tab.',
      monitor(m) {
        m.addEventListener('downloadprogress', (e) => setStatus(`Downloading Chrome's summary model… ${Math.round(e.loaded * 100)}%`));
      },
    });
    const todo = allTabs(state.live).filter((t) => t.page?.excerpt?.length > 200 && !state.aiSummaries.has(t.url));
    let done = 0;
    for (const t of todo) {
      setStatus(`Summarizing ${++done} of ${todo.length}…`);
      try {
        const text = await summarizer.summarize(t.page.excerpt, { context: `Page title: ${t.title}` });
        state.aiSummaries.set(t.url, text.trim());
        t.page.aiSummary = text.trim();
        if (!state.viewing) render();
      } catch (err) {
        console.warn('Summary failed for', t.url, err);
      }
    }
    summarizer.destroy?.();
    setStatus(todo.length ? `Summarized ${todo.length} page(s).` : 'Every page with enough text already has an AI summary.');
  } catch (err) {
    setStatus(`Chrome AI is not available right now: ${err.message}`);
  } finally {
    button.disabled = false;
  }
}

// ---- settings ----------------------------------------------------------------

async function loadSettings() {
  const { settings = {} } = await chrome.storage.local.get('settings');
  state.settings = settings;
  $('record-clipboard').checked = !!settings.recordClipboardText;
  $('platform').replaceChildren(...Object.entries(PLATFORMS).map(([k, v]) => el('option', { value: k }, v.label)));
  $('platform').value = settings.platform in PLATFORMS ? settings.platform : defaultPlatform();
}

async function saveSettings(patch) {
  state.settings = { ...state.settings, ...patch };
  await chrome.storage.local.set({ settings: state.settings });
}

// ---- wiring --------------------------------------------------------------------

$('refresh').addEventListener('click', async () => {
  setStatus('Asking every tab for a fresh report…');
  await refresh({ live: true });
  setStatus('');
});
$('save').addEventListener('click', async () => {
  const snapshot = shown();
  await saveSnapshot(snapshot);
  await renderSaved();
  setStatus('Snapshot saved.');
});
$('export-json').addEventListener('click', () => exportJson(shown()));
$('ai-summarize').addEventListener('click', summarizeWithAi);
$('back-to-live').addEventListener('click', () => { state.viewing = null; render(); });
$('make-script').addEventListener('click', async () => {
  if (!state.viewing) await refresh();
  makeScript(shown(), !state.viewing && $('close-after').checked);
});
$('platform').addEventListener('change', (e) => saveSettings({ platform: e.target.value }));
$('record-clipboard').addEventListener('change', (e) => saveSettings({ recordClipboardText: e.target.checked }));
$('search').addEventListener('input', (e) => { state.query = e.target.value.trim().toLowerCase(); render(); });
for (const chip of document.querySelectorAll('.chip')) {
  chip.addEventListener('click', () => {
    state.filter = chip.dataset.filter;
    for (const c of document.querySelectorAll('.chip')) c.setAttribute('aria-pressed', String(c === chip));
    render();
  });
}

// Keep the view current as tabs change and pages report activity.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && Object.keys(changes).some((k) => k.startsWith(TAB_KEY_PREFIX))) refreshSoon();
});
for (const event of [chrome.tabs.onCreated, chrome.tabs.onRemoved, chrome.tabs.onMoved, chrome.tabs.onAttached,
  chrome.tabs.onActivated, chrome.windows.onCreated, chrome.windows.onRemoved]) {
  event.addListener(refreshSoon);
}
chrome.tabs.onUpdated.addListener((_id, change) => {
  if (change.url || change.title || change.status === 'complete' || 'pinned' in change || 'discarded' in change) refreshSoon();
});

await loadSettings();
await refresh({ live: true });
renderSaved();
setUpAi();
