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

## Live audio and radio

The existing `/ingest` WebSocket file demo is preserved. The new `/start` endpoint opens a persistent live publishing session with four Trickle channels. The local gateway client signs postage and feeds under its publisher identity; a separate remote signer pays Livepeer. The runner is keyless and uses only externally signed stamps and SOCs.

| Channel | Direction | Wire format |
|---|---|---|
| `in` | client → runner | One upload per Trickle segment: 32 ASCII hex characters of upload ID, then initialization/audio bytes |
| `events` | runner → client | Incremental JSONL: ready, stamp-request, uploaded, feed, error, stopped |
| `stamps` | client → runner | Incremental JSONL: session, request_id, ordered stamps (or error) |
| `control` | client → runner | Incremental JSONL: ready, meta, soc, stop |

`POST /start` accepts `{ "owner": "0x…", "batch_id": "…" }` and returns protocol version 1, session ID, and public URLs named `in`, `events`, `stamps`, `control`. The orchestrator supplies the session headers. Runner consumers use internal channel URLs. All JSON messages include the session ID. Each `meta` carries upload_id, kind (`init` or `media`), and duration. Signed `soc` commands include request_id, topic/index, owner/id/signature, base64 CAC body, postage stamp and address. A feed acknowledgment must match that address before the client advances its index.

Stamp requests contain request_id, upload_id, and the ordered chunk-address list. The client recomputes that list from its submitted bytes. No generic signing endpoint is exposed. Uploads are capped at 256 KiB and requests/timeouts/queues are bounded. JSONL is read incrementally; the ten-second channel rotation does not delay signatures. Bee writes use `Swarm-Deferred-Upload: false`, and successful uploads return references and timing metrics.

- Gateway client: [livepeer-swarm-radio](https://github.com/eliteprox/livepeer-swarm-radio).
- [Standalone AAC player](https://github.com/eliteprox/livepeer-swarm-radio).
- [eliteprd1 deployment](deploy/README.md).

The live path requires `BEE_URL`; it never silently disables publishing. Feed signing and playlist construction remain on the client. One active broadcast is supported. The development stub validates layouts but does not validate on-chain batch ownership/expiry or simulate network propagation.
