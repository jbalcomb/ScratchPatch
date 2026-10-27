"""Connected accounts (Settings → Accounts). Google Drive for now.

The OAuth client (ID + secret, from docs/GOOGLE_DRIVE_SETUP.md) and the sign-in (refresh token)
are stored like job settings under ``account:google``; the secret and the refresh token are
credentials, so they go wherever credentials go (the vault when it's on).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

import flet as ft

from taskdeck.core import Field
from taskdeck.shared import gdrive, google_oauth

from .common import screen_view
from .forms import FieldControl

if TYPE_CHECKING:
    from .app import TaskDeckApp

GOOGLE = "account:google"
SECRET_FIELDS = {GOOGLE: {"client_secret", "refresh_token"}}
SETUP_DOC = "https://github.com/jbalcomb/ScratchPatch/blob/main/taskdeck/docs/GOOGLE_DRIVE_SETUP.md"


class GoogleAccount:
    def __init__(self, app: "TaskDeckApp"):
        self.app = app
        self._source: google_oauth.TokenSource | None = None

    @property
    def values(self) -> dict:
        return self.app.job_settings(GOOGLE)

    @property
    def locked(self) -> bool:
        return self.app.vault.needs_unlock(GOOGLE)

    @property
    def configured(self) -> bool:
        v = self.values
        return bool(v.get("client_id") and v.get("client_secret"))

    @property
    def connected(self) -> bool:
        return bool(self.values.get("refresh_token"))

    @property
    def email(self) -> str:
        return self.values.get("email", "")

    def save(self, **changes) -> None:
        self.app.save_job_settings(GOOGLE, {**self.values, **changes})
        self._source = None

    def drive(self) -> gdrive.DriveClient:
        v = self.values
        if self._source is None or self._source.refresh_token != v.get("refresh_token"):
            self._source = google_oauth.TokenSource(v["client_id"], v["client_secret"], v["refresh_token"])
        return gdrive.DriveClient(self._source.token)

    def disconnect(self) -> None:
        token = self.values.get("refresh_token")
        self.save(refresh_token="", email="")
        if token:
            threading.Thread(target=google_oauth.revoke, args=(token,), daemon=True).start()
        self.app.toast("Google Drive disconnected")
        self.app.render()
        self.app.page.update()

    # ---- connecting -------------------------------------------------------------------------

    def connect(self, on_done: Callable[[], None] | None = None) -> None:
        """Show the code dialog and wait for approval in the background."""
        if self.locked:
            self.app.page.run_task(self.app.unlock, lambda: self.connect(on_done))
            return
        if not self.configured:
            self.app.push(GoogleSetupScreen(self.app, self, on_done))
            return
        v = self.values
        status = ft.Text("Getting a code from Google…")
        code_text = ft.Text("", size=32, weight=ft.FontWeight.BOLD, selectable=True, text_align=ft.TextAlign.CENTER)
        copy = ft.TextButton("Copy code", icon=ft.Icons.COPY, disabled=True)
        open_link = ft.FilledButton("Open google.com/device", icon=ft.Icons.OPEN_IN_NEW, disabled=True)
        cancelled = threading.Event()

        def close(e=None):
            cancelled.set()
            self.app.page.pop_dialog()

        dialog = ft.AlertDialog(
            title=ft.Text("Connect Google Drive"),
            content=ft.Column([ft.Text("On any phone or computer, open google.com/device and enter:"),
                               code_text, ft.Row([copy], alignment=ft.MainAxisAlignment.CENTER), status],
                              tight=True, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            actions=[ft.TextButton("Cancel", on_click=close), open_link],
        )
        self.app.page.show_dialog(dialog)

        def work():
            try:
                code = google_oauth.start(v["client_id"])
                code_text.value = code.user_code
                copy.disabled = open_link.disabled = False
                copy.on_click = lambda e: self.app.copy(code.user_code)
                open_link.on_click = lambda e: self.app.open_url(code.verification_url)
                status.value = "Waiting for you to approve…"
                self.app.page.schedule_update()
                def offline(now_offline: bool) -> None:
                    status.value = ("No connection right now. Still waiting; it carries on when the phone is back "
                                    "online." if now_offline else "Waiting for you to approve…")
                    self.app.page.schedule_update()

                tokens = google_oauth.wait(v["client_id"], v["client_secret"], code, cancelled.is_set,
                                           on_offline=offline)
            except (google_oauth.OAuthError, Exception) as exc:  # network errors read the same to the user
                if not cancelled.is_set():
                    status.value = str(exc)
                    status.color = ft.Colors.ERROR
                    self.app.page.schedule_update()
                return
            if tokens is None:
                return
            self.save(refresh_token=tokens.refresh_token, email=tokens.email)
            self.app.page.pop_dialog()
            self.app.toast(f"Google Drive connected{' as ' + tokens.email if tokens.email else ''}")
            self.app.render()
            self.app.page.schedule_update()
            if on_done:
                on_done()

        self.app.page.run_thread(work)

    # ---- Settings tab -----------------------------------------------------------------------------

    def settings_rows(self) -> list[ft.Control]:
        if self.locked:
            state, button = "Locked", ft.TextButton("Unlock", icon=ft.Icons.FINGERPRINT,
                                                    on_click=lambda e: self.app.page.run_task(self.app.unlock))
        elif self.connected:
            state = f"Connected{' as ' + self.email if self.email else ''}"
            button = ft.TextButton("Disconnect", on_click=lambda e: self.disconnect())
        elif self.configured:
            state, button = "Not connected", ft.FilledTonalButton("Connect", on_click=lambda e: self.connect())
        else:
            state, button = "Not set up", ft.FilledTonalButton("Set up", on_click=lambda e: self.connect())
        tile = ft.ListTile(leading=ft.Icon(ft.Icons.ADD_TO_DRIVE), title=ft.Text("Google Drive"),
                           subtitle=ft.Text(state), trailing=button)
        rows: list[ft.Control] = [tile]
        if self.configured and not self.locked:
            rows.append(ft.Row([ft.TextButton("Change client ID / secret", icon=ft.Icons.EDIT,
                                              on_click=lambda e: self.app.push(GoogleSetupScreen(self.app, self)))]))
        return rows


class GoogleSetupScreen:
    """Enter the OAuth client from Google Cloud (docs/GOOGLE_DRIVE_SETUP.md, steps 3–4)."""

    def __init__(self, app: "TaskDeckApp", account: GoogleAccount, on_done: Callable[[], None] | None = None):
        self.app, self.account, self.on_done = app, account, on_done
        v = account.values
        self.client_id = FieldControl(Field.text("client_id", "Client ID",
                                                 help="Ends in .apps.googleusercontent.com"), v.get("client_id", ""))
        self.client_secret = FieldControl(Field.text("client_secret", "Client secret", secret=True),
                                          v.get("client_secret", ""))
        self.error = ft.Text("", color=ft.Colors.ERROR)
        self.view = screen_view(
            route="/accounts/google",
            appbar=ft.AppBar(title=ft.Text("Google Drive")),
            controls=[
                ft.Text("TaskDeck needs its own Google sign-in client. Create one in Google Cloud "
                        "(type “TVs and Limited Input devices”) and paste its Client ID and secret here."),
                ft.TextButton("Open the setup guide", icon=ft.Icons.MENU_BOOK,
                              on_click=lambda e: app.open_url(SETUP_DOC)),
                self.client_id.control, self.client_secret.control, self.error,
                ft.FilledButton("Save and connect", icon=ft.Icons.LINK, height=52, on_click=self.save),
            ],
        )

    def save(self, e=None) -> None:
        cid = (self.client_id.value or "").strip()
        secret = (self.client_secret.value or "").strip()
        if not cid.endswith(".apps.googleusercontent.com"):
            self.error.value = "The Client ID should end in .apps.googleusercontent.com."
        elif not secret:
            self.error.value = "Paste the client secret too."
        else:
            changed = cid != self.account.values.get("client_id")
            self.account.save(client_id=cid, client_secret=secret, **({"refresh_token": "", "email": ""} if changed else {}))
            self.app.back()
            self.account.connect(self.on_done)
            return
        self.app.page.update()
