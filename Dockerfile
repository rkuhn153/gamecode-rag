FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gamecode_rag_server.py .
COPY ingest_code_graph.py .

# Mount your indexes at runtime, e.g.:
#   -v /path/to/PROJECT_DATABASES:/app/PROJECT_DATABASES
#   -e OPENROUTER_API_KEY=...

RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

CMD ["python", "gamecode_rag_server.py"]
