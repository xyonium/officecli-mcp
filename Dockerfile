FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OFFICECLI_MCP_DATA_DIR=/data \
    OFFICECLI_MCP_WORK_DIR=/work

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

VOLUME ["/data", "/work"]

EXPOSE 8765

# Binary is fetched at runtime (first start) per design.
ENTRYPOINT ["officecli-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
