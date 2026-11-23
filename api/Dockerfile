FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
# Logs reach `docker logs` as they are written. The environment lives outside /app, so docker-compose.yml can
# mount the source over /app without hiding it.
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
# Dependencies first, so a source change reuses this layer. --locked fails the build when uv.lock is out of step
# with pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN uv sync --locked --no-dev
EXPOSE 8000
# Serves the API on a migrated database. docker-compose.yml migrates first and serves the mounted source with
# --reload instead.
CMD ["uvicorn", "igneous.main:app", "--host", "0.0.0.0", "--port", "8000"]
