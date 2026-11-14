"""Versioned prompt templates. Bump the file name to change a prompt, so old
traces still identify what produced them (AGENTS.md conventions)."""
from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

_DIR = Path(__file__).parent

TURN = "turn_v1"
CONTRADICTION = "contradiction_v1"
CORRECTION = "correction_v1"
NEW_LIE = "new_lie_v1"


@cache
def template(name: str) -> Template:
    return Template((_DIR / f"{name}.txt").read_text(encoding="utf-8"))


def render(template_name: str, /, **values: str) -> str:
    return template(template_name).substitute(**values)
