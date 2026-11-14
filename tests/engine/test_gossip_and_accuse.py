from datetime import timedelta

from conftest import CASE_ID, all_in, make_store
from mystery_core.engine.game import accuse, is_eliminated
from mystery_core.engine.gossip import run_round
from mystery_core.memory.models import Scope


def rumours(store, scope):
    return [m for m in all_in(store, scope) if m.kind == "rumour"]


async def test_gossip_spreads_decays_and_expires(store, case, A, B):
    written = []
    for r in range(1, 6):
        written += await run_round(store, case, A, r)
    assert written, "five rounds should spread at least one rumour"
    for m in written:
        assert m.kind == "rumour" and m.unlocks == []
        assert len(m.known_by) == 1 and m.confidence <= 0.7
        assert m.source.type == "told_by"
        assert m.team_id == "A"
    assert rumours(store, B) == []  # gossip stays in its own world

    # The killer only ever spreads his cover story, never the deed.
    assert not any("struck the victim" in m.text for m in written)

    listener = written[0].known_by[0]
    assert await store.fetch(A, listener, [written[0].id])
    later = store.now() + timedelta(seconds=601)  # past the rumour TTL, before any TTL delete
    store.now = lambda: later
    assert await store.fetch(A, listener, [written[0].id]) == []


async def test_gossip_is_deterministic_per_round(case):
    texts = []
    for _ in range(2):
        s = await make_store("A")
        written = await run_round(s, case, Scope(case_id=CASE_ID, team_id="A"), 3)
        texts.append([m.text for m in written])
    assert texts[0] == texts[1]


async def test_accusation(store, case, A, B):
    wrong = await accuse(store, case, B, "cook", "poker", "office")
    assert wrong.status == "eliminated" and await is_eliminated(store, B)
    assert "poker" in wrong.message and "Bram" not in wrong.message

    right = await accuse(store, case, A, "butler", "poker", "office")
    assert right.status == "solved" and not await is_eliminated(store, A)
    assert (await accuse(store, case, A, "butler", "poker", "office")).status == "already_accused"
    assert (await accuse(store, case, A, "ghost", "poker", "office")).status == "invalid"
