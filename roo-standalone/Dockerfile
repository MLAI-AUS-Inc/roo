FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY roo/ ./roo/
COPY skills/ ./skills/
COPY ops/ ./ops/
COPY bridge/ ./bridge/

EXPOSE 8000

CMD ["uvicorn", "roo.main:app", "--host", "0.0.0.0", "--port", "8000"]
