"""Plugin commonality review.

Looks across every installed job plugin for:

* **duplicate code** - functions in different plugins (or a plugin and
  ``taskdeck.shared``) whose normalized AST is identical or very similar;
  these are candidates to extract into (or reuse from) ``taskdeck.shared``.
* **repeated step runs** - the same sequence of step keys appearing in several
  pipelines; candidates for a composite shared step.
* **pipeline shapes** - stage sequences shared by many plugins; candidates for
  a pipeline template in the core.
* **hygiene** - per-plugin checks (missing metadata, stages out of order, no
  presentation step, ...).

It is pure analysis: it never edits code. Run it with ``python -m taskdeck.review``.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import inspect
import re
import textwrap
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from types import ModuleType

from taskdeck.core.pipeline import Stage
from taskdeck.core.plugin import JobPlugin, PluginRegistry

_BUILTINS = set(dir(builtins))
_TOKEN = re.compile(r"\w+|[^\w\s]")


@dataclass
class FunctionInfo:
    owner: str  # plugin id, or "shared"
    module: str
    name: str
    lineno: int
    tokens: list[str] = field(repr=False)
    is_step: bool = False

    @property
    def ref(self) -> str:
        return f"{self.module}:{self.name} (line {self.lineno})"


@dataclass
class Duplicate:
    a: str
    b: str
    similarity: float
    kind: str  # "extract" (plugin vs plugin) or "reuse" (plugin vs shared)


@dataclass
class StepRun:
    keys: tuple[str, ...]
    plugins: list[str]


@dataclass
class ShapeGroup:
    shape: tuple[str, ...]
    plugins: list[str]


@dataclass
class Issue:
    plugin: str
    message: str


@dataclass
class ReviewReport:
    plugins: list[str]
    shared_adoption: dict[str, float]
    duplicates: list[Duplicate]
    step_runs: list[StepRun]
    shapes: list[ShapeGroup]
    issues: list[Issue]
    load_errors: dict[str, str]

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def actionable(self) -> int:
        return len(self.duplicates) + len(self.step_runs) + len(self.issues) + len(self.load_errors)


# ---- normalization ----------------------------------------------------------

class _Normalizer(ast.NodeTransformer):
    """Rename local identifiers to positional placeholders and drop docstrings,
    so two functions that differ only in naming compare equal."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def _alias(self, name: str) -> str:
        if name in _BUILTINS:
            return name
        return self.names.setdefault(name, f"v{len(self.names)}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.name = "fn"
        node.decorator_list = []
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = self._alias(node.arg)
        node.annotation = None
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = self._alias(node.id)
        return node


def _tokens(fn_node: ast.AST) -> list[str]:
    norm = _Normalizer().visit(fn_node)
    return _TOKEN.findall(ast.dump(norm, annotate_fields=False, include_attributes=False))


def _functions_in(module: ModuleType, owner: str, step_names: set[str]) -> list[FunctionInfo]:
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):
        return []  # e.g. compiled-only build; nothing to compare
    tree = ast.parse(textwrap.dedent(source))
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("__"):
            out.append(FunctionInfo(owner, module.__name__, node.name, node.lineno, _tokens(node), node.name in step_names))
    return out


# ---- analysis ---------------------------------------------------------------

def similarity(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def find_duplicates(funcs: list[FunctionInfo], threshold: float, min_tokens: int) -> list[Duplicate]:
    dups = []
    candidates = [f for f in funcs if len(f.tokens) >= min_tokens]
    for i, fa in enumerate(candidates):
        for fb in candidates[i + 1:]:
            if fa.owner == fb.owner or (fa.owner == "shared" and fb.owner == "shared"):
                continue
            # Cheap length filter before the quadratic diff.
            la, lb = len(fa.tokens), len(fb.tokens)
            if min(la, lb) / max(la, lb) < threshold:
                continue
            score = similarity(fa.tokens, fb.tokens)
            if score >= threshold:
                kind = "reuse" if "shared" in (fa.owner, fb.owner) else "extract"
                dups.append(Duplicate(fa.ref, fb.ref, round(score, 3), kind))
    return sorted(dups, key=lambda d: -d.similarity)


def find_step_runs(pipelines: dict[str, list[str]], min_len: int = 2, min_plugins: int = 2) -> list[StepRun]:
    """Contiguous step-key sequences used by at least ``min_plugins`` pipelines.

    Only maximal runs are reported (a run contained in a longer run shared by the
    same plugins is dropped).
    """
    seen: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for pid, keys in pipelines.items():
        for n in range(min_len, len(keys) + 1):
            for i in range(len(keys) - n + 1):
                seen[tuple(keys[i:i + n])].add(pid)
    common = {k: v for k, v in seen.items() if len(v) >= min_plugins}
    maximal = []
    for run, owners in common.items():
        covered = any(
            len(other) > len(run) and owners <= other_owners and _contains(other, run)
            for other, other_owners in common.items()
        )
        if not covered:
            maximal.append(StepRun(run, sorted(owners)))
    return sorted(maximal, key=lambda r: (-len(r.plugins), -len(r.keys), r.keys))


def _contains(seq: tuple[str, ...], sub: tuple[str, ...]) -> bool:
    return any(seq[i:i + len(sub)] == sub for i in range(len(seq) - len(sub) + 1))


def check_plugin(plugin: JobPlugin) -> list[str]:
    problems = []
    if not plugin.title:
        problems.append("missing title")
    if len(plugin.description) < 10:
        problems.append("description is missing or too short to be useful in search")
    if not plugin.tags:
        problems.append("no tags: the command palette can only match on title")
    if not plugin.fields:
        problems.append("declares no input fields")
    try:
        pipe = plugin.build_pipeline()
    except Exception as exc:
        return problems + [f"build_pipeline() raised {type(exc).__name__}: {exc}"]
    stages = [s.stage for s in pipe]
    if Stage.PRESENT not in stages:
        problems.append("pipeline has no PRESENT step, so nothing is shown to the user")
    if Stage.VALIDATE not in stages and any(f.required for f in plugin.fields):
        problems.append("required fields but no VALIDATE step")
    orders = [s.order for s in stages]
    if orders != sorted(orders):
        problems.append("steps are not in canonical stage order (validate, prepare, execute, present)")
    names = [f.name for f in plugin.fields]
    if len(names) != len(set(names)):
        problems.append("duplicate field names")
    return problems


def review(registry: PluginRegistry, threshold: float = 0.85, min_tokens: int = 40,
           shared_modules: tuple[ModuleType, ...] | None = None) -> ReviewReport:
    if shared_modules is None:
        from taskdeck.shared import steps as shared_steps, text as shared_text

        shared_modules = (shared_steps, shared_text)

    funcs: list[FunctionInfo] = []
    pipelines: dict[str, list[str]] = {}
    shapes: dict[tuple[str, ...], list[str]] = defaultdict(list)
    adoption: dict[str, float] = {}
    issues: list[Issue] = []
    modules_seen: set[str] = set()

    for plugin in registry:
        issues.extend(Issue(plugin.id, m) for m in check_plugin(plugin))
        try:
            pipe = plugin.build_pipeline()
        except Exception:
            continue
        keys = [s.key for s in pipe]
        pipelines[plugin.id] = keys
        shapes[tuple(s.value for s in pipe.shape)].append(plugin.id)
        adoption[plugin.id] = round(sum(k.startswith("shared.") for k in keys) / len(keys), 2) if keys else 0.0
        module = inspect.getmodule(type(plugin))
        if module and module.__name__ not in modules_seen:
            modules_seen.add(module.__name__)
            step_names = {getattr(s.fn, "__name__", "") for s in pipe if not s.key.startswith("shared.")}
            funcs.extend(_functions_in(module, plugin.id, step_names))

    for mod in shared_modules:
        funcs.extend(_functions_in(mod, "shared", set()))

    shape_groups = [ShapeGroup(k, sorted(v)) for k, v in shapes.items()]
    shape_groups.sort(key=lambda g: -len(g.plugins))

    return ReviewReport(
        plugins=[p.id for p in registry],
        shared_adoption=adoption,
        duplicates=find_duplicates(funcs, threshold, min_tokens),
        step_runs=find_step_runs(pipelines),
        shapes=shape_groups,
        issues=issues,
        load_errors=dict(registry.errors),
    )


# ---- rendering --------------------------------------------------------------

def to_markdown(report: ReviewReport) -> str:
    lines = ["# Plugin commonality review", "",
             f"Plugins reviewed: **{len(report.plugins)}**. Actionable findings: **{report.actionable}**.", ""]

    if report.load_errors:
        lines += ["## Plugins that failed to load", ""]
        lines += [f"- `{name}`: {err}" for name, err in report.load_errors.items()] + [""]

    lines += ["## Duplicate code", ""]
    if report.duplicates:
        lines += ["| Similarity | Suggestion | A | B |", "|---|---|---|---|"]
        for d in report.duplicates:
            hint = "use the existing shared helper" if d.kind == "reuse" else "extract to `taskdeck.shared`"
            lines.append(f"| {d.similarity:.0%} | {hint} | `{d.a}` | `{d.b}` |")
    else:
        lines.append("None above the similarity threshold.")
    lines.append("")

    lines += ["## Repeated step sequences", "",
              "Sequences of steps that several pipelines run back to back. A run shared by many "
              "plugins is a candidate for one composite shared step.", ""]
    if report.step_runs:
        for r in report.step_runs:
            lines.append(f"- {len(r.plugins)} plugins ({', '.join(r.plugins)}): " + " → ".join(f"`{k}`" for k in r.keys))
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Pipeline shapes", "",
              "Stage sequences. When most plugins share one shape, consider a pipeline template "
              "for it in `taskdeck.core`.", ""]
    for g in report.shapes:
        lines.append(f"- {len(g.plugins)} × `{' → '.join(g.shape)}`: {', '.join(g.plugins)}")
    lines.append("")

    lines += ["## Shared-step adoption", "", "| Plugin | Steps from `taskdeck.shared` |", "|---|---|"]
    lines += [f"| {pid} | {share:.0%} |" for pid, share in sorted(report.shared_adoption.items(), key=lambda x: x[1])]
    lines.append("")

    lines += ["## Plugin hygiene", ""]
    if report.issues:
        by_plugin: dict[str, list[str]] = defaultdict(list)
        for i in report.issues:
            by_plugin[i.plugin].append(i.message)
        for pid, msgs in by_plugin.items():
            lines.append(f"- **{pid}**: " + "; ".join(msgs))
    else:
        lines.append("All plugins pass.")
    lines.append("")
    return "\n".join(lines)


def summary_counts(report: ReviewReport) -> Counter:
    return Counter(duplicates=len(report.duplicates), step_runs=len(report.step_runs),
                   issues=len(report.issues), load_errors=len(report.load_errors))
