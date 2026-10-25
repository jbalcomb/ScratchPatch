# Extract Archive: spec (draft, not built)

Unpack zip, 7-Zip, RAR (and tar/gz) archives on the phone, into a folder on the phone, Google Drive or
Dropbox, while TaskDeck stays usable for other jobs.

Status: **proposal for review.** Nothing here is implemented. Open decisions are marked **DECIDE**.

---

## 1. What has to be true for this to work (findings)

### 1.1 Formats

| Format | How | Ships as | Notes |
|---|---|---|---|
| .zip | Python `zipfile` (stdlib) | live update | Deflate, bzip2, LZMA. Password (ZipCrypto) works, slow but fine. AES-encrypted zip needs `pyzipper` + `pycryptodomex` (Flet has an Android build of pycryptodomex). |
| .tar, .tar.gz/.tgz, .tar.bz2, .tar.xz, .gz, .bz2, .xz | Python `tarfile` / `gzip` / `bz2` / `lzma` (stdlib) | live update | |
| .7z | **libarchive** (C library) via `libarchive-c` (pure-Python binding) | **new APK** | `py7zr` is not an option: it needs pyppmd, pybcj, inflate64 and zstd, none built for Android (checked Flet's mobile-forge recipes and PyPI). |
| .rar (RAR4, RAR5) | **libarchive** | **new APK** | There is no Python RAR decompressor. libarchive reads most RAR4/RAR5. |

**libarchive is the one native piece.** It covers 7z and RAR (and every format above), but it is not in
Flet's Android package set, so we build it ourselves: a mobile-forge recipe (or NDK CMake build) in CI that
produces an Android arm64 wheel with `libarchive.so`, added to the APK like the fingerprint plugins.
This is the biggest single task in the project.

Known gaps even with libarchive: **encrypted 7z and encrypted RAR are not supported** (libarchive can list
them but not decrypt). Multi-volume RAR/7z (`.part1.rar`, `.7z.001`): libarchive handles some cases; treat
as "later". **DECIDE**: do you need password-protected 7z/RAR? If yes, that is a second native library
(RARLAB's unrar source, and 7-Zip's) and much more work.

### 1.2 Reaching files on the phone

* Android's own file picker (the "Files" UI with Drive/Dropbox on the side) hands back `content://` links,
  not file paths. Python can't write into those without native code, and the picker copies a chosen file
  into the app's cache first (a 4 GB archive = 4 GB copied before anything happens).
* So TaskDeck gets its **own browser** (spec §3) over phone storage, using Android's **"All files access"**
  permission (`MANAGE_EXTERNAL_STORAGE`, granted once by the user in Android Settings). Fine for an
  app installed from our release page; it would need justification on the Play Store.
* Needs the `flet-permission-handler` plugin (exists for Flet 1.0.4) → part of the same **new APK**.

### 1.3 Running in the background

* Inside TaskDeck: extraction runs in a worker thread; you can go back, run other jobs, come back. This needs
  a new core piece: **background tasks** (today a job's run belongs to its screen).
* If you switch to another app, Android usually lets TaskDeck finish, but may stop it after a while or under
  memory pressure, and may pause network (uploads) when the screen is off. The guaranteed fix is an Android
  **foreground service** (the "TaskDeck is extracting…" notification). There is no Flet plugin for it; it would
  be our own native plugin. **DECIDE**: accept "keep TaskDeck open or recent" for v1, or build the service.
* A finished/failed extraction is announced inside TaskDeck. A system notification needs a native plugin too
  (same decision).

### 1.4 Google Drive and Dropbox: yes, new credentials code

**Why the existing Google key doesn't work:** the Daily Log uses a *service account*. Service accounts have no
storage of their own, so they can't create files in your My Drive. Writing to your Drive needs **you** to sign
in (OAuth).

| | Google Drive | Dropbox |
|---|---|---|
| Sign-in without a native plugin | **Device sign-in**: TaskDeck shows a short code; you open google.com/device, enter it, approve. Pure Python. | **Code paste**: TaskDeck opens Dropbox in the browser, you approve, Dropbox shows a code, you paste it back. Pure Python (PKCE, no app secret). |
| Access we'd get | Device sign-in only allows the **`drive.file`** scope (to verify): TaskDeck can create folders/files and see only what it created. It **can't browse your existing Drive folders.** | Full browsing (if the Dropbox app is created with "Full Dropbox" access) or only `Apps/TaskDeck` ("App folder"). |
| One-time setup by you | In the `scratchpatch` Cloud project: OAuth consent screen + an OAuth client of type "TVs and Limited Input devices". Publish the consent screen "In production" (drive.file is a non-sensitive scope, so no Google review), otherwise sign-ins expire after 7 days. | Create an app in the Dropbox App Console (Scoped access; Full Dropbox or App folder; permissions files.content.write + files.metadata.read). Copy the app key. |
| Uploads | Resumable upload; any size. | Single call up to 150 MB, upload sessions above that. |

**DECIDE (Drive):** accept `drive.file` (extract into a new folder TaskDeck creates in My Drive, or a folder
TaskDeck created before; you can move it in Drive afterwards), or get full Drive browsing. Full browsing
needs the restricted `drive` scope, which device sign-in doesn't allow: it means a native Google Sign-In
plugin plus Google's security review for restricted scopes. Recommendation: `drive.file`.

**DECIDE (Dropbox):** Full Dropbox (browse anywhere) or App folder (safer; everything lands in
`Dropbox/Apps/TaskDeck/`). Recommendation: Full Dropbox, since you want to pick destinations.

**New code (shared, not just for this job):**

* `taskdeck.shared.oauth`: device sign-in, PKCE code-paste sign-in, token refresh.
* **Connected accounts**: Settings → Accounts → Connect / Disconnect Google Drive / Dropbox. Tokens are
  app-written credentials (not typed into a job's settings), so the credentials vault gets a small API for
  them. They are stored wherever credentials are stored (encrypted vault; the fingerprint question is
  separate and still open).
* `taskdeck.shared.cloud`: a "destination" interface (list folders, make folder, upload file) with three
  implementations: phone, Google Drive, Dropbox. Any future job that saves files can reuse it.
* Note: this sign-in does **not** replace the Daily Log's service-account key. Device sign-in can't get the
  Sheets scope, so that remains a separate decision (the earlier "option C").

### 1.5 Safety

* Never write outside the chosen folder (reject `../` and absolute paths inside archives).
* Check free space before starting: the archive lists its total unpacked size. Refuse with the numbers if it
  won't fit. Also stop if the unpacked total is wildly out of proportion (zip bomb).
* Extracting to Drive/Dropbox: files are unpacked one at a time into a temp folder, uploaded, then deleted,
  so the phone needs space for the largest single file, not the whole archive.

---

## 2. Flow

```
Pick archive → (quick scan: format, files, size, encrypted?) → Choose destination → Extract
      → runs in background (progress on the job screen and on Home) → Done: Open folder / Share / Delete archive
```

Readiness checks (shown before Extract is allowed, like the Daily Log): All files access granted;
format supported; password entered if needed; enough free space; cloud account connected if a cloud
destination is chosen.

---

## 3. UI spec

Phone-first: one column, 16 px gutters, 48 px minimum touch targets, nothing important below the fold on
a 6" screen, all text fits two lines.

### 3.1 Job screen: "Extract archive"

```
┌──────────────────────────────────┐
│ ←  Extract archive        📌  🔍 │
├──────────────────────────────────┤
│ ARCHIVE                          │
│ ┌──────────────────────────────┐ │
│ │ 🗜 photos-2026.7z            │ │
│ │ 1.8 GB · 7-Zip · 412 files   │ │
│ │ Unpacks to 2.3 GB            │ │
│ │                    [Change…] │ │
│ └──────────────────────────────┘ │
│                                  │
│ EXTRACT TO                       │
│ ┌──────────────────────────────┐ │
│ │ 📁 Phone › Download ›        │ │
│ │    photos-2026  (new folder) │ │
│ │                    [Change…] │ │
│ └──────────────────────────────┘ │
│                                  │
│ ▸ Options                        │
│   If a file already exists:      │
│   (•) Ask  ( ) Replace           │
│   ( ) Skip ( ) Keep both         │
│   Password: [__________] (only   │
│   shown for encrypted archives)  │
│                                  │
│ [ Extract ]                      │
└──────────────────────────────────┘
```

* **Empty state** (no archive yet): one big **[Browse for an archive]** button and a "Recent archives" list
  (last 5, with size and date). Tapping Extract archive from the palette lands here.
* After picking: the quick scan fills in the card (fast: reads only the archive index). If the format isn't
  supported or the archive is damaged, the card says so in red with the reason.
* **EXTRACT TO** defaults to: same folder as the archive, new folder named after the archive (without
  extension). One tap on **Extract** is enough for the common case.
* The readiness box (red, as in the Daily Log) appears above the Extract button when something is missing,
  with the fixing button (e.g. **Allow file access**, **Connect Google Drive**).

### 3.2 While extracting (same screen; you can leave)

```
│ Extracting… 47%                  │
│ ███████████░░░░░░░░░░░           │
│ 194 of 412 files · 1.1 of 2.3 GB │
│ IMG_2041.jpg                     │
│ About 2 min left                 │
│ [Cancel]                         │
│ You can keep using TaskDeck.     │
```

For cloud destinations two lines: "Unpacking…" and "Uploading to Google Drive… 31%".

**Cancel** asks: "Stop extracting? Files already extracted stay in photos-2026." [Keep extracting] [Stop].

**Done:**

```
│ ✓ Extracted 412 files (2.3 GB)   │
│   to Phone › Download › photos-2026
│ [Open folder] [Share] [Delete archive…]
│ 3 files skipped (already there)  │
```

**Failed:** red card with what happened and what to do ("Out of space: needs 2.3 GB, 1.1 GB free. Free up
space or extract to Google Drive." / "Wrong password." / "Archive is damaged at IMG_2210.jpg; 191 files were
extracted."). Retry button where it makes sense.

### 3.3 Running tasks (new, app-wide)

* **Home** shows a strip at the top while anything runs: `🗜 Extracting photos-2026.7z · 47%  ›`. Tap opens
  the job screen with its progress.
* The bottom nav **Home** icon gets a small badge while tasks run.
* When a task finishes while you're elsewhere in TaskDeck: a SnackBar "photos-2026.7z extracted · [Open]".
* Palette: "Running tasks", "Cancel extraction".

### 3.4 Archive browser (pick a file)

```
┌──────────────────────────────────┐
│ ←  Choose an archive         🔍  │
├──────────────────────────────────┤
│ [Downloads] [Phone] [SD card]    │  ← location chips
│ Phone › Download                 │  ← breadcrumb, tap to go up
├──────────────────────────────────┤
│ 📁 Telegram                    › │
│ 📁 WhatsApp                    › │
│ 🗜 photos-2026.7z   1.8 GB  Oct 22│
│ 🗜 manuals.zip      41 MB   Oct 3 │
│ 🗜 backup.rar       610 MB  Sep 30│
├──────────────────────────────────┤
│ ☐ Show all files                 │
└──────────────────────────────────┘
```

* Opens in **Downloads** (where archives usually are). Folders first, then archives, newest first.
* Only archives are listed unless **Show all files** is on (then non-archives are greyed out).
* 🔍 filters the current folder by name.
* First use without permission: a full-screen explainer "TaskDeck needs access to your files to open and
  extract archives." [Allow file access] → Android's settings page → returns and continues.

### 3.5 Destination picker ("Extract to")

Modelled on a normal "Save as" dialog.

```
┌──────────────────────────────────┐
│ ←  Extract to                    │
├──────────────────────────────────┤
│ ☑ Into a new folder named        │
│ [photos-2026________________]    │
│                                  │
│ [📱 Phone] [△ Drive] [⬚ Dropbox] │  ← location tabs
│ Phone › Download                 │  ← breadcrumb
├──────────────────────────────────┤
│ 📁 Documents                   › │
│ 📁 Pictures                    › │
│ 📁 photos-2025                 › │
│ ＋ New folder                     │
├──────────────────────────────────┤
│ Will extract to:                 │
│ Phone › Download › photos-2026/  │
│ [Cancel]              [Use this] │
└──────────────────────────────────┘
```

* **Folder name** is pre-filled with the archive's name and editable. Unticking "Into a new folder" extracts
  straight into the folder being browsed. If the name already exists: inline warning "photos-2026 already
  exists. Files will be merged." with a one-tap suggestion "Use photos-2026 (2)".
* **Location tabs**: Phone (with Downloads / Phone / SD card chips inside), Google Drive, Dropbox, plus
  **Recent** destinations as the first chip when there are any.
* **Not connected** cloud tab: an explainer and **[Connect Google Drive]** / **[Connect Dropbox]** (§3.6),
  then it continues browsing.
* **Google Drive with `drive.file`:** the tab shows "My Drive" with only the folders TaskDeck created, and a
  note: "TaskDeck can only see folders it created. New folders are made in My Drive." (If full Drive access
  is chosen instead, it browses normally.)
* "Will extract to" always shows the full final path; **Use this** returns to the job screen.

### 3.6 Settings → Accounts (new section)

```
│ ACCOUNTS                         │
│ △ Google Drive                   │
│   Not connected        [Connect] │
│ ⬚ Dropbox                        │
│   jim@… · connected [Disconnect] │
```

* **Connect Google Drive:** a dialog shows the code large (e.g. `BQKD-WXTL`) with **[Copy code]** and
  **[Open google.com/device]**; it waits and closes itself when you've approved ("Connected as …").
* **Connect Dropbox:** **[Open Dropbox]** opens the browser; after approving, Dropbox shows a code; the dialog
  has a code field with a **Paste** button and **[Connect]**.
* **Disconnect** forgets the tokens on the phone (and revokes them).

### 3.7 Copy

Plain, short, says what to do: "Needs 2.3 GB, 1.1 GB free." not "Insufficient storage". Errors always end
with the fix.

---

## 4. Build order (proposal)

1. Background tasks (core) + phone browser + zip/tar extraction + All files access. Works with a new APK
   (permission plugin), zip/tar only.
2. libarchive Android build in CI → 7z + RAR (new APK).
3. Connected accounts + Dropbox destination (live update once the accounts code exists).
4. Google Drive destination (needs your OAuth client setup first).
5. Optional: foreground service + system notifications (native plugin, new APK).

## 5. Decisions needed

1. Password-protected 7z/RAR needed? (no → libarchive is enough)
2. Leaving TaskDeck during long extractions: v1 without a foreground service OK?
3. Google Drive: `drive.file` (TaskDeck-created folders only) OK?
4. Dropbox: Full Dropbox or App folder?
5. When a file already exists: default **Ask**? (or Keep both)
6. After extracting: offer **Delete archive** (with confirmation)? Anything else (open folder, share)?
7. Also accept archives **shared to TaskDeck** from other apps ("Open with TaskDeck")? Needs Android intent
   filters (APK change); could ride along with step 1.
