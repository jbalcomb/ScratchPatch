"""Job plugin contract, input fields and discovery.

A job plugin is a class deriving from :class:`JobPlugin` that declares metadata,
the input :class:`Field` list the UI renders as a form, and a
:meth:`JobPlugin.build_pipeline` method. Plugins live in sub-packages of
``taskdeck.jobs`` (auto-discovered) or are installed by third-party packages
under the ``taskdeck.jobs`` entry-point group.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, ClassVar, Iterable

from .pipeline import Context, Pipeline, ProgressFn, RunReport, StepError

log = logging.getLogger(__name__)


class FieldKind(str, Enum):
    TEXT = "text"
    MULTILINE = "multiline"
    NUMBER = "number"
    CHOICE = "choice"
    TOGGLE = "toggle"


@dataclass(frozen=True)
class Field:
    """One input the job asks for. The UI builds its form from these."""

    name: str
    label: str
    kind: FieldKind = FieldKind.TEXT
    required: bool = False
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()
    secret: bool = False  # masked in the UI; for settings such as API keys
    lines: int = 3  # visible lines for MULTILINE fields
    importable: bool = False  # offer "Import file" to fill the value from a file

    @classmethod
    def text(cls, name: str, label: str, **kw: Any) -> "Field":
        return cls(name, label, FieldKind.TEXT, **kw)

    @classmethod
    def multiline(cls, name: str, label: str, **kw: Any) -> "Field":
        return cls(name, label, FieldKind.MULTILINE, **kw)

    @classmethod
    def number(cls, name: str, label: str, **kw: Any) -> "Field":
        return cls(name, label, FieldKind.NUMBER, **kw)

    @classmethod
    def choice(cls, name: str, label: str, choices: Iterable[str], **kw: Any) -> "Field":
        choices = tuple(choices)
        kw.setdefault("default", choices[0] if choices else None)
        return cls(name, label, FieldKind.CHOICE, choices=choices, **kw)

    @classmethod
    def toggle(cls, name: str, label: str, **kw: Any) -> "Field":
        kw.setdefault("default", False)
        return cls(name, label, FieldKind.TOGGLE, **kw)

    def coerce(self, raw: Any) -> Any:
        """Convert a raw UI value into the field's Python type."""
        if self.kind == FieldKind.TOGGLE:
            return bool(raw)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            if self.required:
                raise StepError(f"{self.label} is required")
            return self.default
        if self.kind == FieldKind.NUMBER:
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise StepError(f"{self.label} must be a number") from None
        if self.kind == FieldKind.CHOICE and raw not in self.choices:
            raise StepError(f"{self.label} must be one of: {', '.join(self.choices)}")
        return raw


@dataclass(frozen=True)
class QuickAction:
    """A preset that shows up in the command palette, e.g. "Convert: km → mi".

    Running it opens the job with ``inputs`` pre-filled.
    """

    title: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    """Something that would make the job fail, found before it runs.

    ``blocking`` problems disable the run button. ``fix`` names where to fix it:
    "settings" adds an "Open settings" button, "recheck" only "Check again", and
    "job:<id>" a button that opens that job (e.g. a clean-up job).
    """

    message: str
    blocking: bool = True
    fix: str = "settings"


class NeedsConfirmation(StepError):
    """Raised by :meth:`JobPlugin.commit` when it could go ahead, but only with the user's OK
    (e.g. overwrite an existing row). The UI shows ``message`` and a ``confirm_label``
    button that commits again with ``payload``."""

    def __init__(self, message: str, confirm_label: str, payload: Any):
        super().__init__(message)
        self.confirm_label = confirm_label
        self.payload = payload


@dataclass(frozen=True)
class CommitOutcome:
    ok: bool
    message: str
    confirm: NeedsConfirmation | None = None


@dataclass(frozen=True)
class SettingsAction:
    """A button on the job's settings screen, e.g. "Test connection".

    ``run`` receives the settings as currently entered (saved or not) and returns
    text to show under the button. It runs off the UI thread.
    """

    label: str
    run: Callable[[dict[str, Any]], str]
    icon: str = "PLAY_ARROW"


class JobPlugin:
    """Base class every job derives from."""

    id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    category: ClassVar[str] = "General"
    icon: ClassVar[str] = "WORK_OUTLINE"  # name in flet.Icons
    tags: ClassVar[tuple[str, ...]] = ()
    fields: ClassVar[tuple[Field, ...]] = ()
    quick_actions: ClassVar[tuple[QuickAction, ...]] = ()
    # Per-job configuration (URLs, IDs, keys). Edited from the job's settings screen,
    # persisted by the host, and handed to steps as ``ctx.settings``.
    settings: ClassVar[tuple[Field, ...]] = ()
    settings_actions: ClassVar[tuple[SettingsAction, ...]] = ()
    # Use another job's stored settings (same fields) instead of keeping a separate copy.
    settings_from: ClassVar[str] = ""
    run_label: ClassVar[str] = "Run"
    # Two-phase jobs: the pipeline must be side-effect free and produce a preview whose
    # ``Result.payload`` is passed to :meth:`commit` once the user confirms.
    commit_label: ClassVar[str] = ""
    committed_label: ClassVar[str] = "Done"

    def build_pipeline(self) -> Pipeline:
        raise NotImplementedError

    @property
    def has_commit(self) -> bool:
        return bool(self.commit_label)

    def commit(self, payload: Any, settings: dict[str, Any]) -> str:
        """Perform the job's side effect. Raise StepError with a user-facing message on failure.

        Returns a short confirmation message.
        """
        raise NotImplementedError

    def check_ready(self, settings: dict[str, Any]) -> list[Problem]:
        """Fast, offline checks run when the job opens. Override and extend ``super()``."""
        return [Problem(f"Set “{f.label}” in this job's settings.")
                for f in self.settings
                if f.required and not str(settings.get(f.name) or "").strip()]

    def check_ready_online(self, settings: dict[str, Any]) -> list[Problem]:
        """Slower checks that may use the network (run in the background when the job opens).
        Only called when :meth:`check_ready` found nothing blocking."""
        return []

    def collect_inputs(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {f.name: f.coerce(raw.get(f.name, f.default)) for f in self.fields}

    def resolve_settings(self, stored: dict[str, Any] | None) -> dict[str, Any]:
        """Stored values merged over defaults. Not validated: steps check what they need."""
        stored = stored or {}
        return {f.name: stored.get(f.name, f.default) for f in self.settings}

    def run(self, raw_inputs: dict[str, Any], on_progress: ProgressFn | None = None,
            ctx: Context | None = None, settings: dict[str, Any] | None = None) -> RunReport:
        """Coerce inputs, build the pipeline and run it. Never raises for user errors."""
        try:
            inputs = self.collect_inputs(raw_inputs)
        except StepError as exc:
            return RunReport(False, None, [], str(exc))
        ctx = ctx or Context(inputs)
        ctx.inputs = inputs
        ctx.settings = self.resolve_settings(settings)
        return self.build_pipeline().run(ctx, on_progress)

    @property
    def settings_id(self) -> str:
        """The job id whose stored settings this job uses."""
        return self.settings_from or self.id

    def attempt_commit(self, payload: Any, settings: dict[str, Any] | None = None) -> CommitOutcome:
        """Run :meth:`commit`, turning any failure into an unsuccessful outcome."""
        try:
            return CommitOutcome(True, self.commit(payload, self.resolve_settings(settings)))
        except NeedsConfirmation as exc:
            return CommitOutcome(False, str(exc), exc)
        except StepError as exc:
            return CommitOutcome(False, str(exc))
        except Exception as exc:  # bug or unexpected I/O failure in the plugin
            return CommitOutcome(False, f"{type(exc).__name__}: {exc}")

    def try_commit(self, payload: Any, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
        outcome = self.attempt_commit(payload, settings)
        return outcome.ok, outcome.message

    @property
    def search_text(self) -> str:
        return " ".join([self.title, self.category, self.description, *self.tags])


class PluginRegistry:
    """Holds loaded plugins, keyed by id."""

    def __init__(self) -> None:
        self._plugins: dict[str, JobPlugin] = {}
        self.errors: dict[str, str] = {}

    def register(self, plugin: JobPlugin) -> None:
        if not plugin.id:
            raise ValueError(f"{type(plugin).__name__} has no id")
        if plugin.id in self._plugins:
            raise ValueError(f"duplicate plugin id {plugin.id!r}")
        self._plugins[plugin.id] = plugin

    def get(self, plugin_id: str) -> JobPlugin | None:
        return self._plugins.get(plugin_id)

    def __iter__(self):
        return iter(sorted(self._plugins.values(), key=lambda p: (p.category, p.title)))

    def __len__(self) -> int:
        return len(self._plugins)

    def by_category(self) -> dict[str, list[JobPlugin]]:
        out: dict[str, list[JobPlugin]] = {}
        for p in self:
            out.setdefault(p.category, []).append(p)
        return out

    def discover(self, package: str = "taskdeck.jobs", entry_point_group: str = "taskdeck.jobs") -> "PluginRegistry":
        """Load every plugin in ``package`` plus any installed via entry points.

        A broken plugin is recorded in :attr:`errors` and skipped so one bad job
        never takes the whole app down.
        """
        pkg = importlib.import_module(package)
        for info in pkgutil.iter_modules(pkg.__path__, prefix=f"{package}."):
            self._load(info.name, lambda name=info.name: importlib.import_module(name))
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group=entry_point_group):
                self._load(ep.name, ep.load)
        except Exception as exc:  # metadata may be unavailable on some Android builds
            log.debug("entry point discovery skipped: %s", exc)
        return self

    def _load(self, name: str, loader: Callable[[], Any]) -> None:
        try:
            obj = loader()
            for plugin in _plugins_from(obj):
                self.register(plugin)
        except Exception as exc:
            log.exception("failed to load plugin %s", name)
            self.errors[name] = f"{type(exc).__name__}: {exc}"


def _plugins_from(obj: Any) -> list[JobPlugin]:
    """Accept a module exposing ``PLUGINS``/``PLUGIN``, a plugin class, or an instance."""
    if isinstance(obj, JobPlugin):
        return [obj]
    if isinstance(obj, type) and issubclass(obj, JobPlugin):
        return [obj()]
    plugins = getattr(obj, "PLUGINS", None) or ([obj.PLUGIN] if hasattr(obj, "PLUGIN") else [])
    return [p for item in plugins for p in _plugins_from(item)]
