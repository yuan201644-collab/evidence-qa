# The frontend build receives source PDFs before Vite copies public/ into dist.
FROM node:24-bookworm-slim AS frontend-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
COPY web/scripts/ ./scripts/
RUN npm ci
COPY web/ ./
COPY data/corpus/raw/*.pdf ./public/documents/
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY app/ ./app/
COPY data/corpus/ ./data/corpus/
COPY infra/schema_meta.json infra/schema_meta.descriptions.zh.json infra/tasks_evidence.json ./infra/
COPY eval/runner.py eval/metrics.py eval/rag_metrics.py ./eval/
COPY eval/cases/*.jsonl ./eval/cases/
COPY eval/reports/mainline/current-candidate-20261007-summary.md eval/reports/mainline/t4-dev-v0.3-20261007-summary.md eval/reports/mainline/t5-dev-v0.2-20261007-summary.md ./eval/reports/mainline/
COPY eval/reports/mainline/t3-dev-v0.2-final-20261007-run1/report.md ./eval/reports/mainline/t3-dev-v0.2-final-20261007-run1/
COPY eval/reports/run-deepseek-robust-20261002T070612Z.md ./eval/reports/
COPY docs/要素识别与改写_接口_v0.1.md ./docs/
COPY docs/architecture/U19大结果集交付方案_v0.1.md ./docs/architecture/
COPY tests/test_mainline_clarifier_memory.py tests/test_mainline_linker_planner.py tests/test_mainline_semantic_validator.py ./tests/
COPY models/ ./models/
RUN test -f models/bge-m3/config.json \
    && test -f models/bge-reranker-v2-m3/config.json \
    || (echo "Both models/bge-m3 and models/bge-reranker-v2-m3 are required for offline RAG." >&2; exit 1)
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=frontend-build /build/web/dist ./web/dist/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
