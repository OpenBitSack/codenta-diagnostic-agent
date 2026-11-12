FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN useradd --create-home --uid 10001 app

# config/ and data/ are read relative to the project root, so the project is installed in place.
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data ./data
COPY scripts ./scripts
COPY tests ./tests
RUN pip install -e ".[postgres,llm,dev]"

USER app
EXPOSE 8000
CMD ["uvicorn", "codenta_diag.main:app", "--host", "0.0.0.0", "--port", "8000"]
