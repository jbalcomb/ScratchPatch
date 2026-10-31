"""Settings, all from environment variables (NSSM sets them with AppEnvironmentExtra)."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Where the binaries to analyse live. Every `file` parameter is a path relative to this folder.
    input_dir: Path
    # idaw.exe (text-mode IDA, 32-bit). IDABRIDGE_IDA_CMD overrides it with a full command prefix (tests use it).
    ida_cmd: list[str]
    # Databases, jobs and logs.
    home: Path
    token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    max_parallel: int = 8
    default_timeout: int = 900          # hard limit per IDA run, seconds
    max_timeout: int = 7200
    default_wait: int = 50              # how long a request waits for the result before answering "still running"
    job_retention_hours: int = 72
    allow_raw_idc: bool = False         # the run_idc escape hatch
    use_log_switch: bool = True         # pass -L<log> to IDA
    allowed_hosts: list[str] = field(default_factory=list)

    @property
    def db_dir(self) -> Path:
        return self.home / "db"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    @property
    def idc_dir(self) -> Path:
        return Path(__file__).parent / "idc"


def load() -> Settings:
    input_dir = _env("IDA_INPUT_DIR")
    if not input_dir:
        raise SystemExit("Set IDA_INPUT_DIR to the folder that holds the binaries to analyse.")

    cmd = _env("IDABRIDGE_IDA_CMD")
    if cmd:
        ida_cmd = shlex.split(cmd, posix=(sys.platform != "win32"))
    else:
        ida_dir = _env("IDA_DIR", r"C:\Program Files\IDA 5.5")
        ida_cmd = [str(Path(ida_dir) / "idaw.exe")]

    default_home = Path(os.environ.get("PROGRAMDATA", Path.home())) / "idabridge"
    hosts = _env("IDABRIDGE_ALLOWED_HOSTS", "")
    return Settings(
        input_dir=Path(input_dir).resolve(),
        ida_cmd=ida_cmd,
        home=Path(_env("IDABRIDGE_HOME", str(default_home))).resolve(),
        token=_env("IDABRIDGE_TOKEN"),
        host=_env("IDABRIDGE_HOST", "127.0.0.1"),
        port=int(_env("IDABRIDGE_PORT", "8765")),
        max_parallel=int(_env("IDABRIDGE_MAX_PARALLEL", "8")),
        default_timeout=int(_env("IDABRIDGE_TIMEOUT", "900")),
        max_timeout=int(_env("IDABRIDGE_MAX_TIMEOUT", "7200")),
        default_wait=int(_env("IDABRIDGE_WAIT", "50")),
        job_retention_hours=int(_env("IDABRIDGE_RETENTION_HOURS", "72")),
        allow_raw_idc=_bool("IDABRIDGE_ALLOW_IDC", False),
        use_log_switch=_bool("IDABRIDGE_LOG_SWITCH", True),
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
    )
