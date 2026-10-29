"""Live code updates ("over the air") for TaskDeck.

This module is the fixed part of the app: it ships inside the APK and is never
replaced by an update. Everything in the ``taskdeck`` package can be replaced.

How it works
------------
* CI publishes ``ota.zip`` (the ``taskdeck`` package) and ``ota-manifest.json``
  to the ``taskdeck-ota`` GitHub release after the tests pass.
* On launch, :func:`start` picks which ``taskdeck`` to run: the newest
  downloaded bundle if it is newer than the code inside the APK, otherwise the
  bundled code. Then it starts the UI.
* After the UI is up, :func:`check_for_update` downloads a newer bundle in the
  background (verifying its SHA-256) and offers **Reload**.

Safety
------
* A bundle is only used if its ``requires_api`` matches :data:`OTA_API`. Changes
  that need new native libraries or a new Flet version bump the API and ship as
  a new APK instead.
* Crash protection: a bundle is marked "pending" before it starts and "good"
  once the UI has been built. If the app is launched while a bundle is still
  pending (the last start crashed), or importing it fails, the bundle is marked
  bad and the bundled code runs instead.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

OTA_API = 1  # bump when the shell (this file, flet, native deps) changes incompatibly
REPO = "jbalcomb/ScratchPatch"
RELEASE = f"https://github.com/{REPO}/releases/download/taskdeck-ota"
MANIFEST_URL = f"{RELEASE}/ota-manifest.json"
BUNDLE_URL = f"{RELEASE}/ota.zip"
SRC_DIR = Path(__file__).resolve().parent  # where the bundled taskdeck package lives

_lock = threading.Lock()
# One update check at a time: the automatic one after starting and "Check for updates" used to run
# together and unpack the same version into the same folder, each clearing the other's files (an
# installed update without taskdeck/jobs: "No module named 'taskdeck.jobs'").
_check_lock = threading.Lock()
active: dict[str, Any] = {"source": "bundled", "version": "dev", "build_time": 0}
listeners: list[Callable[[dict], None]] = []  # notified when an update is ready


# ---- storage --------------------------------------------------------------------

def data_dir() -> Path:
    root = os.environ.get("FLET_APP_STORAGE_DATA") or os.environ.get("TASKDECK_DATA_DIR")
    path = Path(root) if root else Path.home() / ".taskdeck"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ota_dir() -> Path:
    path = data_dir() / "ota"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_state() -> dict:
    try:
        return json.loads((ota_dir() / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = ota_dir() / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, ota_dir() / "state.json")


def bundled_build() -> dict:
    """Version info of the code shipped inside the APK (written by CI into taskdeck/_build.py).

    ``flet build`` ships the app compiled (taskdeck/_build.pyc, no .py), so that is read too; without it
    the APK said "dev" and any downloaded update, even an older one, counted as newer."""
    info = {"version": "dev", "build_time": 0}
    scope: dict = {}
    try:
        text = (SRC_DIR / "taskdeck" / "_build.py").read_text()
        exec(compile(text, "_build.py", "exec"), scope)
    except (OSError, SyntaxError, ValueError):
        pyc = SRC_DIR / "taskdeck" / "_build.pyc"
        try:
            loader = importlib.machinery.SourcelessFileLoader("_taskdeck_bundled_build", str(pyc))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            scope = vars(module)
        except (OSError, ImportError, ValueError, EOFError):
            return info
    try:
        info.update(version=scope.get("VERSION", "dev"), build_time=int(scope.get("BUILD_TIME", 0)))
    except (TypeError, ValueError):
        pass
    return info


# ---- choosing the code to run -----------------------------------------------------

def _purge_taskdeck_modules() -> None:
    for name in [m for m in sys.modules if m == "taskdeck" or m.startswith("taskdeck.")]:
        del sys.modules[name]


def _remove_ota_paths() -> None:
    root = str(ota_dir())
    sys.path[:] = [p for p in sys.path if not p.startswith(root)]


def activate() -> dict:
    """Put the right ``taskdeck`` on sys.path. Returns info about what will run."""
    global active
    with _lock:
        state = _load_state()
        bundled = bundled_build()
        _remove_ota_paths()
        _purge_taskdeck_modules()
        cur = state.get("current")
        if cur and state.get("pending") == cur:  # last start with this bundle never finished
            _mark_bad(state, cur, "did not finish starting last time")
            cur = None
        if cur:
            meta = state.get("bundles", {}).get(cur, {})
            path = ota_dir() / cur
            ok = (meta.get("requires_api") == OTA_API and (path / "taskdeck" / "__init__.py").exists()
                  and int(meta.get("build_time", 0)) > bundled["build_time"] and cur not in state.get("bad", []))
            if ok:
                sys.path.insert(0, str(path))
                state["pending"] = cur
                _save_state(state)
                active = {"source": "live update", "version": cur, "build_time": int(meta["build_time"])}
                return active
        _save_state(state)
        active = {"source": "bundled", **bundled}
        return active


def _mark_bad(state: dict, version: str, reason: str) -> None:
    state.setdefault("bad", []).append(version)
    state["bad"] = state["bad"][-20:]
    state["last_error"] = f"{version}: {reason}"
    if state.get("current") == version:
        state["current"] = None
    state["pending"] = None
    _save_state(state)


def confirm_started() -> None:
    """The UI came up: the active bundle is good."""
    with _lock:
        state = _load_state()
        if state.get("pending"):
            state["pending"] = None
            _save_state(state)


def start(page) -> None:
    """Entry point used by main.py: run the newest good code, falling back to the bundled code."""
    info = activate()
    try:
        from taskdeck.ui.app import main as app_main

        app_main(page)
    except Exception:
        if info["source"] == "bundled":
            raise
        with _lock:
            _mark_bad(_load_state(), info["version"], traceback.format_exc().strip().splitlines()[-1])
        activate()  # now resolves to the bundled code
        page.views.clear()
        from taskdeck.ui.app import main as app_main

        app_main(page)
    confirm_started()
    threading.Thread(target=_background_check, daemon=True).start()


def reload(page) -> dict:
    """Restart the UI on the newest downloaded code without closing the app.

    The new app replaces ``page.views`` itself; the page is pushed to the client once,
    afterwards (an intermediate update with no views leaves Flet showing "Working...").
    """
    start(page)
    page.update()
    return active


# ---- downloading updates ----------------------------------------------------------

class _Response(http.client.HTTPResponse):
    # Skip interim 1xx responses (e.g. 103 Early Hints); same fix as taskdeck.shared.http.
    def _read_status(self):
        while True:
            version, status, reason = super()._read_status()
            if 100 <= status < 200 and status != 101:
                http.client._read_headers(self.fp)
                continue
            return version, status, reason


class _HTTPSConnection(http.client.HTTPSConnection):
    response_class = _Response


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, timeout: float) -> bytes:
    opener = urllib.request.build_opener(_HTTPSHandler(context=_ssl_context()))
    req = urllib.request.Request(url, headers={"User-Agent": "TaskDeck-OTA", "Cache-Control": "no-cache"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def check_for_update(timeout: float = 20) -> dict:
    """Download and stage a newer bundle if there is one. Never raises. One check at a time (a second
    caller waits, then usually finds it up to date).

    Returns ``{"status": ..., "version": ..., "message": ...}`` where status is one of
    ``up_to_date``, ``ready`` (downloaded, reload to use), ``needs_apk`` or ``error``.
    """
    with _check_lock:
        return _check_for_update(timeout)


def _check_for_update(timeout: float) -> dict:
    try:
        manifest = json.loads(_get(f"{MANIFEST_URL}?t={int(time.time())}", timeout))
        version, build_time = str(manifest["version"]), int(manifest["build_time"])
        if manifest.get("requires_api") != OTA_API:
            return {"status": "needs_apk", "version": version,
                    "message": f"Version {version} needs a new APK (download it from the releases page)."}
        state = _load_state()
        newest = max(active["build_time"], int(state.get("bundles", {}).get(state.get("current") or "", {}).get("build_time", 0)))
        if build_time <= newest or version in state.get("bad", []):
            return {"status": "up_to_date", "version": active["version"], "message": "Up to date."}
        data = _get(BUNDLE_URL + f"?v={version}", 60)
        if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            return {"status": "error", "version": version, "message": "Downloaded update failed its checksum; will retry."}
        _install(version, manifest, data)
        result = {"status": "ready", "version": version, "message": f"Update {version} is ready."}
        for fn in list(listeners):
            try:
                fn(result)
            except Exception:
                pass
        return result
    except Exception as exc:  # offline, GitHub hiccup, bad manifest ...
        return {"status": "error", "version": "", "message": f"Couldn't check for updates: {exc}"}


def _install(version: str, manifest: dict, data: bytes) -> None:
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{version}-", dir=ota_dir()))  # never shared
    final = ota_dir() / version
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():  # refuse paths that escape the folder
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError(f"unsafe path in update: {member}")
            zf.extractall(staging)
            for info in zf.infolist():  # every file there, whole, before it can be switched on
                path = staging / info.filename
                if not info.is_dir() and (not path.is_file() or path.stat().st_size != info.file_size):
                    raise ValueError(f"update unpacked incompletely: {info.filename}")
        if not (staging / "taskdeck" / "__init__.py").exists():
            raise ValueError("update bundle has no taskdeck package")
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # gone already when installed
    with _lock:
        state = _load_state()
        state.setdefault("bundles", {})[version] = {
            "build_time": int(manifest["build_time"]), "requires_api": manifest.get("requires_api"),
            "sha256": manifest["sha256"],
        }
        state["current"] = version
        _save_state(state)
        _prune(state, keep={version, active["version"]})


def _prune(state: dict, keep: set[str]) -> None:
    for child in ota_dir().iterdir():
        if child.is_dir() and child.name.startswith(".staging-"):  # left by an interrupted install
            shutil.rmtree(child, ignore_errors=True)
        elif child.is_dir() and child.name not in keep and not child.name.startswith("."):
            shutil.rmtree(child, ignore_errors=True)
            state.get("bundles", {}).pop(child.name, None)
    _save_state(state)


def _background_check() -> None:
    time.sleep(3)  # let the UI settle first
    check_for_update()


def status() -> dict:
    state = _load_state()
    return {"active": dict(active), "bundled": bundled_build(), "staged": state.get("current"),
            "last_error": state.get("last_error"), "api": OTA_API}
