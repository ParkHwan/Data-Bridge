FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md* ./
COPY src ./src
COPY scripts ./scripts
COPY samples ./samples
COPY evals ./evals

# Locked install: the exact dependency graph the CI gates tested (uv.lock), never a
# fresh resolution — gate and production image share one lock graph (review: Codex #1).
RUN uv sync --locked --no-dev --no-editable --extra server --extra gcp

ENV PATH="/app/.venv/bin:$PATH"
ENV PORT=8080
CMD ["sh", "-c", "uvicorn databridge.server.app:app --host 0.0.0.0 --port ${PORT}"]
