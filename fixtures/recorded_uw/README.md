# Recorded Unusual Whales data (PRIVATE, optional, not in git)

This directory may hold a **locally built, private** capture of real Unusual
Whales API responses (`uw_capture_2026-10-15_17.json.gz`). It is **git-ignored
and never distributed**: it is third-party market data, and redistribution
rights have not been established for this MIT-licensed repository.

- Build it yourself from a database your own Live-mode install captured:
  `python tools/build_recorded_uw_fixture.py --db <tip_hackathon.db> --out fixtures/recorded_uw/uw_capture_2026-10-15_17.json.gz`
  (read-only; market-data fields only; wire-format names).
- When present, the private replay tests run (`backend/tests/test_e2e_live_replay.py`,
  the recorded-data cases in `test_flow_direction.py`, `test_darkpool_session_baseline.py`,
  `test_options_session_baseline.py`). When absent they **skip** cleanly.
- Public end-to-end coverage uses the fully synthetic fixture in
  `fixtures/synthetic_uw/` (`tools/build_synthetic_uw_fixture.py`, deterministic),
  exercised by `backend/tests/test_e2e_live_replay_synthetic.py` and the browser test
  `frontend/test_e2e_live_replay_cdp.py`.
