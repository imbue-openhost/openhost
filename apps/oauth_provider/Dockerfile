FROM python:3.12-alpine

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY . .
RUN uv sync

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "oauth_provider.app:app", "--host", "0.0.0.0", "--port", "8080"]
