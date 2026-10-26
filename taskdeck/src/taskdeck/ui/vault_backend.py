"""Android backend for the credentials vault (see taskdeck.core.secrets).

Secrets go into flet-secure-storage with ``enforce_biometrics``: the AES key lives in the
Android keystore and is only usable after a fingerprint or the phone PIN, so the data
can't be read without one. Android shows that prompt once per process; every later unlock
in the same process (after a lock, or after a live-update reload) is gated by
flet-local-auth instead.

Both are native plugins built into the APK. A live update can arrive on an APK built
without them; then :func:`make_backend` returns None and secrets stay in state.json.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from taskdeck.core import VaultError

TITLE = "Unlock TaskDeck"
SUBTITLE = "Your fingerprint (or phone PIN) unlocks your saved credentials"


class AndroidVaultBackend:
    def __init__(self, secure_storage: Any, local_auth: Any):
        from flet_secure_storage import AndroidOptions, KeyCipherAlgorithm, StorageCipherAlgorithm

        self.storage = secure_storage.SecureStorage(android_options=AndroidOptions(
            enforce_biometrics=True,  # the keystore key itself requires the fingerprint
            key_cipher_algorithm=KeyCipherAlgorithm.AES_GCM_NO_PADDING,  # the one that supports biometrics
            storage_cipher_algorithm=StorageCipherAlgorithm.AES_GCM_NO_PADDING,
            reset_on_error=True,  # an invalidated key (new fingerprint added) can't be recovered anyway
            preferences_key_prefix="taskdeck_vault",
            biometric_prompt_title=TITLE,
            biometric_prompt_subtitle=SUBTITLE,
        ))
        self.secure_storage = secure_storage
        self.local_auth = local_auth

    async def load_all(self, reason: str) -> dict[str, str]:
        # Remembered on the plugin module, which comes from the APK and survives a live-update
        # reload of TaskDeck's own modules (the native storage stays open for the whole process).
        if getattr(self.secure_storage, "_taskdeck_opened", False):
            await self.verify_user(reason)  # Android won't prompt again by itself
        values = dict(await self.storage.get_all() or {})  # first time: Android's fingerprint prompt
        self.secure_storage._taskdeck_opened = True
        return values

    async def verify_user(self, reason: str) -> None:
        from flet_local_auth import AndroidAuthMessages, LocalAuthentication, LocalAuthException

        try:
            ok = await LocalAuthentication().authenticate(
                reason, biometric_only=False, persist_across_backgrounding=True,
                android_messages=AndroidAuthMessages(sign_in_title=TITLE, cancel_button="Cancel"))
        except LocalAuthException as exc:
            raise VaultError(_auth_message(exc)) from None
        if not ok:
            raise VaultError("Unlock cancelled.")

    async def put(self, key: str, value: str) -> None:
        await self.storage.set(key, value)

    async def delete(self, key: str) -> None:
        await self.storage.remove(key)


def _auth_message(exc: Any) -> str:
    code = getattr(getattr(exc, "code", None), "value", "")
    return {
        "userCanceled": "Unlock cancelled.",
        "systemCanceled": "Unlock cancelled.",
        "noCredentialsSet": "Set up a fingerprint or screen lock on the phone first (Android Settings → Security).",
        "noBiometricsEnrolled": "Set up a fingerprint or screen lock on the phone first (Android Settings → Security).",
        "temporaryLockout": "Too many attempts. Wait a moment, or unlock with your phone PIN.",
        "biometricLockout": "Fingerprint is locked after too many attempts. Unlock the phone with its PIN, then try again.",
    }.get(code, f"Couldn't unlock your credentials: {getattr(exc, 'description', None) or code or exc}")


def make_backend(page: ft.Page) -> AndroidVaultBackend | None:
    """The vault backend on Android when the APK includes the plugins; otherwise None."""
    if page.web or page.platform != ft.PagePlatform.ANDROID:
        return None
    try:
        import flet_local_auth
        import flet_secure_storage
    except ImportError:  # APK built before the vault existed
        return None
    return AndroidVaultBackend(flet_secure_storage, flet_local_auth)
