import json
from pathlib import Path

import pytest

from mystery_core.memory import FakeStore, Scope

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mini_case() -> dict:
    return json.loads((FIXTURES / "mini_case.json").read_text())


@pytest.fixture
async def store(mini_case) -> FakeStore:
    s = FakeStore(cases={mini_case["_id"]: mini_case})
    await s.seed_world("case_test", "A")
    await s.seed_world("case_test", "B")
    return s


@pytest.fixture
def team_a() -> Scope:
    return Scope(case_id="case_test", team_id="A")


@pytest.fixture
def team_b() -> Scope:
    return Scope(case_id="case_test", team_id="B")
