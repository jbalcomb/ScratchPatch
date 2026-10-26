import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const context = {};
vm.runInNewContext(readFileSync(new URL('../lib/summarize.js', import.meta.url), 'utf8'), context);
// Arrays from the vm context have a different prototype; copy them into this realm.
const summarize = (...args) => [...context.TabLedgerSummarize.summarize(...args)];
const splitSentences = (...args) => [...context.TabLedgerSummarize.splitSentences(...args)];

const ARTICLE = `
Solar panels convert sunlight into electricity using photovoltaic cells made of silicon.
The efficiency of solar panels has improved steadily, and modern panels convert over twenty percent of sunlight.
Click here to subscribe to our newsletter for weekly updates and offers.
Installing solar panels on a roof reduces electricity bills and lowers carbon emissions over time.
Battery storage lets households keep solar electricity for use at night when panels produce nothing.
Cookies help us deliver our services and by using them you agree to our policy.
Government incentives in many countries make solar panels and battery storage cheaper to install.
`;

test('picks the sentences about the main topic, in page order', () => {
  const picked = summarize(ARTICLE, 3);
  assert.equal(picked.length, 3);
  for (const s of picked) assert.match(s, /solar|panels|battery/i);
  const positions = picked.map((s) => ARTICLE.indexOf(s));
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});

test('returns short texts as they are', () => {
  assert.deepEqual(summarize('This is a single sentence that is long enough to keep.', 3),
    ['This is a single sentence that is long enough to keep.']);
  assert.deepEqual(summarize('', 3), []);
});

test('skips near-duplicate sentences', () => {
  const text = Array(5).fill('The quick brown fox jumps over the lazy sleeping dog today.').join(' ') +
    ' A completely different sentence about astronomy and distant galaxies follows here.' +
    ' Another unrelated line concerning cooking pasta with fresh tomatoes and basil.' +
    ' One more distinct remark regarding mountain hiking trails in the springtime.';
  const picked = summarize(text, 3);
  assert.equal(picked.filter((s) => s.includes('quick brown fox')).length, 1);
});

test('splits on sentence punctuation and line breaks', () => {
  assert.deepEqual(splitSentences('One. Two! Three?\nFour'), ['One.', 'Two!', 'Three?', 'Four']);
});
