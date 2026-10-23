// Runs in every page. Reports the page's metadata and summary, plus whether the
// user has scrolled it and copied, cut or pasted anything, to the service worker.
(() => {
  // Each injection takes over from any earlier one (e.g. after the extension
  // reloads), so events are never counted twice.
  const instance = Symbol('tab-ledger');
  window.__tabLedgerInstance = instance;
  const current = () => window.__tabLedgerInstance === instance;

  const MAX_TEXT = 20000;      // characters of page text used for the summary
  const MAX_EXCERPT = 6000;    // characters kept for on-device AI summaries
  const CLIPBOARD_PREVIEW = 120;
  const CLIPBOARD_HISTORY = 10;
  const USER_INPUT_WINDOW_MS = 1500;

  let recordClipboardText = false;
  let activity = freshActivity();
  let page = null;
  let lastUrl = withoutHash(location.href);
  let lastInputAt = 0;
  let pushTimer = null;

  function freshActivity() {
    return {
      scrolled: false,
      scrollEvents: 0,
      maxScrollPercent: scrollPercent(document.scrollingElement),
      firstScrollAt: null,
      lastScrollAt: null,
      copies: 0,
      cuts: 0,
      pastes: 0,
      lastCopyAt: null,
      lastPasteAt: null,
      clipboard: [],
    };
  }

  function scrollPercent(el) {
    if (!el) return 0;
    const range = el.scrollHeight - el.clientHeight;
    if (range <= 0) return 100;
    return Math.min(100, Math.round(((el.scrollTop + el.clientHeight) / el.scrollHeight) * 100));
  }

  // ---- settings ----------------------------------------------------------

  chrome.storage.local.get('settings').then(({ settings }) => {
    recordClipboardText = !!settings?.recordClipboardText;
  }).catch(() => {});
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.settings) {
      recordClipboardText = !!changes.settings.newValue?.recordClipboardText;
    }
  });

  // ---- scrolling ---------------------------------------------------------

  // Only count scrolls that follow real input, not the browser restoring the
  // scroll position or the page scrolling itself.
  const markInput = () => { lastInputAt = Date.now(); };
  for (const type of ['wheel', 'touchmove', 'keydown', 'mousedown', 'pointerdown']) {
    window.addEventListener(type, markInput, { capture: true, passive: true });
  }

  document.addEventListener('scroll', (event) => {
    if (!current()) return;
    if (Date.now() - lastInputAt > USER_INPUT_WINDOW_MS) return;
    const el = event.target === document ? document.scrollingElement : event.target;
    const now = new Date().toISOString();
    activity.scrolled = true;
    activity.scrollEvents++;
    activity.firstScrollAt ||= now;
    activity.lastScrollAt = now;
    if (el === document.scrollingElement || isLargeScroller(el)) {
      activity.maxScrollPercent = Math.max(activity.maxScrollPercent, scrollPercent(el));
    }
    schedulePush();
  }, { capture: true, passive: true });

  // Apps like webmail scroll an inner panel rather than the document.
  function isLargeScroller(el) {
    return el instanceof Element && el.clientHeight > window.innerHeight * 0.5;
  }

  // ---- clipboard ---------------------------------------------------------

  function onClipboard(kind, event) {
    if (!current()) return;
    const now = new Date().toISOString();
    if (kind === 'paste') {
      activity.pastes++;
      activity.lastPasteAt = now;
    } else {
      activity[kind === 'cut' ? 'cuts' : 'copies']++;
      activity.lastCopyAt = now;
    }
    if (recordClipboardText) {
      const text = kind === 'paste'
        ? event.clipboardData?.getData('text/plain') || ''
        : selectedText();
      activity.clipboard.unshift({ kind, at: now, text: clip(text, CLIPBOARD_PREVIEW) });
      activity.clipboard.length = Math.min(activity.clipboard.length, CLIPBOARD_HISTORY);
    }
    schedulePush();
  }

  function selectedText() {
    const el = document.activeElement;
    if (el && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && typeof el.selectionStart === 'number'))) {
      return el.value.slice(el.selectionStart, el.selectionEnd);
    }
    return String(window.getSelection() || '');
  }

  document.addEventListener('copy', (e) => onClipboard('copy', e), true);
  document.addEventListener('cut', (e) => onClipboard('cut', e), true);
  document.addEventListener('paste', (e) => onClipboard('paste', e), true);

  // ---- page metadata and summary ----------------------------------------

  function meta(selector) {
    return document.querySelector(selector)?.getAttribute('content')?.trim() || null;
  }

  function mainText() {
    const root = document.querySelector('article, main, [role="main"]') || document.body;
    if (!root) return '';
    const parts = [];
    let length = 0;
    for (const el of root.querySelectorAll('h1, h2, h3, p, li, blockquote, dd')) {
      if (el.closest('nav, header, footer, aside, form, [aria-hidden="true"], [hidden]')) continue;
      if (el.querySelector('p, li')) continue;  // keep only the innermost blocks
      const text = el.innerText?.replace(/\s+/g, ' ').trim();
      if (!text || text.length < 20) continue;
      // Headings and list items often lack a full stop; end them so they stay separate sentences.
      parts.push(/[.!?]$/.test(text) ? text : text + '.');
      length += text.length;
      if (length > MAX_TEXT) break;
    }
    let text = parts.join('\n');
    if (text.length < 200) text = (root.innerText || '').slice(0, MAX_TEXT);
    return text;
  }

  function buildPage() {
    const text = mainText();
    const words = text.match(/\S+/g)?.length || 0;
    const description = meta('meta[name="description"]') || meta('meta[property="og:description"]');
    return {
      title: document.title,
      description,
      siteName: meta('meta[property="og:site_name"]'),
      type: meta('meta[property="og:type"]'),
      image: meta('meta[property="og:image"]'),
      author: meta('meta[name="author"]') || meta('meta[property="article:author"]'),
      keywords: meta('meta[name="keywords"]'),
      published: meta('meta[property="article:published_time"]'),
      canonical: document.querySelector('link[rel="canonical"]')?.href || null,
      language: document.documentElement.lang || null,
      headings: [...document.querySelectorAll('h1, h2')]
        .map((h) => h.innerText?.replace(/\s+/g, ' ').trim())
        .filter(Boolean)
        .slice(0, 6),
      wordCount: words,
      readingMinutes: Math.max(1, Math.round(words / 230)),
      summary: globalThis.TabLedgerSummarize.summarize(text, 3),
      excerpt: text.slice(0, MAX_EXCERPT),
      builtAt: new Date().toISOString(),
    };
  }

  function clip(text, n) {
    text = String(text).replace(/\s+/g, ' ').trim();
    return text.length > n ? text.slice(0, n - 1) + '…' : text;
  }

  // ---- reporting ---------------------------------------------------------

  function record() {
    return { url: location.href, page, activity, updatedAt: new Date().toISOString() };
  }

  function push() {
    pushTimer = null;
    if (!current()) return;
    try {
      chrome.runtime.sendMessage({ type: 'tab-ledger:update', record: record() }).catch(() => {});
    } catch {
      // The extension was reloaded or removed; this copy of the script is orphaned.
    }
  }

  function schedulePush() {
    if (!pushTimer) pushTimer = setTimeout(push, 1000);
  }

  function refreshPage() {
    if (!current()) return;
    page = buildPage();
    schedulePush();
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== 'tab-ledger:collect' || !current()) return;
    page = buildPage();
    schedulePush();
    sendResponse(record());
  });

  // Single-page apps change the URL without reloading; treat that as a new page.
  function withoutHash(url) {
    return url.split('#')[0];
  }

  function checkUrl() {
    const url = withoutHash(location.href);
    if (url === lastUrl) return;
    lastUrl = url;
    activity = freshActivity();
    setTimeout(refreshPage, 1500);
  }
  try {
    window.navigation?.addEventListener('navigatesuccess', checkUrl);
  } catch { /* Navigation API not available */ }
  window.addEventListener('popstate', checkUrl);
  window.addEventListener('hashchange', checkUrl);

  refreshPage();
  // Pages that render client-side often have little text at document_idle.
  setTimeout(refreshPage, 4000);
})();
