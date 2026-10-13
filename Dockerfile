FROM python:3.11-slim

WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY fixtures fixtures

# Isolated MCP-protocol venv, deliberately NOT merged into backend's requirements
# (see mcp_isolated/requirements.txt for why). Built here so the real MCP
# transport actually works at runtime -- without this the main app's
# mcp_tool_client.py raises MCPSubprocessError and INVESTIGATE_TRANSPORT=mcp
# is non-functional in the container.
COPY mcp_isolated mcp_isolated
RUN python -m venv mcp_isolated/venv \
    && mcp_isolated/venv/bin/pip install --no-cache-dir -r mcp_isolated/requirements.txt

ENV PYTHONPATH=/srv/backend \
    TIP_MODE=demo \
    TIP_FIXTURES_DIR=/srv/fixtures \
    TIP_DB_PATH=/srv/data/tip_hackathon.db

RUN mkdir -p /srv/data

EXPOSE 8000

WORKDIR /srv/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
