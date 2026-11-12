FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# One worker only: the IMAP and scheduler threads live inside it.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8149", "--timeout", "60", "app:app"]
