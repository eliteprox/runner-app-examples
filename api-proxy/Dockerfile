FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "aiohttp>=3.10,<4"

WORKDIR /app
COPY runner.py ./
COPY fal_adapter ./fal_adapter
COPY routes ./routes
COPY contracts ./contracts

EXPOSE 8989

ENTRYPOINT ["python", "runner.py", "--host=0.0.0.0"]
