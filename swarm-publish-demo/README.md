# Swarm publish demo

A persistent cap with no GPU. It accepts packed chunks on a WebSocket, returns one JSON result, and posts the chunks to Bee when `BEE_URL` is set. The file bytes are not written back to the socket.

The publish logic lives in [`packages/swarm-publish`](../packages/swarm-publish). That README shows how [`realtime-transcription`](../realtime-transcription/runner.py) would opt in. This demo does not modify that example.

|              |                                       |
| ------------ | ------------------------------------- |
| App id       | `livepeer-example/swarm-publish-demo` |
| Runner mode  | persistent                            |
| Registration | dynamic                               |
| Transport    | WebSocket (`/ingest`)                 |
| Port         | 8989                                  |

## Run offchain

```sh
docker compose up -d --build
uv run client.py --discovery https://localhost:8935/discovery sample.bin
docker compose down
```

The client prints one JSON object, for example `{"text": "received 12 bytes", "byte_count": 12}`. Compose points `BEE_URL` at the stub Bee in this file (`python -m livepeer_swarm.stub`). Set `BEE_URL` to a real node when the batch in the stamp exists and the signature recovers to its owner.

## Run on-chain

```sh
cp .env.example .env
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 sample.bin
docker compose -f compose.yml -f compose.onchain.yml down
```

The on-chain overlay adds Livepeer payment only. It does not start a Swarm chain.
