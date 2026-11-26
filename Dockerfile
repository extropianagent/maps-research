FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY . /app

RUN uv sync --extra mcp --no-dev --frozen

ENV MAPS_STATE_DIR=/data \
    PORT=8080 \
    PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /data
EXPOSE 8080

# MAPS_MCP_TOKEN must be provided at run time; the server refuses to start without it.
CMD ["sh", "-c", "exec python -m maps.mcp_server --http --host 0.0.0.0 --port ${PORT:-8080}"]
