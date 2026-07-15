"""Production inventory composition, ported from nintent (Phase 2 Step 2).

`contract.py` and `composer.py` are pure ports of nintent's
`production_inventory_contract.py` and `production_inventory.py` — the
Job-input byte contract (`parse_profile_job_input` and friends) is not
ported; see `profiles.py`'s docstring for why. `adapter.py` replaces the ORM
adapter (`jobs.py::_build_production_node_inputs`) with one that reads a
`nctl_core.sources.snapshot.SourceSnapshot` instead.
"""
