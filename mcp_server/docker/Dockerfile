FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e /app -e /app/mcp_server

WORKDIR /app/mcp_server

EXPOSE 8000

CMD ["context-graph-mcp", "--transport", "http", "--config", "/app/mcp_server/config/config.yaml"]
