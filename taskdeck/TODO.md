# TaskDeck to-do

## Keep long jobs running in the background

Context: Android limits network and CPU for apps in the background (battery optimization, Data Saver,
phone makers' own power managers). It cut TaskDeck's network while the user was approving the Google
sign-in in the browser, and it can stall long Google Drive uploads if TaskDeck isn't on screen. The
sign-in now retries through it; these make it much less likely to happen at all.

- [x] **Battery exemption**: asked the first time a task runs; also Settings → Background work.
- [ ] **Background data usage** ("Allow background data usage" under TaskDeck's app info → Mobile data).
      Apps can't turn it on or prompt for it; only the user can.
      * Add a button that opens TaskDeck's app-info page: `PermissionHandler().open_app_settings()`.
      * Say what to switch on; mention it only matters on mobile data and with Data Saver on.
- [x] **Foreground service** while tasks run: `native/background` (taskdeck-background extension, Java
      `TaskService`), driven by `taskdeck.ui.background`. Ongoing notification with progress; a "finished"
      notification when TaskDeck isn't on screen.
      * Not done yet: a Cancel button in the notification (cancel from TaskDeck for now).

## Other open items (details in the linked docs)

- [ ] Dropbox as an Extract Archive destination (Full Dropbox): `docs/jobs/extract_archive_SPEC.md`, step 3.
- [ ] Full Google Drive browsing (native Google Sign-In + Google review): same spec, §1.4, "planned (b)".
- [ ] Fingerprint lock design: keep the vault always on and make only the fingerprint check optional
      (discussed, not decided): `docs/CREDENTIALS.md`.
- [ ] Daily Log: Google sign-in on the phone instead of the service-account key ("option C"):
      `docs/jobs/daily_log.md`, Security note.
