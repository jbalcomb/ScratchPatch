"""Live-update loader (src/taskdeck_boot.py): which code runs, and how updates are staged."""

import hashlib
import io
import json
import sys
import zipfile

import pytest

import taskdeck_boot as boot


def bundle_zip(marker: str = "ota") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("taskdeck/__init__.py", f"MARKER = {marker!r}\n")
        zf.writestr("taskdeck/_build.py", "VERSION = 'x'\n")
    return buf.getvalue()


def manifest_for(data: bytes, version="abc1234", build_time=2000, api=boot.OTA_API) -> dict:
    return {"version": version, "build_time": build_time, "requires_api": api,
            "sha256": hashlib.sha256(data).hexdigest()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir; a fake bundled build (build_time 1000); a fake GitHub."""
    monkeypatch.setenv("TASKDECK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    src = tmp_path / "src"
    (src / "taskdeck").mkdir(parents=True)
    (src / "taskdeck" / "_build.py").write_text("VERSION = 'bundled1'\nBUILD_TIME = 1000\n")
    monkeypatch.setattr(boot, "SRC_DIR", src)
    # Never purge the real taskdeck modules from this test process.
    monkeypatch.setattr(boot, "_purge_taskdeck_modules", lambda: None)
    monkeypatch.setattr(boot, "active", {"source": "bundled", "version": "bundled1", "build_time": 1000})
    remote = {}
    monkeypatch.setattr(boot, "_get", lambda url, timeout: remote[url.split("?")[0]])
    saved_path = list(sys.path)
    yield remote
    sys.path[:] = saved_path
    boot.listeners.clear()


def publish(remote, data, **kw):
    remote[boot.BUNDLE_URL] = data
    remote[boot.MANIFEST_URL] = json.dumps(manifest_for(data, **kw)).encode()


def test_no_update_runs_bundled(env):
    assert boot.activate()["source"] == "bundled"
    assert boot.activate()["version"] == "bundled1"


def test_newer_update_is_downloaded_then_used(env):
    data = bundle_zip()
    publish(env, data)
    seen = []
    boot.listeners.append(seen.append)
    result = boot.check_for_update()
    assert result["status"] == "ready" and seen and seen[0]["version"] == "abc1234"
    info = boot.activate()
    assert info == {"source": "live update", "version": "abc1234", "build_time": 2000}
    assert sys.path[0].endswith("abc1234")


def test_already_current_is_up_to_date(env):
    publish(env, bundle_zip(), build_time=1000)  # same age as the APK
    assert boot.check_for_update()["status"] == "up_to_date"


def test_newer_apk_wins_over_older_download(env, monkeypatch):
    publish(env, bundle_zip())
    boot.check_for_update()
    # The user installs a newer APK (built after the downloaded bundle).
    (boot.SRC_DIR / "taskdeck" / "_build.py").write_text("VERSION = 'apk2'\nBUILD_TIME = 3000\n")
    assert boot.activate()["version"] == "apk2"


def test_bad_checksum_is_rejected(env):
    data = bundle_zip()
    env[boot.BUNDLE_URL] = data + b"tampered"
    env[boot.MANIFEST_URL] = json.dumps(manifest_for(data)).encode()
    assert boot.check_for_update()["status"] == "error"
    assert boot.activate()["source"] == "bundled"


def test_new_api_needs_apk(env):
    publish(env, bundle_zip(), api=boot.OTA_API + 1)
    assert boot.check_for_update()["status"] == "needs_apk"
    assert boot.activate()["source"] == "bundled"


def test_crash_on_start_falls_back_and_blacklists(env):
    publish(env, bundle_zip())
    boot.check_for_update()
    assert boot.activate()["source"] == "live update"  # starts, marks it pending...
    # ...and the app dies before confirm_started(). Next launch:
    assert boot.activate()["source"] == "bundled"
    st = boot.status()
    assert "did not finish starting" in st["last_error"]
    # The same version is not downloaded again.
    assert boot.check_for_update()["status"] == "up_to_date"


def test_confirmed_start_keeps_using_update(env):
    publish(env, bundle_zip())
    boot.check_for_update()
    boot.activate()
    boot.confirm_started()
    assert boot.activate()["source"] == "live update"


def test_zip_with_escaping_paths_is_refused(env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("taskdeck/__init__.py", "")
        zf.writestr("../evil.py", "print('x')")
    publish(env, buf.getvalue())
    assert boot.check_for_update()["status"] == "error"
    assert not (boot.data_dir() / "evil.py").exists()


def test_offline_is_an_error_not_a_crash(env, monkeypatch):
    def offline(url, timeout):
        raise OSError("network unreachable")

    monkeypatch.setattr(boot, "_get", offline)
    result = boot.check_for_update()
    assert result["status"] == "error" and "network unreachable" in result["message"]
