import pytest

from conftest import case_doc
from mystery_core.memory import FakeStore
from mystery_core.engine.case import (Case, CaseNotFound, Truth, check_truth, load_case)


def without_truth(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "truth"}


async def test_case_loads_from_the_store(case):
    assert case.id == "case_test"
    assert len(case.suspects) == 4
    assert case.killer_suspect().id == "butler"
    assert not case.room("wine_cellar").open


async def test_missing_case_fails_loudly():
    with pytest.raises(CaseNotFound):
        await load_case(FakeStore(), "case_nothing")


async def test_truth_never_reaches_a_case(store, case):
    assert "truth" not in await store.get_case("case_test")
    assert not hasattr(case, "truth")
    with pytest.raises(ValueError, match="includes `truth`"):
        Case.model_validate(case_doc())


async def test_check_truth(case):
    check_truth(case, Truth(killer="butler", weapon="poker", room="office"))
    with pytest.raises(ValueError, match="truth.weapon"):
        check_truth(case, Truth(killer="butler", weapon="banana", room="office"))
    with pytest.raises(ValueError, match="truth.killer"):
        check_truth(case, Truth(killer="cook", weapon="poker", room="office"))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["evidence"][0].update(room="attic"), "unknown room"),
    (lambda d: d["evidence"][0].update(unlocks=["fly_away"]), "unlocks unknown action"),
    (lambda d: d["actions"][0].update(reveals=["ev_nothing"]), "reveals unknown evidence"),
    (lambda d: d["rooms"].append({"id": "attic", "name": "the attic", "open": False}),
     "not reachable"),
    (lambda d: d["suspects"][1].update(is_killer=True), "exactly one suspect"),
    (lambda d: d["suspects"][1].update(confession="I did it."), "only the killer may have a confession"),
    (lambda d: d["events"][0].update(ref="ev_ledger"), "breaks a cover story"),
    (lambda d: d["seed_memories"][0].update(known_by=["suspect:nobody"]), "unknown viewer"),
    (lambda d: d["events"][0].update(ref="ev_nothing"), "unknown ref"),
    (lambda d: d["evidence"][1].update(contradicts={"s_nothing": None}), "contradicts unknown"),
    (lambda d: d["seed_memories"][0].update(known_by=["suspect:cook"]),
     "cover story must be held only by the killer"),
])
async def test_validation_catches_broken_cases(mutate, message):
    d = without_truth(case_doc())
    mutate(d)
    with pytest.raises(ValueError, match=message):
        Case.model_validate(d)


async def test_real_case_handoff_file_is_valid():
    """The case Person 1 uploads must pass the same validator (skipped once the
    handoff file has been deleted after upload)."""
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "handoff" / "case_manor.json"
    if not path.exists():
        pytest.skip("handoff file already uploaded and removed")
    doc = json.loads(path.read_text(encoding="utf-8"))
    c = Case.model_validate(without_truth(doc))
    check_truth(c, Truth.model_validate(doc["truth"]))
