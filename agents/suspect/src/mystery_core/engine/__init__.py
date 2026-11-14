"""Game rules for the mystery. Pure Python: no Discord, Agent Engine or
ElevenLabs imports, and no direct database access (everything goes through a
`GameStore`, see ports.py).

Entry points used by the bot, the agent tools and the local runner:

    load_case(store, case_id)        case.py   (cases live in Atlas)
    prepare_interrogate              turn.py   (/interrogate)
    prepare_present                  turn.py   (/present, with correction)
    format_memories                  turn.py   (search_memories output)
    check_decision, allowed_decisions rules.py (confession gate, submit_reply)
    validate_offer                   actions.py (submit_reply)
    use_offer                        actions.py (button click)
    search_room, discover, notebook  actions.py (/search, /discover, /notebook)
    team_findings                    actions.py (/present autocomplete)
    trigger_event                    events.py (/event, host only)
    run_round                        gossip.py (/round, host only)
    accuse, is_eliminated            game.py   (/accuse)
"""
