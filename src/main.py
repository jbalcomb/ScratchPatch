"""Entry point used by `flet run` and `flet build apk`."""

import flet as ft

from taskdeck.ui.app import main

if __name__ == "__main__":
    ft.run(main)
