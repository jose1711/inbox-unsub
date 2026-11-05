FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/inbox.db \
    TZ=Europe/Berlin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates
RUN mkdir -p /app/data

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["python", "app.py"]
