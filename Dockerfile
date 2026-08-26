FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY wa_mcp ./wa_mcp
RUN pip install --no-cache-dir .

ENV WA_DATA_DIR=/data
VOLUME ["/data"]

ENV WA_HOST=0.0.0.0 WA_PORT=8100
EXPOSE 8100

CMD ["python", "-m", "wa_mcp"]
