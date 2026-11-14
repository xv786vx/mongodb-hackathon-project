import json
import sys
from pathlib import Path

import pytest

# Lets the test modules import helpers from this conftest.
sys.path.insert(0, str(Path(__file__).parent))

from mystery_core.engine.case import load_case  # noqa: E402
from mystery_core.memory import FakeStore  # noqa: E402
from mystery_core.memory.models import Memory, Scope  # noqa: E402

# Synthetic test case. The real cases live only in Atlas (mystery.cases).
CASE_PATH = Path(__file__).parent / "fixtures" / "case_test.json"
CASE_ID = "case_test"


def case_doc() -> dict:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


def all_in(store: FakeStore, scope: Scope) -> list[Memory]:
    """Every memory in one world, any status. Test inspection only."""
    return [m for m in store.docs.values() if m.scope == scope]


async def make_store(*teams: str) -> FakeStore:
    s = FakeStore(cases={CASE_ID: case_doc()})
    for t in teams:
        await s.seed_world(CASE_ID, t)
    return s


# Overrides the memory layer's `store` fixture (tests/conftest.py) for engine tests.
@pytest.fixture
async def store() -> FakeStore:
    return await make_store("A", "B")


@pytest.fixture
async def case(store):
    return await load_case(store, CASE_ID)


@pytest.fixture
def A() -> Scope:
    return Scope(case_id=CASE_ID, team_id="A")


@pytest.fixture
def B() -> Scope:
    return Scope(case_id=CASE_ID, team_id="B")


# ---- Person 2's agent turn (TurnService) on the engine's test case ----

from mystery_core.engine.context import TurnContext  # noqa: E402
from mystery_core.engine.turn import TurnService  # noqa: E402
from mystery_core.memory import FakeTraceStore  # noqa: E402


@pytest.fixture
def traces() -> FakeTraceStore:
    return FakeTraceStore()


@pytest.fixture
def turns(store, traces) -> TurnService:
    return TurnService(store, traces, model="test-model")


async def begin(traces, scope: Scope, suspect: str, turn_id: str, command: str = "interrogate",
                question: str = "Where were you after midnight?") -> TurnContext:
    """What the bot does first for every turn: open a pending trace."""
    ctx = TurnContext(scope, suspect, turn_id, command)
    await traces.begin_turn(scope, turn_id, ctx.viewer, command, question)
    return ctx


async def allowed(store, scope: Scope, suspect_id: str) -> list[str]:
    """TurnService.allowed_decisions for a suspect, the single source for the crack gate."""
    case = await store.get_case(scope.case_id)
    suspect = next(s for s in case["suspects"] if s["id"] == suspect_id)
    ctx = TurnContext(scope, suspect_id, "t_check", "interrogate")
    return await TurnService.allowed_decisions(store, ctx, suspect)
