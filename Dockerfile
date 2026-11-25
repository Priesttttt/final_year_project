FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DIMF_DATA_DIR=/app/data

COPY requirements.txt requirements-evm.txt pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY docs ./docs
COPY contracts ./contracts
COPY README.md AGENTS.md Makefile ./

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e . --no-deps

EXPOSE 8000
# Synthetic demo only.  No production hardening.
CMD ["python", "scripts/run_demo.py", "--host", "0.0.0.0", "--port", "8000"]
