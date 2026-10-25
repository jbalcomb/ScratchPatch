# Credentials vault

Every job setting marked `secret=True` (API keys, the Google service-account key, ...) is a credential.
TaskDeck keeps credentials out of `state.json` and in a vault that your **fingerprint** unlocks.

## How it works

* **Storage**: [flet-secure-storage](https://pypi.org/project/flet-secure-storage/) with
  `enforce_biometrics=True` and the AES-GCM key cipher. The AES key lives in the Android keystore
  (hardware-backed on most phones) and is created with "user authentication required", so Android only
  lets the app use it after a fingerprint or the phone PIN. Without one, the stored data can't be decrypted,
  not even from a backup or by another app.
* **Unlock**: the first time a job that needs credentials is opened (or its settings), Android shows its
  fingerprint prompt. After that the credentials are served from memory.
* **Lock**: after 5 minutes in the background, or **Lock now** (Settings tab, or "Lock credentials" in the
  palette). Unlocking again asks for the fingerprint
  ([flet-local-auth](https://pypi.org/project/flet-local-auth/)). Closing the app locks too.
* **Jobs without credentials** never ask.
* **Moving in**: credentials saved before the vault existed are moved into it after the first unlock
  (written to the vault first, then deleted from `state.json`).

Code: `taskdeck.core.secrets` (the vault, platform-independent) and `taskdeck.ui.vault_backend` (Android).
The host routes settings through it: `TaskDeckApp.job_settings()` / `save_job_settings()`. Jobs don't
change; they just mark fields `secret=True`.

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
