"""The mode registry.

Import the mode modules once here so they register themselves, and the session builder
renders its checklist from this list — which is what makes "modes are optional per training
run" structural instead of something to remember.
"""

from __future__ import annotations

from app.modes.base import BuildContext, Mode, Outcome
from app.modes.confusion import Confusion
from app.modes.listen import Listen
from app.modes.match import Match3, Match6
from app.modes.mc4 import MultipleChoice
from app.modes.recall_self import RecallSelf
from app.modes.reverse import Reverse
from app.modes.study import StudyCard

_REGISTRY: dict[str, Mode] = {}


def register(mode: Mode) -> Mode:
    if not mode.key:
        raise ValueError("a mode needs a key")
    _REGISTRY[mode.key] = mode
    return mode


def all_modes() -> list[Mode]:
    """Every known mode, gentlest first — the order the session builder shows them in."""
    return sorted(_REGISTRY.values(), key=lambda mode: (mode.demand, mode.key))


def get(key: str) -> Mode | None:
    return _REGISTRY.get(key)


def available(pool_size: int, *, keys: list[str] | None = None) -> list[Mode]:
    """Modes that this many people can actually support."""
    modes = [get(key) for key in keys] if keys else all_modes()
    return [mode for mode in modes if mode is not None and pool_size >= mode.min_people]


for _mode in (
    StudyCard(),
    MultipleChoice(),
    Confusion(),
    Match3(),
    Reverse(),
    Match6(),
    Listen(),
    RecallSelf(),
):
    register(_mode)

__all__ = [
    "BuildContext",
    "Mode",
    "Outcome",
    "all_modes",
    "available",
    "get",
    "register",
]
