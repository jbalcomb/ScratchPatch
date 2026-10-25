"""Carries lib7z.so (7-Zip's archive library) and lib7zshim.so (TaskDeck's C API over it).

There is no Python code here: ``taskdeck.shared.archive`` loads the libraries. On Android
the APK build moves these .so files next to the app's native libraries.
"""

SEVEN_ZIP_VERSION = "26.05"
