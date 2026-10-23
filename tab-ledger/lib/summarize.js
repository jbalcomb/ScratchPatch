// Extractive summarizer: picks the sentences that best represent a page's text.
// Plain script (not a module) so it can run as a content script; it exposes
// globalThis.TabLedgerSummarize.
(function (root) {
  const STOP_WORDS = new Set((
    'a about above after again against all also am an and any are as at be because been before being below ' +
    'between both but by can could did do does doing down during each few for from further had has have having ' +
    'he her here hers herself him himself his how i if in into is it its itself just me more most my myself no ' +
    'nor not now of off on once only or other our ours ourselves out over own same she should so some such than ' +
    'that the their theirs them themselves then there these they this those through to too under until up very ' +
    'was we were what when where which while who whom why will with would you your yours yourself yourselves ' +
    'may might must shall us get got one two new like use used using via per etc'
  ).split(' '));

  const SENTENCE_RE = /[^.!?。！？\n]+(?:[.!?。！？]+["'”’)\]]*|$)/g;

  function splitSentences(text) {
    return (String(text).match(SENTENCE_RE) || [])
      .map((s) => s.replace(/\s+/g, ' ').trim())
      .filter(Boolean);
  }

  function tokens(text) {
    return (text.toLowerCase().match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || [])
      .filter((w) => w.length > 2 && !STOP_WORDS.has(w));
  }

  // Returns up to maxSentences sentences, in the order they appear in the text.
  function summarize(text, maxSentences = 3) {
    const sentences = splitSentences(text).filter((s) => s.length >= 40 && s.length <= 400);
    if (sentences.length <= maxSentences) return sentences;

    const freq = new Map();
    for (const w of tokens(text)) freq.set(w, (freq.get(w) || 0) + 1);
    const top = Math.max(1, ...freq.values());

    const scored = sentences.map((s, index) => {
      const words = tokens(s);
      if (!words.length) return { s, index, score: 0, words: new Set() };
      const sum = words.reduce((acc, w) => acc + (freq.get(w) || 0) / top, 0);
      const position = index < 3 ? 1.25 : index < 10 ? 1.1 : 1;
      return { s, index, score: (sum / Math.sqrt(words.length)) * position, words: new Set(words) };
    });

    const picked = [];
    for (const cand of scored.slice().sort((a, b) => b.score - a.score)) {
      if (picked.length >= maxSentences) break;
      // Skip near-duplicates of a sentence already chosen.
      const dup = picked.some((p) => overlap(p.words, cand.words) > 0.7);
      if (!dup) picked.push(cand);
    }
    return picked.sort((a, b) => a.index - b.index).map((p) => p.s);
  }

  function overlap(a, b) {
    if (!a.size || !b.size) return 0;
    let shared = 0;
    for (const w of a) if (b.has(w)) shared++;
    return shared / Math.min(a.size, b.size);
  }

  root.TabLedgerSummarize = { summarize, splitSentences };
})(globalThis);
