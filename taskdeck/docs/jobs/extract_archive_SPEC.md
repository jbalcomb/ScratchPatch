# Extract Archive: spec (draft, not built)

Unpack zip, 7-Zip, RAR (and tar/gz) archives on the phone, into a folder on the phone, Google Drive or
Dropbox, while TaskDeck stays usable for other jobs.

Status: **steps 1–2 built** (7-Zip engine; phone extraction with browser, destination picker, background
progress). Steps 3–4 (Dropbox, Google Drive) not started. Decisions are in §5.

---

## 1. What has to be true for this to work (findings)

### 1.1 Formats: 7-Zip's core library

Password-protected 7z and RAR are required, which rules out libarchive (it can't decrypt either) and
py7zr (native dependencies not built for Android). So TaskDeck uses **7-Zip's own core library**
(the open-source engine behind 7-Zip; LGPL, plus the unRAR clause that only forbids creating RAR files).
It reads every format we want, with passwords:

| Format | Password-protected |
|---|---|
| .zip | ZipCrypto and AES |
| .7z | AES |
| .rar (RAR4, RAR5) | yes (incl. encrypted file names) |
| .tar, .gz/.tgz, .bz2, .xz | n/a |

How it is called: the library exposes C++ (COM-style) interfaces, not a C API, so we add a **small C++
shim** with a plain C API (open archive with optional password; list entries with sizes; extract to a
folder, one entry at a time, with progress and cancel callbacks; clear errors for wrong password /
damaged archive). Python calls the shim through `ctypes`. 7-Zip + shim are compiled for Android arm64 with
the NDK in the APK workflow (**new APK**).

**To prove first (small test build):** how the compiled `.so` gets into Flet's APK. Plan A: package it as
a Python wheel built with Flet's mobile-forge tooling and add it as a dependency. Plan B: a small custom Flet
extension that ships it as a native library, located at runtime and loaded with `ctypes`.

Zip/tar could also be done with Python's stdlib, but one engine for everything keeps behaviour (overwrite,
progress, errors, safety checks) identical across formats.

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
  be our own native plugin. **Decided:** v1 without it ("keep TaskDeck open or recent").
* A finished/failed extraction is announced inside TaskDeck. A system notification needs a native plugin too
  (later, with the foreground service).

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

**Decided (Drive): `drive.file` now, full Drive planned.** v1 extracts into a new folder TaskDeck creates in
My Drive (or one it created before); you move it in Drive if needed. **Planned (b):** full Drive browsing
needs the restricted `drive` scope, which device sign-in doesn't allow: a native Google Sign-In plugin plus
Google's security review for restricted scopes. To keep that a drop-in change, the Drive destination is
written against the "destination" interface (list folders / make folder / upload) with the access level
as a setting, so (b) only changes the sign-in and the folder list.

**Decided (Dropbox): Full Dropbox** (browse and pick any folder). Dropbox app created with "Full Dropbox"
access, permissions files.content.write + files.metadata.read.

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
      → runs in background (progress on the job screen and on Home) → Done: Open folder
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
│ Password: [__________]           │
│ (only for encrypted archives)    │
│ Existing files are replaced.     │
│                                  │
│ [ Extract ]                      │
└──────────────────────────────────┘
```

* **Existing files are overwritten** (decided), stated on the screen so it's never a surprise. No other
  conflict options in v1.
* **Password**: the field appears when the quick scan finds encryption (7z/RAR with encrypted names can't be
  listed until the password is entered: then the card says "Encrypted archive: enter the password to see
  its contents"). Wrong password: "Wrong password." right under the field.
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
│ [Open folder]                    │
│ 3 files replaced                 │
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
* **Google Drive (v1, `drive.file`):** the tab shows "My Drive" with only the folders TaskDeck created, and a
  note: "TaskDeck can only see folders it created. New folders are made in My Drive." (With full access,
  planned, it browses normally.)
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

## 4. Build order

1. **Prove the packaging**: 7-Zip core + C shim compiled for Android, loaded on the phone, lists an archive
   (test APK). Decides plan A or B in §1.1.
2. Background tasks (core) + phone browser + All files access + extraction of every format via the shim,
   with passwords and overwrite (new APK).
3. Connected accounts + Dropbox destination, Full Dropbox (live update).
4. Google Drive destination, `drive.file` (needs your OAuth client setup first; live update).
5. Later: full Drive access (native Google Sign-In + Google review); foreground service + system
   notifications.

## 5. Decisions

1. Password-protected 7z/RAR: **needed** → 7-Zip core library + C shim (§1.1).
2. Leaving TaskDeck during long extractions: **v1 without a foreground service.**
3. Google Drive: **`drive.file` now, plan for full Drive** (§1.4).
4. Dropbox: **Full Dropbox.**
5. Existing files: **overwrite.**
6. Delete archive afterwards: **no.**
7. "Open with TaskDeck" from other apps: **no.**
