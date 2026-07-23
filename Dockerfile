FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 /uv /usr/local/bin/uv

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
