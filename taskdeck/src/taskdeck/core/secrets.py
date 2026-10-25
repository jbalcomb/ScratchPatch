"""Credentials vault: secret settings live in the phone's secure storage, not in state.json.

The settings marked ``secret=True`` (API keys, the Google key file...) are kept by a
:class:`SecretBackend`. On Android that is the system keystore, whose key is only
released after the user's fingerprint (or phone PIN), so the fingerprint acts as the
master key for every credential. Everything else stays in the plain JSON store.

Lifecycle:

* **Locked** at start. :meth:`Vault.unlock` reads everything from the backend, which asks
  for the fingerprint every time.
* **Unlocked**: secrets are served from memory.
* :meth:`Vault.lock` (on request, or after the app sat in the background) forgets them;
  the next unlock asks for the fingerprint again.

If the backend is unavailable (desktop, tests, or an APK built before the vault existed)
secrets stay in the JSON store exactly as before.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .storage import JsonStore

KEYS_MARKER = "vault:keys"  # which secrets the vault should hold, to notice if Android erased them


class VaultError(Exception):
    """Unlocking failed or was cancelled; the message is shown to the user."""


class SecretBackend(Protocol):
    async def load_all(self, reason: str) -> dict[str, str]:
        """Ask for the fingerprint, then return every stored secret. Raise if refused."""

    async def put(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...


def vault_key(settings_id: str, field: str) -> str:
    return f"settings:{settings_id}:{field}"


class Vault:
    def __init__(self, backend: SecretBackend | None, store: JsonStore, secret_fields: dict[str, set[str]]):
        """``secret_fields`` maps a settings id (job id) to the names of its secret settings."""
        self.backend = backend
        self.store = store
        self.secret_fields = {k: set(v) for k, v in secret_fields.items() if v}
        self._cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        self.lost: list[str] = []  # secrets Android erased (e.g. a new fingerprint was added)

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def unlocked(self) -> bool:
        return not self.available or self._cache is not None

    def protects(self, settings_id: str) -> bool:
        """Does this job's settings include secrets the vault keeps?"""
        return self.available and settings_id in self.secret_fields

    def needs_unlock(self, settings_id: str) -> bool:
        return self.protects(settings_id) and not self.unlocked

    # ---- reading and writing settings ----------------------------------------------

    def split(self, settings_id: str, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """(plain values for the JSON store, secret values for the vault)."""
        if not self.protects(settings_id):
            return dict(values), {}
        names = self.secret_fields[settings_id]
        plain = {k: v for k, v in values.items() if k not in names}
        secret = {k: str(v or "") for k, v in values.items() if k in names}
        return plain, secret

    def secrets_for(self, settings_id: str) -> dict[str, str]:
        """The job's secrets, or {} while locked."""
        if not self.protects(settings_id) or self._cache is None:
            return {}
        return {f: self._cache[vault_key(settings_id, f)] for f in self.secret_fields[settings_id]
                if vault_key(settings_id, f) in self._cache}

    def stage(self, settings_id: str, secrets: dict[str, str]) -> list[tuple[str, str | None]]:
        """Apply secrets to memory now (blank removes one) and return the writes for :meth:`persist`.
        Only while unlocked."""
        if not secrets:
            return []
        if self._cache is None:
            raise VaultError("Unlock your credentials first.")
        ops: list[tuple[str, str | None]] = []
        for field, value in secrets.items():
            key = vault_key(settings_id, field)
            if value.strip():
                if self._cache.get(key) != value:
                    ops.append((key, value))
                self._cache[key] = value
            elif key in self._cache:
                ops.append((key, None))
                del self._cache[key]
        self._remember_keys()
        return ops

    async def persist(self, ops: list[tuple[str, str | None]]) -> None:
        for key, value in ops:
            if value is None:
                await self.backend.delete(key)
            else:
                await self.backend.put(key, value)

    async def save(self, settings_id: str, secrets: dict[str, str]) -> None:
        await self.persist(self.stage(settings_id, secrets))

    # ---- locking ----------------------------------------------------------------------

    async def unlock(self, reason: str = "Unlock your TaskDeck credentials") -> None:
        """Ask for the fingerprint and load the secrets. Raises VaultError if that fails."""
        if self.unlocked:
            return
        async with self._lock:
            if self._cache is not None:
                return
            try:
                values = await self.backend.load_all(reason)
            except VaultError:
                raise
            except Exception as exc:  # the platform's own error (cancelled, no screen lock, ...)
                raise VaultError(_explain(exc)) from None
            self._cache = {k: v for k, v in (values or {}).items() if isinstance(v, str) and k.startswith("settings:")}
            expected = self.store.get(KEYS_MARKER, [])
            self.lost = sorted(k for k in expected if k not in self._cache)
            await self._migrate_plaintext()
            self._remember_keys()

    def lock(self) -> None:
        if self.available:
            self._cache = None

    async def _migrate_plaintext(self) -> None:
        """Move secrets written before the vault existed out of state.json (vault first, then delete)."""
        for settings_id, names in self.secret_fields.items():
            stored = self.store.get(f"settings:{settings_id}", {})
            if not isinstance(stored, dict) or not names & stored.keys():
                continue
            moving = {k: str(stored[k] or "") for k in names & stored.keys()}
            await self.save(settings_id, {k: v for k, v in moving.items() if v.strip()})
            self.store.set(f"settings:{settings_id}", {k: v for k, v in stored.items() if k not in names})

    def _remember_keys(self) -> None:
        keys = sorted(self._cache or {})
        if self.store.get(KEYS_MARKER) != keys:
            self.store.set(KEYS_MARKER, keys)


def _explain(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if any(w in low for w in ("cancel", "negative button", "user canceled")):
        return "Unlock cancelled."
    if any(w in low for w in ("no biometric", "not enrolled", "nobiometrics", "no credentials", "not secure",
                              "keyguard", "screen lock")):
        return "Set up a fingerprint or screen lock on the phone first (Android Settings → Security)."
    if "lockout" in low:
        return "Too many attempts. Wait a moment, or unlock with your phone PIN."
    return f"Couldn't unlock your credentials: {text}"
