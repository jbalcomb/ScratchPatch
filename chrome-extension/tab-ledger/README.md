# Tab Ledger (Chrome extension)

Captures every open Chrome window and tab, summarizes each page, tracks whether you scrolled each page and
whether you copied or pasted anything in it, and creates a script that reopens all of them. It can then close
the windows for you.

## Install

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick this `tab-ledger` folder.
3. Click the Tab Ledger toolbar button (pin it from the puzzle-piece menu) to open the dashboard.

Tabs that were already open are picked up when the extension is installed. To include incognito windows,
turn on **Allow in Incognito** on the extension's details page; for `file://` pages, turn on **Allow access
to file URLs**.

## What it records

| For each | Recorded |
|---|---|
| Window | Type, state (normal / maximized / minimized / fullscreen), size and position, focused, incognito |
| Tab | Title, address, favicon, active, pinned, playing audio / muted, sleeping (discarded), tab group, last viewed |
| Page | Description, site name, author, published date, language, keywords, canonical address, main headings, word count and reading time |
| Summary | The page's own description plus the three sentences that best represent its text (computed on your computer). **Summarize with Chrome AI** adds a short summary from the model built into Chrome 138+, on devices that support it. |
| Scrolling | Whether you scrolled, how far down you got (%), first and last scroll time. Only scrolling you did counts, not the browser restoring a position. |
| Clipboard | How many times you copied, cut and pasted, and when. Turn on the setting to also keep a 120-character preview of the last 10 items (off by default). |

Everything stays in Chrome's local extension storage; nothing is sent anywhere. Pages Chrome does not let
extensions read (`chrome://` pages, the Web Store, the PDF viewer) show their title and address only.

## Relaunch script

Pick a platform and click **Create relaunch script**:

- **macOS / Linux (.sh)** — run `bash relaunch-tabs-….sh`. Uses Google Chrome on macOS; on Linux it looks
  for `google-chrome`, `chromium`, … (set `CHROME=/path/to/browser` to choose).
- **Windows (.bat)** — double-click it.
- **Windows PowerShell (.ps1)** — `powershell -ExecutionPolicy Bypass -File relaunch-tabs-….ps1`.

Each window reopens as its own window with its tabs in order, incognito windows as incognito.

Tick **Close these windows and tabs after creating the script** to also save a snapshot and close everything
except the dashboard once the script is downloaded.

**Saved snapshots** keep a copy you can view, export as JSON, turn into a script later, or **Reopen in
Chrome** directly, which also restores pinned tabs and window sizes.

## Develop

```bash
npm test                         # summarizer and script generator (Node 20+, no dependencies)
node tools/measure-layout.cjs layout   # re-measure every data-ui element (needs playwright)
```

### Layout workflow

The [UI spec](https://claude.ai/artifact/Fxf9QBmvYvRRk1hVSfpkdh) has a **Layout measurements** tab: one row per
dashboard control with its x, y, width and height at 1280 × 800. That table is the layout spec. To change the
layout, edit the table (or its Δ columns), then: compare it with `layout/measured.json` to find the changed rows,
change the code, re-measure into `layout/`, and write the new numbers and screenshot back to the doc.

| File | Role |
|---|---|
| `content.js` | Runs in each page: metadata, summary, scroll and clipboard tracking |
| `lib/summarize.js` | Extractive summarizer used by the content script |
| `background.js` | Stores each tab's latest report; opens the dashboard |
| `dashboard.*` | The dashboard page |
| `lib/capture.js` | Capture, save, restore and close windows |
| `lib/relaunch.js` | Builds the `.sh`, `.ps1` and `.bat` scripts |
