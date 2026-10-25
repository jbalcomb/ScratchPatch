"""The job list (jobs.toml): what may be run, and how to turn a request into argv."""

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = re.compile(r"\{(\w+)\}")


class BadRequest(Exception):
    """The request names an unknown job or has bad parameters. The message is safe to show."""


@dataclass(frozen=True)
class Job:
    name: str
    title: str
    command: tuple[str, ...]
    cwd: Path
    params: dict[str, re.Pattern]
    quick: bool = False
    timeout: float | None = None

    def argv(self, values):
        values = dict(values or {})
        unknown = set(values) - set(self.params)
        if unknown:
            raise BadRequest(f"{self.name} doesn't take: {', '.join(sorted(unknown))}")
        for name, pattern in self.params.items():
            value = values.get(name)
            if not isinstance(value, str) or not value:
                raise BadRequest(f"{self.name} needs {name}")
            if not pattern.fullmatch(value):
                raise BadRequest(f"{name} must match {pattern.pattern}")
        values["python"] = sys.executable
        return [PLACEHOLDER.sub(lambda m: values[m.group(1)], part) for part in self.command]

    def describe(self):
        return {"name": self.name, "title": self.title, "quick": self.quick,
                "params": {k: p.pattern for k, p in self.params.items()}}


def load(path):
    path = Path(path).resolve()
    with open(path, "rb") as f:
        config = tomllib.load(f)
    jobs = {}
    for name, spec in (config.get("jobs") or {}).items():
        params = {k: re.compile(v) for k, v in (spec.get("params") or {}).items()}
        command = tuple(spec["command"])
        for part in command:
            for placeholder in PLACEHOLDER.findall(part):
                if placeholder != "python" and placeholder not in params:
                    raise ValueError(f"{path}: job {name} uses {{{placeholder}}} but has no such param")
        quick = bool(spec.get("quick", False))
        jobs[name] = Job(
            name=name,
            title=spec.get("title", name),
            command=command,
            cwd=(path.parent / spec.get("cwd", ".")).resolve(),
            params=params,
            quick=quick,
            timeout=spec.get("timeout", 60 if quick else None),
        )
    return jobs
