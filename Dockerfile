FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
COPY scripts/generate_dependency_notices.py /tmp/generate_dependency_notices.py
RUN pip install --no-cache-dir -r requirements.txt && \
    python /tmp/generate_dependency_notices.py --output /app/third-party/python-notices.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
