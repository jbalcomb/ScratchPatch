import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildRelaunchScript, relaunchUrls } from '../lib/relaunch.js';

const snapshot = {
  capturedAt: '2026-10-22T18:05:00.000Z',
  windows: [
    { incognito: false, tabs: [
      { title: 'Search: "tabs" & more', url: 'https://example.com/search?q=a&b=50%25' },
      { title: "It's quoted", url: "https://example.com/it's" },
      { title: 'Dashboard', url: 'chrome-extension://abc/dashboard.html' },
    ] },
    { incognito: true, tabs: [{ title: 'Private', url: 'https://private.example/' }] },
    { incognito: false, tabs: [{ title: 'Only the dashboard', url: 'chrome-extension://abc/dashboard.html' }] },
  ],
};

test('leaves out extension pages and empty windows', () => {
  assert.deepEqual(relaunchUrls(snapshot.windows[0]), [
    'https://example.com/search?q=a&b=50%25',
    "https://example.com/it's",
  ]);
  const { content } = buildRelaunchScript(snapshot, 'sh');
  assert.equal((content.match(/^open_window /gm) || []).length, 2);
});

test('shell script passes each URL to Chrome unchanged, one window per call', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tab-ledger-'));
  const fake = join(dir, 'fake-chrome');
  const log = join(dir, 'calls.log');
  // Records its arguments, one per line, with a separator per call.
  writeFileSync(fake, `#!/usr/bin/env bash\nfor a in "$@"; do echo "$a" >> "${log}"; done\necho --- >> "${log}"\n`);
  chmodSync(fake, 0o755);
  const { content, filename } = buildRelaunchScript(snapshot, 'sh');
  assert.equal(filename, 'relaunch-tabs-2026-10-22_1805.sh');
  const script = join(dir, filename);
  // No real waiting in the test.
  writeFileSync(script, content.replace('sleep 2', 'wait'));
  execFileSync('bash', [script], { env: { ...process.env, CHROME: fake } });
  const calls = readFileSync(log, 'utf8').split('---\n').filter(Boolean).map((c) => c.trim().split('\n'));
  assert.deepEqual(calls[0], ['--new-window', 'https://example.com/search?q=a&b=50%25', "https://example.com/it's"]);
  assert.deepEqual(calls[1], ['--new-window', '--incognito', 'https://private.example/']);
  assert.equal(calls.length, 2);
});

test('PowerShell script quotes single quotes and uses CRLF', () => {
  const { content } = buildRelaunchScript(snapshot, 'ps1');
  assert.ok(content.startsWith('﻿'));
  assert.ok(!/[^\r]\n/.test(content));
  assert.match(content, /'https:\/\/example\.com\/it''s'/);
  assert.match(content, /Open-Tabs -NewWindow -Incognito -Urls @\(\r\n  'https:\/\/private\.example\/'\r\n\)/);
});

test('batch script escapes percent signs and keeps comments safe', () => {
  const { content } = buildRelaunchScript(snapshot, 'bat');
  assert.match(content, /start "" "%CHROME%" --new-window "https:\/\/example\.com\/search\?q=a&b=50%%25" "https:\/\/example\.com\/it's"/);
  assert.match(content, /--new-window --incognito "https:\/\/private\.example\/"/);
  assert.match(content, /rem   - Search: tabs  more/);
});

test('splits a very large window across several commands on Windows', () => {
  const tabs = Array.from({ length: 200 }, (_, i) => ({ title: `t${i}`, url: `https://example.com/${'x'.repeat(60)}/${i}` }));
  const { content } = buildRelaunchScript({ capturedAt: snapshot.capturedAt, windows: [{ tabs }] }, 'bat');
  const starts = content.split('\r\n').filter((l) => l.startsWith('start '));
  assert.ok(starts.length > 1);
  assert.match(starts[0], /--new-window/);
  assert.doesNotMatch(starts[1], /--new-window/);
  for (const l of starts) assert.ok(l.length < 8000);
  assert.equal(starts.join(' ').match(/https:/g).length, 200);
});
