FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false
WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt && useradd --create-home --uid 10001 app
COPY service.py download.py model_lock.json ./
USER app
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8090"]
