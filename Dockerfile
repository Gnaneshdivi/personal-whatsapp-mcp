# neonize ships a CGO shared library built against glibc, so this is a
# glibc image. Alpine's musl will import-error at runtime, not at build.
FROM python:3.12-slim

# libmagic is not optional: neonize imports python-magic at module load, and
# without it the package fails to import with an error that reads like a
# missing Python dependency rather than a missing system library.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY wa_mcp ./wa_mcp
RUN pip install --no-cache-dir .

# The session and SQLite files live here. Mount it, or a restart means
# re-pairing — and history only ever syncs once, at pair time.
ENV WA_DATA_DIR=/data
VOLUME ["/data"]

# 127.0.0.1 is the default and would be unreachable from outside the container.
ENV WA_HOST=0.0.0.0 WA_PORT=8100
EXPOSE 8100

# No CMD arguments: everything is configured by environment, so `docker run
# --env-file .env` and a bare `python -m wa_mcp` behave identically.
CMD ["python", "-m", "wa_mcp"]
