# Credentials vault

Every job setting marked `secret=True` (API keys, the Google service-account key, ...) is a credential.
TaskDeck can keep credentials out of `state.json` in a vault that your **fingerprint** unlocks.

**Optional, off by default**: Settings → Credentials → **Fingerprint lock**. Turning it on asks for the
fingerprint and moves the credentials in; turning it off asks for the fingerprint too and moves them back
into `state.json`. Exception to the default: if the vault already holds credentials (moved in before the
switch existed), it stays on until switched off.

## How it works

* **Storage**: [flet-secure-storage](https://pypi.org/project/flet-secure-storage/) with
  `enforce_biometrics=True` and the AES-GCM key cipher. The AES key lives in the Android keystore
  (hardware-backed on most phones) and is created with "user authentication required", so Android only
  lets the app use it after a fingerprint or the phone PIN. Without one, the stored data can't be decrypted,
  not even from a backup or by another app.
* **Unlock**: the first time a job that needs credentials is opened (or its settings), Android shows its
  fingerprint prompt. After that the credentials are served from memory. A job needs credentials in two
  ways:
  * its own settings marked `secret=True` (e.g. Daily Log's key): asked when the job **opens**;
  * an **account** it signs in with (the Google account for Drive), declared with `JobPlugin.accounts`:
    asked when **Run** is pressed, and only if that run uses it (e.g. Parse executable with *Push to Google
    Drive* on). A job using an account must declare it, or a locked vault just looks like "not connected".
* **Lock**: after 5 minutes in the background, or **Lock now** (Settings tab, or "Lock credentials" in the
  palette). Unlocking again asks for the fingerprint
  ([flet-local-auth](https://pypi.org/project/flet-local-auth/)). Closing the app locks too.
* **Jobs without credentials** never ask.
* **Moving in**: credentials saved before the vault existed are moved into it after the first unlock
  (written to the vault first, then deleted from `state.json`).

Code: `taskdeck.core.secrets` (the vault, platform-independent) and `taskdeck.ui.vault_backend` (Android).
The host routes settings through it: `TaskDeckApp.job_settings()` / `save_job_settings()`. Jobs don't
handle the vault themselves: they mark their own fields `secret=True`, and declare the accounts they sign in
with:

```python
accounts = ((GOOGLE_ACCOUNT, "drive"),)  # the Google account, when the "drive" toggle is on ("" = always)
```

The job then gets a signed-in client from `ctx.services["google_drive"]` (absent while not connected).
Screens of their own (Extract Archive's) check `app.google.locked` and offer **Unlock** themselves.

## Good to know

* **Adding a new fingerprint to the phone erases the vault.** Android invalidates the key on purpose
  (otherwise a newly added finger could read it). TaskDeck notices, says so, and the job's setup check
  lists what to enter again. Keep the original key files (the Google `.json`, the Gemini key) somewhere
  safe, e.g. your password manager.
* **No screen lock on the phone, no vault**: Android refuses to create the key. The app says to set up a
  fingerprint or screen lock first.
* **Needs the APK built with the vault** (native plugins can't arrive as a live update). On an older APK,
  or on desktop, credentials stay in `state.json` as before, and the Settings tab says so.
* The in-app lock after 5 minutes is enforced by the app: within one app session Android keeps the
  storage key unlocked once released. Across app restarts it is enforced by the keystore itself.
