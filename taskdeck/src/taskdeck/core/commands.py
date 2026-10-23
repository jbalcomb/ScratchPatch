"""Command registry and fuzzy matching behind the command palette.

Query prefixes work like VS Code's quick open:

* ``>``  actions only (navigate, pin, theme, ...)
* ``@``  jobs only
* no prefix: everything, jobs first on ties
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable


class CommandKind(str, Enum):
    JOB = "job"
    QUICK = "quick"  # a job preset
    ACTION = "action"


PREFIXES = {">": {CommandKind.ACTION}, "@": {CommandKind.JOB, CommandKind.QUICK}}


@dataclass
class Command:
    id: str
    title: str
    run: Callable[[], None]
    kind: CommandKind = CommandKind.ACTION
    subtitle: str = ""
    icon: str = "CHEVRON_RIGHT"
    keywords: tuple[str, ...] = ()
    shortcut: str = ""
    # Whether the command is applicable right now (e.g. "Unpin current job").
    when: Callable[[], bool] = field(default=lambda: True)

    @property
    def haystack(self) -> str:
        return " ".join([self.title, self.subtitle, *self.keywords])


@dataclass
class Match:
    command: Command
    score: float
    positions: list[int]  # matched character indexes in command.title, for highlighting


def fuzzy_score(query: str, text: str) -> tuple[float, list[int]] | None:
    """Score how well ``query`` matches ``text`` as an in-order subsequence.

    Returns ``None`` when not every query character is found. Higher is better:
    consecutive runs, word starts and an early first hit all add bonus points,
    gaps cost a little. Case-insensitive.
    """
    q = query.lower().replace(" ", "")
    if not q:
        return 0.0, []
    t = text.lower()
    positions: list[int] = []
    score = 0.0
    ti = 0
    prev = -2
    for ch in q:
        idx = t.find(ch, ti)
        if idx < 0:
            return None
        # Prefer a later occurrence at a word start if it is close by.
        ws = _next_word_start(t, ch, ti)
        if ws is not None and ws != idx and ws - idx <= 8 and prev != idx - 1:
            idx = ws
        bonus = 1.0
        if idx == prev + 1:
            bonus += 2.0  # consecutive
        if idx == 0 or not t[idx - 1].isalnum():
            bonus += 1.5  # word start
        if prev >= 0:
            bonus -= min(idx - prev - 1, 5) * 0.1  # gap penalty
        score += bonus
        positions.append(idx)
        prev = idx
        ti = idx + 1
    score -= positions[0] * 0.05  # earlier is better
    score += 2.0 if t.startswith(q) else 0.0
    return score, positions


def keyword_score(query: str, text: str) -> float | None:
    """Looser match against subtitles and keywords: every query word must occur
    as a substring. Scored below a typical title hit so titles win ties."""
    terms = query.lower().split()
    if not terms:
        return 0.0
    hay = text.lower()
    words = re.findall(r"\w+", hay)
    score = 0.0
    for t in terms:
        if t not in hay:
            return None
        score += 1.5 if any(w.startswith(t) for w in words) else 0.75
    return score


def _next_word_start(t: str, ch: str, start: int) -> int | None:
    for i in range(start, len(t)):
        if t[i] == ch and (i == 0 or not t[i - 1].isalnum()):
            return i
    return None


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def add(self, command: Command) -> Command:
        self._commands[command.id] = command
        return command

    def extend(self, commands: Iterable[Command]) -> None:
        for c in commands:
            self.add(c)

    def remove_prefix(self, prefix: str) -> None:
        for cid in [c for c in self._commands if c.startswith(prefix)]:
            del self._commands[cid]

    def get(self, command_id: str) -> Command | None:
        return self._commands.get(command_id)

    def __len__(self) -> int:
        return len(self._commands)

    def search(self, query: str, usage: dict[str, int] | None = None, limit: int = 50) -> list[Match]:
        """Rank applicable commands for ``query``.

        ``usage`` maps command ids to how often they were used; frequently used
        commands get a small boost so the palette learns the user's habits.
        """
        usage = usage or {}
        kinds = None
        query = query.strip()
        if query[:1] in PREFIXES:
            kinds = PREFIXES[query[0]]
            query = query[1:].strip()
        matches: list[Match] = []
        for c in self._commands.values():
            if kinds and c.kind not in kinds:
                continue
            if not c.when():
                continue
            title_hit = fuzzy_score(query, c.title)
            if title_hit is not None:
                score, pos = title_hit
            else:
                kw = keyword_score(query, c.haystack)
                if kw is None:
                    continue
                score, pos = kw, []  # keyword hit, nothing to highlight in title
            score += min(usage.get(c.id, 0), 10) * 0.3
            if c.kind == CommandKind.JOB:
                score += 0.25
            matches.append(Match(c, score, pos))
        matches.sort(key=lambda m: (-m.score, m.command.title.lower()))
        return matches[:limit]
