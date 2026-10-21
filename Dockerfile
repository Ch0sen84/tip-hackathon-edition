FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

# Build-time version/commit info (2026-10-15): there is no .git directory
# inside this image (only source is COPYed below), so the installed
# commit cannot be discovered at runtime by shelling out to git. These
# ARGs let the build pass in real values (e.g. `git rev-parse HEAD`) which
# get written to a plain /srv/VERSION file -- see backend/app/version.py,
# which only ever reads that file, and update_check.py, which compares it
# against the public GitHub repo. Default "unknown" if not passed, which
# both of those modules treat honestly as "no installed version known",
# never as a fabricated value.
ARG BUILD_COMMIT=unknown
ARG BUILD_VERSION=unknown
ARG BUILD_DATE=unknown

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

# Write the build args baked above into a plain file version.py reads at
# request time -- see that module's docstring. Never a secret; just a
# commit hash / version tag / build timestamp, all safe to include in a
# public image.
RUN printf 'BUILD_COMMIT=%s\nBUILD_VERSION=%s\nBUILD_DATE=%s\n' \
    "$BUILD_COMMIT" "$BUILD_VERSION" "$BUILD_DATE" > /srv/VERSION

EXPOSE 8000

WORKDIR /srv/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
