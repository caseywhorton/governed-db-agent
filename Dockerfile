# Governed Database Assistant: container image
#
# Build:  docker build -t governed-db-agent .
# Run:    docker run --rm governed-db-agent            (runs the guardrail tests)
# Shell:  docker run --rm -it governed-db-agent bash   (opens a shell inside the container)

# A small official Python image (Debian "slim" variant).
FROM python:3.13-slim

# Don't write .pyc files, show Python output immediately, and don't keep pip's download cache in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first. Docker caches this layer, so rebuilds are fast
# unless requirements.txt itself changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the project. .dockerignore keeps .env, the databases, the audit log, and
# anything private out of the image. No secret is ever baked in: pass keys at
# run time instead (docker run --env-file .env ...).
COPY . .

# Run as an ordinary user, not root, and give it ownership of /app so the
# tests and setup_db.py can write their working files.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Default command: the guardrail tests. Override it to run something else.
CMD ["python", "-m", "unittest", "test_guardrails", "-v"]
