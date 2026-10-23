// Builds a script that reopens the windows and tabs in a snapshot.
// Pure functions, no chrome.* calls, so they can be tested in Node.

export const PLATFORMS = {
  sh: { label: 'macOS / Linux (.sh)', ext: 'sh', mime: 'text/x-shellscript' },
  ps1: { label: 'Windows PowerShell (.ps1)', ext: 'ps1', mime: 'text/plain' },
  bat: { label: 'Windows batch (.bat)', ext: 'bat', mime: 'application/x-bat' },
};

// cmd.exe lines are limited to 8191 characters; keep each command well under that.
const WINDOWS_COMMAND_CHARS = 6000;

export function relaunchUrls(window) {
  return window.tabs
    .map((t) => t.url || t.pendingUrl || '')
    .filter((u) => u && !u.startsWith('chrome-extension://') && !u.startsWith('devtools://'))
    // A literal double quote would end the argument early; URLs normally have it percent-encoded.
    .map((u) => u.replace(/"/g, '%22'));
}

export function buildRelaunchScript(snapshot, platform) {
  const info = PLATFORMS[platform];
  if (!info) throw new Error(`Unknown platform: ${platform}`);
  const windows = snapshot.windows
    .map((w) => ({ ...w, urls: relaunchUrls(w) }))
    .filter((w) => w.urls.length);
  const content = { sh: shScript, ps1: ps1Script, bat: batScript }[platform](snapshot, windows);
  return {
    filename: `relaunch-tabs-${stamp(snapshot.capturedAt)}.${info.ext}`,
    content,
    mime: info.mime,
  };
}

function stamp(iso) {
  return new Date(iso).toISOString().slice(0, 16).replace('T', '_').replace(':', '');
}

function header(snapshot, windows, comment) {
  const tabs = windows.reduce((n, w) => n + w.urls.length, 0);
  return [
    `${comment} Reopens ${windows.length} Chrome window(s) with ${tabs} tab(s).`,
    `${comment} Captured by Tab Ledger on ${snapshot.capturedAt}.`,
  ];
}

function titleLines(window, comment, clean = (s) => s) {
  return window.tabs
    .filter((t) => t.url || t.pendingUrl)
    .map((t) => `${comment}   - ${clean(oneLine(t.title || t.url || t.pendingUrl))}`);
}

function oneLine(s) {
  s = String(s).replace(/[\r\n\t]+/g, ' ').trim();
  return s.length > 100 ? s.slice(0, 99) + '…' : s;
}

function windowLabel(w, i) {
  return `Window ${i + 1}: ${w.urls.length} tab(s)${w.incognito ? ', incognito' : ''}`;
}

// ---- macOS / Linux -------------------------------------------------------

function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function shScript(snapshot, windows) {
  const lines = [
    '#!/usr/bin/env bash',
    ...header(snapshot, windows, '#'),
    '# Run with:  bash this-file.sh   (or chmod +x it and run it directly)',
    '# Set CHROME to a browser binary to override the one found below.',
    '',
    'set -u',
    '',
    'if [ "$(uname)" = Darwin ]; then',
    '  CHROME_APP=${CHROME_APP:-Google Chrome}',
    'else',
    '  CHROME=${CHROME:-}',
    '  if [ -z "$CHROME" ]; then',
    '    for c in google-chrome google-chrome-stable chromium chromium-browser; do',
    '      if command -v "$c" >/dev/null 2>&1; then CHROME=$c; break; fi',
    '    done',
    '  fi',
    '  if [ -z "$CHROME" ]; then echo "Chrome not found; set CHROME=/path/to/chrome" >&2; exit 1; fi',
    'fi',
    '',
    '# open_window normal|incognito URL...',
    'open_window() {',
    '  local flags=(--new-window)',
    '  [ "$1" = incognito ] && flags+=(--incognito)',
    '  shift',
    '  if [ "$(uname)" = Darwin ]; then',
    '    open -na "$CHROME_APP" --args "${flags[@]}" "$@"',
    '  else',
    '    "$CHROME" "${flags[@]}" "$@" >/dev/null 2>&1 &',
    '  fi',
    '  # Give Chrome a moment so each window opens separately.',
    '  sleep 2',
    '}',
  ];
  windows.forEach((w, i) => {
    lines.push('', `# ${windowLabel(w, i)}`, ...titleLines(w, '#'));
    lines.push(`open_window ${w.incognito ? 'incognito' : 'normal'} \\`);
    lines.push(w.urls.map((u) => `  ${shQuote(u)}`).join(' \\\n'));
  });
  lines.push('');
  return lines.join('\n');
}

// Splits a window's URLs into groups that each fit on one command line.
function chunks(urls, maxChars) {
  const out = [[]];
  let size = 0;
  for (const u of urls) {
    const cost = u.length + 3;
    if (out[out.length - 1].length && size + cost > maxChars) {
      out.push([]);
      size = 0;
    }
    out[out.length - 1].push(u);
    size += cost;
  }
  return out;
}

// ---- Windows PowerShell ----------------------------------------------------

function psQuote(s) {
  return `'${String(s).replace(/'/g, "''")}'`;
}

function ps1Script(snapshot, windows) {
  const lines = [
    ...header(snapshot, windows, '#'),
    '# Run with:  powershell -ExecutionPolicy Bypass -File this-file.ps1',
    '# Set $env:CHROME to a browser executable to override the one found below.',
    '',
    '$chrome = $env:CHROME',
    'if (-not $chrome) {',
    '  $candidates = @(',
    '    "$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe",',
    '    "${env:ProgramFiles(x86)}\\Google\\Chrome\\Application\\chrome.exe",',
    '    "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"',
    '  )',
    '  $chrome = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1',
    '}',
    "if (-not $chrome) { $chrome = 'chrome.exe' }",
    '',
    '# The first group of URLs opens a new window; later groups add tabs to it.',
    'function Open-Tabs([string[]]$Urls, [switch]$NewWindow, [switch]$Incognito) {',
    '  $argList = @()',
    "  if ($NewWindow) { $argList += '--new-window' }",
    "  if ($Incognito) { $argList += '--incognito' }",
    "  $argList += $Urls | ForEach-Object { '\"' + $_ + '\"' }",
    '  Start-Process -FilePath $chrome -ArgumentList $argList',
    '  Start-Sleep -Seconds 2',
    '}',
  ];
  windows.forEach((w, i) => {
    lines.push('', `# ${windowLabel(w, i)}`, ...titleLines(w, '#'));
    chunks(w.urls, WINDOWS_COMMAND_CHARS).forEach((group, j) => {
      const flags = [j === 0 ? ' -NewWindow' : '', w.incognito ? ' -Incognito' : ''].join('');
      lines.push(`Open-Tabs${flags} -Urls @(`);
      lines.push(group.map((u) => `  ${psQuote(u)}`).join(',\n'));
      lines.push(')');
    });
  });
  lines.push('');
  // A byte-order mark makes Windows PowerShell 5 read the file as UTF-8.
  return '﻿' + lines.join('\r\n').replace(/(?<!\r)\n/g, '\r\n');
}

// ---- Windows batch ---------------------------------------------------------

function batUrl(u) {
  return `"${u.replace(/%/g, '%%')}"`;
}

// Comments in a batch file still go through the parser; keep them to safe characters.
function batClean(s) {
  return s.replace(/[^A-Za-z0-9 .,:;/_'()\-]/g, '').trim() || '(untitled)';
}

function batScript(snapshot, windows) {
  const lines = [
    '@echo off',
    ...header(snapshot, windows, 'rem').map(batClean).map((l) => 'rem ' + l.replace(/^rem\s*/, '')),
    'rem Double-click to run. Set CHROME to a browser executable to override the one found below.',
    '',
    'if not defined CHROME set "CHROME=chrome"',
    'if "%CHROME%"=="chrome" if exist "%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME=%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe"',
    'if "%CHROME%"=="chrome" if exist "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME=%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe"',
    'if "%CHROME%"=="chrome" if exist "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME=%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"',
  ];
  windows.forEach((w, i) => {
    lines.push('', `rem ${windowLabel(w, i)}`, ...titleLines(w, 'rem', batClean));
    chunks(w.urls, WINDOWS_COMMAND_CHARS).forEach((group, j) => {
      const flags = [j === 0 ? '--new-window' : '', w.incognito ? '--incognito' : ''].filter(Boolean).join(' ');
      lines.push(`start "" "%CHROME%" ${flags ? flags + ' ' : ''}${group.map(batUrl).join(' ')}`);
      lines.push('timeout /t 2 /nobreak >nul');
    });
  });
  lines.push('');
  return lines.join('\r\n');
}
