# TaskDeck to-do

## Keep long jobs running in the background

Context: Android limits network and CPU for apps in the background (battery optimization, Data Saver,
phone makers' own power managers). It cut TaskDeck's network while the user was approving the Google
sign-in in the browser, and it can stall long Google Drive uploads if TaskDeck isn't on screen. The
sign-in now retries through it; these make it much less likely to happen at all.

- [ ] **Battery exemption** ("Unrestricted" battery use). TaskDeck *can* ask: Android shows a system
      prompt, "Let TaskDeck always run in the background?".
      * Add `android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` to `[tool.flet.android.permission]`
        in `pyproject.toml` (new APK) and check it in the APK workflow.
      * Request with flet-permission-handler (already in the APK):
        `PermissionHandler().request(Permission.IGNORE_BATTERY_OPTIMIZATIONS)`; check with `get_status`.
      * Ask the first time an extraction goes to Google Drive (with one line of why), and offer it in
        Settings (e.g. a "Background work" section showing the current state).
- [ ] **Background data usage** ("Allow background data usage" under TaskDeck's app info → Mobile data).
      Apps can't turn it on or prompt for it; only the user can.
      * Add a button that opens TaskDeck's app-info page: `PermissionHandler().open_app_settings()`.
      * Say what to switch on; mention it only matters on mobile data and with Data Saver on.
- [ ] **Foreground service** for running tasks (the reliable fix): while a background task runs, Android
      shows a persistent notification ("TaskDeck · Extracting photos.7z · 47%") and doesn't pause the app.
      * No Flet plugin exists: needs a small custom Flet extension (Kotlin `Service` with
        `startForeground`, notification channel, progress updates, a Cancel action), built into the APK.
      * Permissions: `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, and `POST_NOTIFICATIONS`
        (Android 13+, asked at runtime).
      * Hook it to `taskdeck.core.tasks.TaskManager`: start the service when the first task starts, update
        it from task progress, stop it when no task is running. Also gives finished/failed notifications.
      * Android 15 caps data-sync foreground services at about 6 hours per day; fine for extractions.

## Other open items (details in the linked docs)

- [ ] Dropbox as an Extract Archive destination (Full Dropbox): `docs/jobs/extract_archive_SPEC.md`, step 3.
- [ ] Full Google Drive browsing (native Google Sign-In + Google review): same spec, §1.4, "planned (b)".
- [ ] Fingerprint lock design: keep the vault always on and make only the fingerprint check optional
      (discussed, not decided): `docs/CREDENTIALS.md`.
- [ ] Daily Log: Google sign-in on the phone instead of the service-account key ("option C"):
      `docs/jobs/daily_log.md`, Security note.
