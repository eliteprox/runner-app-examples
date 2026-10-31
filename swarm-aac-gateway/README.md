# Hosted Swarm AAC gateway

**Network capability: `livepeer-example/swarm-aac-gateway`.** Any Live Runner–enabled
orchestrator can register this CPU-only service and set a USD/hour price for
Swarm retrieval and AAC delivery. This is separate from
`livepeer-example/swarm-publish-demo`, which uploads content.

```text
Publisher → orchestrator A / publisher Bee → Swarm
                                              ↓
VLC ← local HTTP bridge ← paid Trickle ← orchestrator B / gateway Bee
```

The radio engine remains in the pinned [livepeer-swarm-radio application](https://github.com/eliteprox/livepeer-swarm-radio).
This directory supplies its hosting adapter and a minimal SDK client. No SDK source
changes, publisher wallet, postage signatures, or BZZ wallet are needed by listeners.
Only their independently configured Livepeer payment signer is required for paid use.

## Supported deployment

Linux Docker with host networking; one active listener session per runner. The
orchestrator must enable Live Runners and support persistent session/Trickle routing
(go-livepeer v0.9.3 was tested). A shared-host orchestrator can reach the default
loopback runner. If the orchestrator uses a Docker bridge, set `RUNNER_HOST` and
`RUNNER_URL` to that bridge's host gateway address. For separate hosts, bind a
private interface reachable by the orchestrator and restrict access to that
orchestrator. Session headers are trusted proxy headers, not standalone authentication:
**do not expose the runner directly to the internet**.

From this directory:

```sh
cp .env.example .env
# Set this operator's ORCHESTRATOR_URL, RUNNER_URL, ORCH_SECRET_PATH,
# BEE_READ_URL, and PRICE_USD_PER_HOUR.
docker compose up -d --build
docker compose logs --tail 30 gateway
```

`ORCH_SECRET_PATH` is the local path to a private file containing this orchestrator's
registration secret. No secret belongs in `.env` or Git. The capability name is fixed
across operators so clients discover the same service on different orchestrators.
`RUNNER_PORT` defaults to 8993. Each operator chooses the **total** service price,
including compute, Swarm retrieval bandwidth and delivery overhead; the illustrative
$0.10/hour is not a profitability claim. This charge is additional to any independent
publisher session. Wall-clock billing continues while the playback session is held,
including temporary station stalls.

### Operator-funded Bee

Use an operator-controlled Bee read endpoint. For a dedicated persistent light node:

```sh
# Add BEE_PASSWORD_PATH=/private/path/bee-password to .env; create the file first.
# Set BEE_READ_URL=http://127.0.0.1:11634 and BEE_MIN_AVAILABLE_BZZ=0.01.
docker compose -f compose.yml -f compose.bee.yml up -d bee
curl http://127.0.0.1:11634/addresses
# Operator funds the displayed node address with xDAI for deployment gas and xBZZ.
# Once the chequebook is deployed, deposit operator xBZZ via Bee's chequebook API.
docker compose -f compose.yml -f compose.bee.yml up -d --build gateway
```

The optional Bee API binds only to loopback. Preserve its volume and password;
`docker compose down` without `-v` preserves the node identity. No postage batch
purchase is needed for read-only playback. Startup and new sessions check available
chequebook funds when `BEE_MIN_AVAILABLE_BZZ` is configured. Insufficient funds or
failed accounting returns an operator error; users are not asked to fund Bee.
Inspect `/chequebook/balance` and `/settlements` privately to monitor retrieval
costs. Only the operator performs top-ups; there are no automated transfers.

An existing external read gateway can also be configured. Omit
`BEE_MIN_AVAILABLE_BZZ` if it does not expose private accounting endpoints; the
operator must arrange and monitor that upstream service separately. A public gateway's
free access is not a guarantee of unlimited commercial capacity.

## Listener / VLC client

Install Python 3.12+, Git, and the pinned radio application in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python client.py \
  --orchestrator https://your-orchestrator.example:8936 \
  --owner 0xPUBLISHER_ADDRESS --station your-station \
  --signer https://your-payment-signer.example \
  --signer-headers-file /private/path/signer-headers.json \
  --max-hourly-usd 0.11 --max-wei-per-second 14000000000 \
  --seconds 600 --listen-host 127.0.0.1 --listen-port 8788
```

Open **`http://127.0.0.1:8788/stream.aac`** in VLC's *Media → Open Network Stream*.
For VLC on another LAN machine, bind `--listen-host` to the bridge machine's LAN
address and open that address from VLC. The local URL uses the bridge owner's payer,
so keep it on a trusted LAN; do not publicly port-forward it. One listener is supported
per bridge; a second connection receives 409. The HTTP response is `audio/aac`.

The bridge opens a paid session only when VLC connects, releases it on disconnect,
and shuts down after `--seconds` (default 300, maximum 86400). Reconnects cannot extend
that bridge lifetime. Without `--listen-port`, the client writes ADTS AAC to stdout
for `ffplay -f aac -` or a file. Repeat `--orchestrator` to supply alternative
orchestrators for discovery/reservation. An interrupted stream requires a new session;
there is no seamless mid-stream failover. The signer handles Livepeer payments;
its authentication headers remain local and are never sent to Bee.

`--offchain` explicitly enables free development sessions without a signer.
Paid mode requires a signer and a price limit. Set the wei/second limit for
orchestrators advertising converted on-chain prices. Price limits should be chosen
for the current exchange rate rather than copied blindly.

## Protocol

1. Discover `livepeer-example/swarm-aac-gateway`; use SDK `reserve_session()`.
2. POST to the reserved `session.app_url + '/start'` with exactly:
   `{"owner":"0x...","station":"your-station"}`.
3. Response: `{"session":"...","protocol":1,"out":"https://...", "mime_type":"audio/aac","codec":"aac-lc","container":"adts"}`.
4. Read `out` with `TrickleSubscriber(start_seq=0)`. Concatenate sequence bodies;
   each contains complete ADTS AAC frames remuxed from fMP4, without re-encoding.
5. GET `session.app_url + '/status'` for playback sequence, gaps and terminal error.
6. Exit the SDK session context on EOF, failure, disconnect or timeout. This stops
   its payment loop and releases runner resources. EOF alone does not release a
   custom client's reservation; the supplied client does so automatically.

`/start` is idempotent for the same session/owner/station. Different sessions or
station changes conflict until release. Only the operator configures the Bee URL;
client URL overrides and extra fields are rejected. SOC ownership and chunk
references are verified by the shared engine. Playback begins near the live edge,
keeps a three-segment queue, drops slow consumers, and terminates after 30 seconds
without audio. Channels close on EOF/error/release. One station pipeline exists
per active session, with no unbounded retrieval cache or cross-session listener state.

The supported format is the Donkey-push station/master/rendition feed convention,
AAC-LC in fragmented MP4. It is not a general HTTP proxy or arbitrary HLS transcoder.

## Validation

```sh
.venv/bin/pip install pytest pytest-asyncio
PYTHONPATH=.:../packages/swarm-publish .venv/bin/pytest -q tests
```

The full integration test additionally requires FFmpeg and an offchain orchestrator:

```sh
docker run -d --name swarm-gateway-test --network host livepeer/go-livepeer:v0.9.3 \
  -orchestrator -useLiveRunners -network=offchain \
  -httpAddr=127.0.0.1:18935 -serviceAddr=https://localhost:18935 \
  -liveRunnerAddr=https://localhost:18935 -cliAddr=127.0.0.1:17935 \
  -orchSecret=swarm-test-only -monitor=false
# Wait for startup, then copy its development TLS certificate:
docker cp swarm-gateway-test:/root/.lpData/offchain/cert.pem /tmp/swarm-gateway-cert.pem
SSL_CERT_FILE=/tmp/swarm-gateway-cert.pem \
SWARM_TEST_ORCHESTRATOR=https://localhost:18935 \
PYTHONPATH=.:../packages/swarm-publish .venv/bin/pytest -q tests
docker rm -f swarm-gateway-test
```

The test loads deterministic, signed station feeds and audio chunks into a Bee stub,
reserves the actual capability through go-livepeer, reads its Trickle output, and
decodes the resulting AAC. It also checks malformed inputs, client URL rejection,
operator-funding failures, idempotency, capacity isolation, and release/reuse.
This proves the protocol without spending funds; public Swarm/paid deployment
validation is a separate check.
