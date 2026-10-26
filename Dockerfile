FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home assr && mkdir /state && chown assr:assr /state
ENV PATH="/app/.venv/bin:$PATH" ASSR_ROOT=/state PYTHONUNBUFFERED=1
USER assr
EXPOSE 8000
CMD ["assr", "serve", "--host", "0.0.0.0"]
