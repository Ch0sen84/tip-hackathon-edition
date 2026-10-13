"""Optional, isolated 'Investigate with AI' module.

Nothing in backend/app/main.py's core routes (/api/radar, /api/ticker/*,
/api/settings*) imports anything from this package at module scope beyond
what's needed to expose the one new /api/investigate/{ticker} route and a
feature-availability flag -- the rest of the app works unmodified with zero
AI key configured. See orchestrator.py for the bounded two-round design and
README.md ("Investigate with AI") for the transport decision (REST fallback,
not real MCP protocol -- see that doc for why).
"""
