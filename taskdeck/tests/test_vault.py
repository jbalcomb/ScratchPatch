"""Credentials vault: secrets leave state.json, need the fingerprint, survive lock/unlock."""

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from taskdeck.core import JsonStore, Vault, VaultError
from taskdeck.core.secrets import ENABLED, KEYS_MARKER

SECRETS = {"daily_log": {"fallback_api_key", "service_account_json", "llm_api_key"}}


class FakeKeystore:
    """Stands in for Android secure storage + the fingerprint prompt."""

    def __init__(self, data=None, refuse=None):
        self.data = dict(data or {})
        self.prompts = 0  # fingerprint prompts shown
        self.refuse = refuse  # exception to raise instead of accepting the fingerprint
        self.opened = False  # Android releases the key once per app launch, after a fingerprint

    async def load_all(self, reason):
        self._prompt()  # Android's prompt the first time, the local-auth prompt after that
        self.opened = True
        return dict(self.data)

    def _prompt(self):
        self.prompts += 1
        if self.refuse:
            raise self.refuse

    async def put(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def run(coro):
    return asyncio.run(coro)


def vault(tmp_path, backend, enabled=True):
    store = JsonStore(tmp_path / "state.json")
    store.set(ENABLED, enabled)
    return Vault(backend, store, SECRETS)


def test_without_a_keystore_everything_stays_plain(tmp_path):
    v = vault(tmp_path, None)
    assert not v.available and v.unlocked and not v.needs_unlock("daily_log")
    assert v.split("daily_log", {"sheet_id": "S", "fallback_api_key": "k"}) == ({"sheet_id": "S", "fallback_api_key": "k"}, {})


def test_unlock_needs_the_fingerprint_and_serves_secrets(tmp_path):
    ks = FakeKeystore({"settings:daily_log:fallback_api_key": "gem"})
    v = vault(tmp_path, ks)
    assert v.needs_unlock("daily_log") and v.secrets_for("daily_log") == {}
    assert not v.needs_unlock("tip_split")  # no secrets: never asks
    run(v.unlock())
    assert ks.prompts == 1 and v.secrets_for("daily_log") == {"fallback_api_key": "gem"}
    run(v.unlock())
    assert ks.prompts == 1  # already unlocked: no second prompt


def test_lock_then_unlock_asks_again(tmp_path):
    ks = FakeKeystore({"settings:daily_log:fallback_api_key": "gem"})
    v = vault(tmp_path, ks)
    run(v.unlock())
    v.lock()
    assert v.needs_unlock("daily_log") and v.secrets_for("daily_log") == {}
    run(v.unlock())
    assert ks.prompts == 2 and v.secrets_for("daily_log")["fallback_api_key"] == "gem"


def test_refused_fingerprint_keeps_it_locked(tmp_path):
    ks = FakeKeystore({"settings:daily_log:fallback_api_key": "gem"}, refuse=RuntimeError("User canceled"))
    v = vault(tmp_path, ks)
    with pytest.raises(VaultError, match="cancelled"):
        run(v.unlock())
    assert v.needs_unlock("daily_log")
    ks.refuse = RuntimeError("No biometrics enrolled")
    with pytest.raises(VaultError, match="screen lock"):
        run(v.unlock())


def test_plaintext_secrets_move_into_the_vault(tmp_path):
    ks = FakeKeystore()
    v = vault(tmp_path, ks)
    v.store.set("settings:daily_log", {"sheet_id": "S", "fallback_api_key": "gem", "service_account_json": "{...}",
                                       "llm_api_key": ""})
    run(v.unlock())
    assert ks.data == {"settings:daily_log:fallback_api_key": "gem", "settings:daily_log:service_account_json": "{...}"}
    assert v.store.get("settings:daily_log") == {"sheet_id": "S"}  # no secrets left in state.json
    assert "gem" not in (tmp_path / "state.json").read_text()


def test_save_and_clear_secrets(tmp_path):
    ks = FakeKeystore()
    v = vault(tmp_path, ks)
    with pytest.raises(VaultError):
        v.stage("daily_log", {"fallback_api_key": "x"})  # locked: refuses
    run(v.unlock())
    plain, secret = v.split("daily_log", {"sheet_id": "S", "fallback_api_key": "new", "llm_api_key": ""})
    assert plain == {"sheet_id": "S"} and secret == {"fallback_api_key": "new", "llm_api_key": ""}
    ops = v.stage("daily_log", secret)
    assert v.secrets_for("daily_log") == {"fallback_api_key": "new"}  # visible at once
    run(v.persist(ops))
    assert ks.data == {"settings:daily_log:fallback_api_key": "new"}
    run(v.save("daily_log", {"fallback_api_key": ""}))
    assert ks.data == {} and v.secrets_for("daily_log") == {}


def test_notices_when_android_erased_the_secrets(tmp_path):
    ks = FakeKeystore()
    v = vault(tmp_path, ks)
    run(v.unlock())
    run(v.save("daily_log", {"fallback_api_key": "gem"}))
    assert v.store.get(KEYS_MARKER) == ["settings:daily_log:fallback_api_key"]
    ks.data.clear()  # a new fingerprint was enrolled: the keystore key and its data are gone
    fresh = Vault(FakeKeystore(), v.store, SECRETS)
    run(fresh.unlock())
    assert fresh.lost == ["settings:daily_log:fallback_api_key"]


# ---- the app shell ---------------------------------------------------------------------------

class FakePage:
    """Just enough of ft.Page for TaskDeckApp, running tasks to completion at once."""

    def __init__(self):
        self.views, self.web, self.platform = [], False, None
        self.overlay, self.shown = [], []

    def update(self, *a):
        pass

    def run_task(self, fn, *args):
        result = fn(*args)
        if inspect.isawaitable(result):
            asyncio.run(result)

    def show_dialog(self, d):
        self.shown.append(d)


@pytest.fixture
def app_factory(tmp_path):
    from taskdeck.ui.app import TaskDeckApp

    def make(backend, enabled=True):
        store = JsonStore(tmp_path / "state.json")
        store.set(ENABLED, enabled)
        app = TaskDeckApp(FakePage(), store=store, vault_backend=backend)
        app.toasts = []
        app.toast = app.toasts.append
        return app

    return make


def test_app_keeps_credentials_out_of_state_json(app_factory, tmp_path):
    ks = FakeKeystore()
    app = app_factory(ks)
    run(app.unlock())
    app.save_job_settings("daily_log", {"sheet_id": "S", "fallback_api_key": "gem", "service_account_json": "{k}"})
    assert ks.data == {"settings:daily_log:fallback_api_key": "gem", "settings:daily_log:service_account_json": "{k}"}
    assert "gem" not in (tmp_path / "state.json").read_text()
    assert app.job_settings("daily_log")["fallback_api_key"] == "gem"
    assert app.job_settings("daily_log_reconcile")["service_account_json"] == "{k}"  # shared settings
    app.lock_credentials()
    assert "fallback_api_key" not in app.job_settings("daily_log") and app.toasts[-1] == "Credentials locked"


def test_opening_a_job_asks_for_the_fingerprint(app_factory):
    ks = FakeKeystore({"settings:daily_log:fallback_api_key": "gem"})
    app = app_factory(ks)
    app.open_job("daily_log")
    assert ks.prompts == 1 and app.vault.unlocked
    assert not any("locked" in p.message for p in app.top_job_screen.problems)


def test_cancelled_unlock_leaves_the_job_locked_with_a_button(app_factory):
    ks = FakeKeystore(refuse=RuntimeError("User canceled"))
    app = app_factory(ks)
    app.open_job("daily_log")
    screen = app.top_job_screen
    assert screen.blocked and screen.problems[0].fix == "unlock"
    assert app.toasts == ["Unlock cancelled."]
    app.open_settings("daily_log")
    assert app.top_job_screen is screen  # settings didn't open while locked


def test_jobs_without_secrets_never_ask(app_factory):
    ks = FakeKeystore()
    app = app_factory(ks)
    app.open_job("tip_split")
    assert ks.prompts == 0


def test_locks_after_a_while_in_the_background(app_factory, monkeypatch):
    import flet as ft
    from taskdeck.ui import app as app_module

    ks = FakeKeystore({"settings:daily_log:fallback_api_key": "gem"})
    app = app_factory(ks)
    app.open_job("daily_log")
    clock = [1000.0]
    monkeypatch.setattr(app_module.time, "time", lambda: clock[0])
    app.on_lifecycle(SimpleNamespace(state=ft.AppLifecycleState.HIDE))
    clock[0] += 60
    app.on_lifecycle(SimpleNamespace(state=ft.AppLifecycleState.RESUME))
    assert app.vault.unlocked and ks.prompts == 1  # a minute away: still unlocked
    app.on_lifecycle(SimpleNamespace(state=ft.AppLifecycleState.HIDE))
    clock[0] += app_module.LOCK_AFTER + 1
    app.on_lifecycle(SimpleNamespace(state=ft.AppLifecycleState.RESUME))
    assert ks.prompts == 2 and app.vault.unlocked  # locked, and the open job asked again


# ---- the Android backend (plugins faked; they only run on a phone) -----------------------------

def test_android_backend_prompts_on_every_unlock(monkeypatch):
    pytest.importorskip("flet_secure_storage")
    import types

    import flet_local_auth

    from taskdeck.ui import vault_backend

    calls = []

    class FakeStorage:
        def __init__(self, android_options):
            assert android_options.enforce_biometrics  # the keystore key needs the fingerprint
            self.options = android_options

        async def get_all(self):
            calls.append("android-prompt" if not calls else "read")
            return {"settings:daily_log:fallback_api_key": "gem"}

    class FakeLocalAuth:
        async def authenticate(self, reason, **kw):
            calls.append("local-auth")
            return True

    module = types.SimpleNamespace(SecureStorage=FakeStorage)
    monkeypatch.setattr(flet_local_auth, "LocalAuthentication", FakeLocalAuth)
    backend = vault_backend.AndroidVaultBackend(module, flet_local_auth)
    assert run(backend.load_all("why")) == {"settings:daily_log:fallback_api_key": "gem"}
    assert calls == ["android-prompt"]
    again = vault_backend.AndroidVaultBackend(module, flet_local_auth)  # e.g. after a live-update reload
    run(again.load_all("why"))
    assert calls == ["android-prompt", "local-auth", "read"]


def test_android_backend_refused_fingerprint(monkeypatch):
    pytest.importorskip("flet_secure_storage")
    import types

    import flet_local_auth
    from flet_local_auth import LocalAuthErrorCode, LocalAuthException

    from taskdeck.ui import vault_backend

    class FakeLocalAuth:
        async def authenticate(self, reason, **kw):
            raise LocalAuthException(code=LocalAuthErrorCode.BIOMETRIC_LOCKOUT)

    monkeypatch.setattr(flet_local_auth, "LocalAuthentication", FakeLocalAuth)
    backend = vault_backend.AndroidVaultBackend(types.SimpleNamespace(SecureStorage=lambda **kw: None,
                                                                      _taskdeck_opened=True), flet_local_auth)
    with pytest.raises(VaultError, match="phone with its PIN"):
        run(backend.load_all("why"))


# ---- the on/off switch -------------------------------------------------------------------------

def test_off_by_default(tmp_path):
    ks = FakeKeystore()
    v = Vault(ks, JsonStore(tmp_path / "state.json"), SECRETS)
    assert v.supported and not v.enabled and not v.available and not v.needs_unlock("daily_log")
    assert v.split("daily_log", {"fallback_api_key": "k"}) == ({"fallback_api_key": "k"}, {})


def test_stays_on_when_the_vault_already_holds_secrets(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    store.set(KEYS_MARKER, ["settings:daily_log:fallback_api_key"])  # moved in before the switch existed
    assert Vault(FakeKeystore(), store, SECRETS).available


def test_turning_on_moves_secrets_in_and_off_moves_them_back(tmp_path):
    ks = FakeKeystore()
    v = vault(tmp_path, ks, enabled=False)
    v.store.set("settings:daily_log", {"sheet_id": "S", "fallback_api_key": "gem"})
    run(v.enable())
    assert v.available and ks.prompts == 1
    assert ks.data == {"settings:daily_log:fallback_api_key": "gem"} and v.store.get("settings:daily_log") == {"sheet_id": "S"}
    v.lock()
    run(v.disable())
    assert ks.prompts == 2  # turning it off needs the fingerprint too
    assert not v.available and ks.data == {}
    assert v.store.get("settings:daily_log") == {"sheet_id": "S", "fallback_api_key": "gem"}
    assert v.store.get(KEYS_MARKER) == []


def test_turning_on_refused_stays_off(tmp_path):
    v = vault(tmp_path, FakeKeystore(refuse=RuntimeError("User canceled")), enabled=False)
    v.store.set("settings:daily_log", {"fallback_api_key": "gem"})
    with pytest.raises(VaultError, match="cancelled"):
        run(v.enable())
    assert not v.available and v.store.get("settings:daily_log") == {"fallback_api_key": "gem"}


def test_turning_off_refused_stays_on(tmp_path):
    ks = FakeKeystore({"settings:daily_log:fallback_api_key": "gem"})
    v = vault(tmp_path, ks)
    ks.refuse = RuntimeError("User canceled")
    with pytest.raises(VaultError):
        run(v.disable())
    assert v.available and ks.data  # nothing moved


def test_cannot_turn_on_without_the_plugins(tmp_path):
    with pytest.raises(VaultError, match="latest APK"):
        run(vault(tmp_path, None, enabled=False).enable())


def test_settings_switch(app_factory):
    ks = FakeKeystore()
    app = app_factory(ks, enabled=False)
    app.save_job_settings("daily_log", {"fallback_api_key": "gem"})
    assert app.store.get("settings:daily_log") == {"fallback_api_key": "gem"} and ks.prompts == 0
    run(app.set_fingerprint_lock(True))
    assert app.vault.available and app.toasts[-1] == "Fingerprint lock on"
    assert ks.data == {"settings:daily_log:fallback_api_key": "gem"}
    run(app.set_fingerprint_lock(False))
    assert not app.vault.available and app.job_settings("daily_log")["fallback_api_key"] == "gem"
