# Pinned to an exact patch + distro release (not the floating `3.12-slim`
# tag) so rebuilds are reproducible; bump deliberately when patching.
FROM python:3.12.13-slim-trixie AS runtime

# The container binds 0.0.0.0 so the port can be published; keep the host
# side restricted (see docker-compose.yml, which maps to 127.0.0.1).
# The API ships unauthenticated with permissive CORS by default — for any
# non-local deployment set:
#   SEMANTIC_RAILS_API_KEYS / SEMANTIC_RAILS_API_KEY_FILE  (require API keys)
#   SEMANTIC_RAILS_CORS_ORIGINS                            (CORS allow-list)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEMANTIC_RAILS_PACKAGE=jaffle_shop \
    SEMANTIC_RAILS_HOST=0.0.0.0 \
    SEMANTIC_RAILS_PORT=8080

WORKDIR /app

RUN useradd --create-home --uid 10001 semantic

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY semantic_rails ./semantic_rails
COPY mf2sr ./mf2sr
COPY configs ./configs
COPY data ./data

RUN pip install --no-cache-dir .[server] \
    && python -c "from semantic_rails.runtime import Runtime; runtime = Runtime('jaffle_shop'); runtime._ensure_db(); runtime.close()" \
    && python -c "from semantic_rails.manifest import load_manifest,write_manifest; from semantic_rails.runtime import Runtime; runtime=Runtime('jaffle_shop'); write_manifest(runtime); source=runtime.source_path; runtime.close(); manifest=load_manifest(source); assert manifest and 'summary|summary' in manifest['catalogs'], 'verified catalog manifest missing'" \
    && chown -R semantic:semantic /app

USER semantic
EXPOSE 8080

# Keep this endpoint in sync with the docker-compose.yml healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/ready' % os.environ.get('SEMANTIC_RAILS_PORT','8080'), timeout=3).read()"

CMD ["sh", "-c", "uvicorn semantic_rails.asgi:app --host ${SEMANTIC_RAILS_HOST} --port ${SEMANTIC_RAILS_PORT}"]
