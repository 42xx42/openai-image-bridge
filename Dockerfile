FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir .

RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /app/data/generated && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["python", "-m", "openai_image_bridge"]
