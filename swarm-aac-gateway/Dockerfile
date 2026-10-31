FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY swarm-aac-gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY packages/swarm-publish /deps/swarm-publish
RUN pip install --no-cache-dir --no-deps /deps/swarm-publish
WORKDIR /app
COPY swarm-aac-gateway/service.py swarm-aac-gateway/runner.py swarm-aac-gateway/client.py ./
ENTRYPOINT ["python", "runner.py"]
