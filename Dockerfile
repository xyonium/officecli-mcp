FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OFFICECLI_MCP_DATA_DIR=/data \
    OFFICECLI_MCP_WORK_DIR=/work \
    # officecli is a self-contained .NET app that fails fast without ICU
    # ("Couldn't find a valid ICU package"). The image has no libicu; run .NET
    # in globalization-invariant mode instead of bundling ICU. This is the
    # upstream-recommended, package-free path and keeps the image small. Only
    # affects locale-aware culture data — fine for office document manipulation.
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

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
