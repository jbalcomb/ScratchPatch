export const TAB_KEY_PREFIX = 'tab:';
export const CONTENT_SCRIPT_FILES = ['lib/summarize.js', 'content.js'];

// Pages Chrome lets an extension script into (not chrome://, the Web Store, PDFs, …).
export function canInject(tab) {
  return /^(https?|file):/.test(tab.url || '') && !tab.discarded
    && !/^https:\/\/chromewebstore\.google\.com\//.test(tab.url);
}

export function withoutHash(url) {
  return String(url || '').split('#')[0];
}
