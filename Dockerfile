FROM python:3.11-slim

WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY fixtures fixtures

ENV PYTHONPATH=/srv/backend \
    TIP_MODE=demo \
    TIP_FIXTURES_DIR=/srv/fixtures \
    TIP_DB_PATH=/srv/data/tip_hackathon.db

RUN mkdir -p /srv/data

EXPOSE 8000

WORKDIR /srv/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
