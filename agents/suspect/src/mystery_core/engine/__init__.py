"""Governed turn rules shared by hosted and local suspect runners, and the game rules.

Pure Python: no Discord, Agent Engine or ElevenLabs imports.

Agent turn (Person 2):
    TurnService.prepare / search / submit   turn.py    (prompt, search_memories, submit_reply,
                                                        allowed decisions and the crack gate, offers)
    turn_context, TurnContext                context.py (identity from custom headers)
    use_offer                                actions.py (button click, atomic)
    discover, present_evidence               evidence.py (host staging / smoke test)
    apply_event                              events.py

Game rules (Person 3):
    load_case(store, case_id)                case.py    (cases live in Atlas)
    prepare_interrogate, prepare_present     present.py (re-exported from turn.py; /present runs
                                                        the contradiction step before the agent)
    find_contradiction, apply_contradiction  contradiction.py
    search_room, discover, notebook          actions.py (/search, /discover, /notebook)
    team_findings                            actions.py (/present autocomplete)
    trigger_event                            events.py  (/event, host only)
    run_round                                gossip.py  (/round, host only)
    accuse, is_eliminated                    game.py    (/accuse)
"""
