# livepeer-swarm-publish

Optional Swarm upload for a cap that already has its own result path. The client chunks a payload and signs a postage stamp for every chunk address before anything is sent. The runner posts those chunks to Bee. The bytes are not returned through the orchestrator. The cap's own result — a transcript, a tile, a JSON object — is the only thing that comes back.

Caps that do not install this package are unchanged.

```python
publisher = SwarmPublisher(os.environ.get("BEE_URL"))

async def on_inbound(frame: bytes) -> dict:
    payload = unwrap(frame) if publisher.enabled else frame
    result = await pipeline(payload)  # whatever this cap returns
    await publisher.put(frame)  # no-op when BEE_URL is unset
    return result  # the only bytes that go back through the orchestrator
```

`put` logs Bee's HTTP status for the operator. It does not open a refs channel, emit a segment event, or echo the payload.

## What the client signs

`chunk_and_stamp(payload, signer, batch_id)` splits the payload into 4096-byte leaves and parent chunks. The hash matches `pub.swarmsign.Cac`: keccak256 of the 8-byte little-endian span plus a binary Merkle root over 32-byte segments. The stamp is 113 bytes, matching `StampIssuer.encode`: batch id (32), bucket and slot (8), timestamp in nanoseconds (8), and an EIP-191 `personal_sign` (65). Leaves are first, in file order. The last item is the root, and its address is the Swarm reference of the file.

`pack` frames one chunk as `stamp || chunk`, where `chunk` is the body Bee stores (`span || payload`). `unwrap` returns the file bytes from a data chunk and empty bytes from a parent, so concatenating `unwrap` of every frame rebuilds the input.

A real Bee accepts `POST /chunks` with header `Swarm-Postage-Stamp` only when that batch exists and the signature recovers to its owner. This package never sends `Swarm-Postage-Batch-Id` and never sees the batch key. `python -m livepeer_swarm.stub` is an offchain stand-in: it checks the stamp is 113 bytes, stores the body, and returns 201.

## Opting in from realtime-transcription

[realtime-transcription](../../realtime-transcription/runner.py) is the pattern this package follows, and it does not depend on this package. Audio goes up the WebSocket. `_handle_transcribe` appends each binary frame to `seg` and sends only `_message(...)` back with `ws.send_json`. To publish that audio as well, install this package in that cap and change the binary branch. Leave the `ws.send_json(_message(...))` calls as they are.

```python
from livepeer_swarm import SwarmPublisher, unwrap

publisher = SwarmPublisher(os.environ.get("BEE_URL"))

# inside _handle_transcribe, BINARY branch:
frame = msg.data
payload = unwrap(frame) if publisher.enabled else frame
seg.extend(payload)
if _rms(payload) >= SILENCE_RMS:
    spoke = True
await publisher.put(frame)
```

The client of a cap that opts in calls `chunk_and_stamp` and `pack` before it sends. A client that does not opt in keeps sending raw PCM, and the runner leaves `BEE_URL` unset so `put` is a no-op and the frame is still the original bytes. Turn the publisher on only for clients that send packed frames.
