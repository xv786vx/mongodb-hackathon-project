from conftest import allowed, all_in, begin
from mystery_core.engine.actions import discover
from mystery_core.engine.turn import prepare_interrogate, prepare_present


async def test_prompt_has_no_truth_and_innocents_cannot_lie(store, case, turns, traces, A):
    for i, s in enumerate(case.suspects):
        prepared = await turns.prepare(await begin(traces, A, s.id, f"t_{i}"))
        assert "poker" not in prepared.system and '"is_killer"' not in prepared.system
        assert case.suspect("butler").confession not in prepared.system
        decisions = await allowed(store, A, s.id)
        assert ("lie" in decisions) == s.is_killer and "crack" not in decisions


async def test_unknown_suspect_is_refused(store, case, A):
    prep = await prepare_interrogate(store, case, A, "mayor", "hello")
    assert not prep.ok and "no suspect" in prep.error


async def test_present_requires_a_finding(store, case, A):
    prep = await prepare_present(store, case, A, "butler", "ev_ledger")
    assert not prep.ok and prep.error == "Your team hasn't found the ledger."


async def test_two_contradictions_open_crack_for_that_team_only(store, case, turns, traces, A, B):
    await discover(store, case, A, "ev_ledger")
    await discover(store, case, A, "ev_glove")

    first = await prepare_present(store, case, A, "butler", "ev_ledger")
    assert first.ok and len(first.superseded) == 1
    assert "crack" not in await allowed(store, A, "butler")
    new_lie = first.fresh[-1]
    assert new_lie.kind == "cover_story" and new_lie.source.type == "fabricated"
    assert new_lie.text == case.suspect("butler").fallback_lies[0]

    # Presenting the same item again must not break the story a second time.
    again = await prepare_present(store, case, A, "butler", "ev_ledger")
    assert again.superseded == []
    assert "crack" not in await allowed(store, A, "butler")

    second = await prepare_present(store, case, A, "butler", "ev_glove")
    assert len(second.superseded) == 1
    assert "crack" in await allowed(store, A, "butler")
    # The confession reaches the prompt only now, and only in Team A's world.
    prepared_a = await turns.prepare(await begin(traces, A, "butler", "t_a"))
    prepared_b = await turns.prepare(await begin(traces, B, "butler", "t_b"))
    assert case.suspect("butler").confession in prepared_a.system
    assert case.suspect("butler").confession not in prepared_b.system

    # Team B's butler is untouched.
    assert "crack" not in await allowed(store, B, "butler")
    [cover_b] = await store.find_by_ref(B, "suspect:butler", "s_butler_cover", kinds=["cover_story"])
    assert cover_b.source.type == "seed"


async def test_present_corrects_a_wrong_belief_in_one_world(store, case, A, B):
    await discover(store, case, A, "ev_ticket")
    prep = await prepare_present(store, case, A, "cook", "ev_ticket", "Who did you hear?")
    assert len(prep.superseded) == 1
    old_id, new_id = prep.superseded[0]
    assert store.docs[old_id].status == "superseded"
    assert store.docs[old_id].superseded_by == new_id
    assert "on the train" in store.docs[new_id].text
    # Old text is never edited.
    assert "know her voice anywhere" in store.docs[old_id].text

    # Team B's cook still holds her original belief.
    b_hits = await store.retrieve(B, "suspect:cook", "niece shouting office")
    assert any("know her voice anywhere" in m.text for m in b_hits)
    a_hits = await store.retrieve(A, "suspect:cook", "niece shouting office")
    assert not any("know her voice anywhere" in m.text for m in a_hits)


async def test_present_with_llm_contradiction_only_accepts_scoped_candidates(store, case, A):
    # ev_draught declares contradicts {}, so make one with no declaration.
    case.evidence_item("ev_poker").contradicts = None
    await discover(store, case, A, "ev_poker")

    async def rogue_llm(prompt: str) -> str:
        return '{"contradicts": true, "memory_id": "m_test_B_s_cook_quarrel"}'

    prep = await prepare_present(store, case, A, "cook", "ev_poker", llm=rogue_llm)
    assert prep.ok and prep.superseded == []
    assert store.docs["m_test_B_s_cook_quarrel"].status == "active"


async def test_confession_is_never_a_searchable_memory(store, case, A, B):
    # The deed lives on the killer's case entry, not in any world, so search_memories
    # cannot surface it before the gate opens.
    confession = case.suspect("butler").confession
    assert confession
    for scope in (A, B):
        assert all(m.text != confession for m in all_in(store, scope))
    hits = await store.retrieve(A, "suspect:butler", confession, k=20)
    assert all(m.text != confession for m in hits)


async def test_present_retry_after_partial_failure_breaks_the_story_once(store, case, A):
    await discover(store, case, A, "ev_ledger")
    real_write = store.write

    async def crash_on_evidence(scope, memories, **kw):
        if any(m.kind == "evidence" for m in memories):
            raise RuntimeError("crash after the supersede, before the evidence memory")
        return await real_write(scope, memories, **kw)

    store.write = crash_on_evidence
    try:
        await prepare_present(store, case, A, "butler", "ev_ledger")
    except RuntimeError:
        pass
    store.write = real_write
    assert await store.count_superseded(A, "suspect:butler", "cover_story") == 1

    retry = await prepare_present(store, case, A, "butler", "ev_ledger")
    assert retry.ok and retry.superseded == []
    assert await store.count_superseded(A, "suspect:butler", "cover_story") == 1
    assert await store.find_by_ref(A, "suspect:butler", "ev_ledger", kinds=["evidence"])
    assert "crack" not in await allowed(store, A, "butler")


async def test_second_clue_breaks_the_replacement_lie(store, case, A):
    # The new lie is tagged with ev_ledger, not the seed ref; ev_glove must still find it.
    await discover(store, case, A, "ev_ledger")
    await discover(store, case, A, "ev_glove")
    first = await prepare_present(store, case, A, "butler", "ev_ledger")
    assert first.fresh[-1].source.ref == "ev_ledger"
    second = await prepare_present(store, case, A, "butler", "ev_glove")
    assert second.superseded and second.superseded[0][0] == first.fresh[-1].id
    [current] = await store.list_by_viewer(A, "suspect:butler", kinds=["cover_story"])
    assert current.text == case.suspect("butler").fallback_lies[1]
