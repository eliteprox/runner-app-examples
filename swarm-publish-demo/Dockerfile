# swarm-publish demo (result back, bytes stay on the runner).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

COPY packages/swarm-publish /tmp/swarm-publish
COPY swarm-publish-demo/runner.py swarm-publish-demo/client.py /app/

WORKDIR /app

RUN pip install --no-cache-dir \
    "/tmp/swarm-publish" \
    "livepeer-gateway>=1.0.0" \
    aiohttp \
    eth-account

EXPOSE 8989

ENTRYPOINT ["python", "runner.py"]
