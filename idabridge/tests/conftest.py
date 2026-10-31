import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from idabridge.config import Settings  # noqa: E402
from idabridge.runner import JobManager  # noqa: E402

FAKE = Path(__file__).with_name("fake_idaw.py")


@pytest.fixture
def settings(tmp_path):
    inputs = tmp_path / "inputs"
    (inputs / "game").mkdir(parents=True)
    for name in ("MAGIC.EXE", "hang.exe", "slow.exe", "crash.exe", "noscript.exe", "nodb.exe"):
        (inputs / "game" / name).write_bytes(b"MZ" + name.encode())
    return Settings(input_dir=inputs, ida_cmd=[sys.executable, str(FAKE)], home=tmp_path / "home",
                    token="secret", default_timeout=30, default_wait=20, max_parallel=4)


@pytest.fixture
def manager(settings):
    m = JobManager(settings)
    yield m
    m.shutdown()
