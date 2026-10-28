# taskdeck-background

A small Flet extension: an Android **foreground service** that keeps TaskDeck running while a long
task (e.g. Extract Archive uploading to Google Drive) runs and you use other apps. Android shows an
ongoing notification with the task's progress; when the task ends, a "finished" notification.

* `src/taskdeck_background/`: the Python service control, `BackgroundWork` (`start`, `update`, `stop`).
* `src/flutter/taskdeck_background/`: the Flutter side. `lib/` passes the calls to the Android plugin in
  `android/` (Java): `TaskService` (foreground service, type `dataSync`, with wake and Wi-Fi locks) and
  `BackgroundPlugin` (method channel). Its `AndroidManifest.xml` declares the service and permissions,
  merged into TaskDeck's manifest at build time.

`flet build` picks it up through `[tool.flet.dev_packages]` in TaskDeck's `pyproject.toml`. The work
itself stays in TaskDeck's Python (`taskdeck.ui.background` drives the service from the task manager).

Compile-check the Java without the Android SDK (as done during development):

```
javac --release 17 -d out -cp android-all-15-robolectric-13954326.jar:flutter_embedding_release.jar \
  src/flutter/taskdeck_background/android/src/main/java/dev/taskdeck/background/*.java
```

(`org.robolectric:android-all` from Maven Central; the embedding jar from download.flutter.io for the
engine version of Flet's Flutter.)
