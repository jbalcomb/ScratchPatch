# Updating TaskDeck on the phone

There are two kinds of update.

## Live updates (most changes)

Every push that touches `src/` runs the **Live update** workflow (`.github/workflows/ota.yml`):
tests, an import smoke test, then the `taskdeck` package is zipped and published with a manifest
(version, build time, SHA-256, required shell API) to the `taskdeck-ota` GitHub release.

The installed app (`src/taskdeck_boot.py`, which is part of the APK and never replaced):

1. On launch runs the newest downloaded bundle if it is newer than the code inside the APK, otherwise
   the bundled code.
2. A few seconds after start, downloads a newer bundle if there is one, checks its SHA-256 and shows
   **Update … downloaded · Reload**. Reload restarts the UI on the new code without closing the app;
   otherwise it is used on the next launch. *Settings → App updates* shows the running version and
   has **Check for updates**.
3. Crash protection: if a bundle fails to import, or the app is launched again before the previous start
   with that bundle finished, the bundle is marked bad and the bundled code runs. Settings shows the last
   rejected update.

What a live update **cannot** change: `src/main.py`, `src/taskdeck_boot.py`, Python packages in
`pyproject.toml` dependencies, the Flet version, Android permissions or anything native. Those need a new APK.
When such a change is made, bump `OTA_API` in `taskdeck_boot.py`; installed apps then ignore newer bundles
and say a new APK is needed.

Trust: anyone who can push to this repository can change the code that runs on the phone. Downloads are
HTTPS from github.com and checked against the manifest's SHA-256.

## New APK (shell changes)

The **Android APK** workflow builds `TaskDeck.apk`, signed with the permanent key in
`android/taskdeck-release.jks` (password in the `ANDROID_KEYSTORE_PASSWORD` repository secret), and
publishes it to the `taskdeck-latest` release. Because the signature never changes, each APK installs over
the previous one and keeps the app's data. The workflow fails if the secret is missing and checks the
certificate of every APK it builds.

Download: https://github.com/jbalcomb/ScratchPatch/releases/download/taskdeck-latest/TaskDeck.apk

The keystore file is public but useless without its password. Losing the password means the next APK
needs a new key, and the app has to be uninstalled once (losing its settings).
