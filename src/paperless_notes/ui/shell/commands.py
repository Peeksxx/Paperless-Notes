"""The command registry behind the search palette's > mode.

Every command has a visible route elsewhere (menu, button or panel); shortcuts only accelerate them.
Providers add commands that depend on the moment, such as "Go to note" for open and recent notes and the
editing commands of the active note.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    run: Callable[[], object] = field(compare=False)
    shortcut: str = ""
    category: str = "Commands"
    keywords: tuple[str, ...] = ()


def _score(query: str, command: Command) -> int:
    """Higher is better; 0 means no match. Every query word must match the title or a keyword."""
    title = command.title.casefold()
    haystacks = [title, *(k.casefold() for k in command.keywords), command.category.casefold()]
    total = 0
    for word in query.casefold().split():
        best = 0
        for index, text in enumerate(haystacks):
            weight = 3 if index == 0 else 2
            if text.startswith(word):
                best = max(best, 40 * weight)
            elif f" {word}" in f" {text}":
                best = max(best, 30 * weight)
            elif word in text:
                best = max(best, 15 * weight)
            elif _subsequence(word, text):
                best = max(best, 4 * weight)
        if best == 0:
            return 0
        total += best
    return total - len(title) // 8


def _subsequence(word: str, text: str) -> bool:
    it = iter(text)
    return all(ch in it for ch in word)


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._providers: list[Callable[[], list[Command]]] = []

    def add(self, command: Command) -> Command:
        self._commands[command.id] = command
        return command

    def add_provider(self, provider: Callable[[], list[Command]]) -> None:
        self._providers.append(provider)

    def find(self, command_id: str) -> Command | None:
        return self._commands.get(command_id)

    def all(self) -> list[Command]:
        dynamic = [c for provider in self._providers for c in provider()]
        return [*self._commands.values(), *dynamic]

    def search(self, query: str, limit: int = 12) -> list[Command]:
        commands = self.all()
        if not query.strip():
            return commands[:limit]
        scored = [(_score(query, c), i, c) for i, c in enumerate(commands)]
        ranked = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
        return [c for _, _, c in ranked[:limit]]
