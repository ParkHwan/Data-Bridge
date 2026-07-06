FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md* ./
COPY src ./src
COPY scripts ./scripts
COPY samples ./samples
COPY evals ./evals

RUN uv pip install --system --no-cache ".[server,gcp]"

ENV PORT=8080
CMD ["sh", "-c", "uvicorn databridge.server.app:app --host 0.0.0.0 --port ${PORT}"]
