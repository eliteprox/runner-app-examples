# Build context contains wheels from both checked-out repositories plus runner.py.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY wheels/ /wheels/
RUN pip install --no-cache-dir /wheels/*.whl
COPY runner.py /app/runner.py
COPY deploy/wait_for_bee.py /app/wait_for_bee.py
WORKDIR /app
ENTRYPOINT ["python", "wait_for_bee.py"]
