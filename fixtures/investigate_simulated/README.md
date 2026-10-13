# Simulated 'Investigate with AI' transcripts

These JSON files are pre-written, static example outputs of the bounded
AI-investigation feature (`backend/app/ai/`). They are served verbatim
whenever the running app is in demo mode or `PUBLIC_DEMO=1` -- see
`backend/app/ai/simulated.py` and `backend/app/main.py`'s
`POST /api/investigate/{ticker}` route.

**No file in this directory is produced by calling a real LLM or the real
Unusual Whales API.** They exist so the feature's UI/output shape can be
demonstrated with zero credentials and zero network calls, which is a hard
requirement for the public static demo build. Every one of these is
labeled `"simulated": true` and carries a `"simulated_label"` string that
the frontend renders prominently, not as a footnote.

One file per existing synthetic Market Radar scenario ticker (ZYNC call
sweep, QRVX dark-pool anomaly, FLUX multi-signal), plus `_default.json` for
any other ticker loaded in demo mode.
