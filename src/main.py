"""Entry point used by `flet run` and `flet build apk`.

Startup goes through taskdeck_boot, which runs the newest live update if there is
one (see that module) and falls back to the code bundled in the APK.
"""

import flet as ft

import taskdeck_boot

if __name__ == "__main__":
    ft.run(taskdeck_boot.start)
