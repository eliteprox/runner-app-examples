# eliteprd1 deployment

Target: `elite@hky.eliteencoder.net:7216`, using the operator's SSH identity. The existing AI orchestrator is `https://ai1.eliteencoder.net:8936`; it runs go-livepeer v0.9.3 with Live Runners enabled. This deployment adds a separate Compose project and requires no orchestrator restart.

## Build and configure

Build wheels from the two checked-out repositories. Use a temporary build context containing:

```text
build/
  wheels/livepeer_gateway-1.0.0-py3-none-any.whl
  wheels/livepeer_swarm_publish-0.1.0-py3-none-any.whl
  runner.py
  deploy/Dockerfile
  deploy/wait_for_bee.py
```

From each Python project use `uv build --wheel --out-dir /path/to/build/wheels`. Copy `runner.py` and the deployment scripts into the context, then:

```sh
docker build -t livepeer-swarm-publisher:local -f build/deploy/Dockerfile build
```

Copy `compose.eliteprd1.yml` to the deployment directory as `compose.yml`. Create a private `.env`:

```dotenv
GNOSIS_RPC_URL=https://rpc.gnosischain.com
PRICE_USD_PER_HOUR=0.10
SWARM_PUBLISHER_IMAGE=livepeer-swarm-publisher:local
```

Create `secrets/orch-secret` from the existing AI orchestrator's registration secret and `secrets/bee-password` with a newly generated Bee operator password. These are **not** publisher or payer credentials. Keep the secrets directory mode 0700. The Bee container user must be able to read its mounted password file; the deployed example uses a read-only file inside the private directory.

```sh
docker compose up -d
docker compose logs --tail 30 bee publisher
```

Bee 2.8.3 runs in light mode with persistent volume `livepeer-swarm_bee-data`. Fund the operator address shown by Bee on Gnosis before it can deploy its chequebook and synchronize. Preserve the volume and password across deployments. Do not use `down -v` to update this deployment.

The upload API is bound only to `127.0.0.1:11633`. The publisher binds only to `127.0.0.1:8992`; clients reach it through the orchestrator. Its entrypoint waits for Bee `/readiness` before registering a paid capability. An unhealthy publisher container while Bee awaits funding is expected: it is deliberately not advertising capacity yet.

## Verify

```sh
curl -f http://127.0.0.1:11633/readiness
curl -f http://127.0.0.1:8992/healthz
curl -f https://ai1.eliteencoder.net:8936/discovery
```

After Bee is ready, discovery must list `livepeer-example/swarm-publish-demo`, persistent mode, capacity one, and the configured metered price. The runner has no GPU requirement. The client must use an independent local payment signer; neither publisher signing keys nor BZZ are needed by the gateway's payment identity.

The local client's runbook is [livepeer-swarm-radio operations](https://github.com/eliteprox/livepeer-swarm-radio/blob/main/docs/operations.md). Before the 30-minute paid acceptance test, provide a valid publisher-owned postage batch with existing counters, independent read gateway, publisher keystore/password, and a funded Livepeer payer. Test external-radio playback as well as `/stream.aac`. A successful local stub test does not establish public Swarm delivery.

## Current installation

Deployment directory: `/home/elite/livepeer-swarm` on eliteprd1. The newly generated Bee operator address is `0x2d0991c3d789079eEA883F036D854214CD7eE20F`. The operator was funded with 1 xBZZ and 0.01 xDAI, and its chequebook is `0x6FA1f35D08b0f7a5d254888c42d748d23d931D58`. A fresh publisher-owned depth-20, bucket-depth-16 mutable batch was created: `f6b9f693fe530ef03da5f85f9947a736cec3ecb28ee35a69c328c0ab6fbe23e3`. Total setup allocation was 1.3937175649386496 xBZZ plus 0.01 xDAI and gas. The batch is temporary (approximately 26 hours at creation), not a permanent entitlement; check its remaining TTL before subsequent tests. Use live logs and readiness to check synchronization. This address is different from publisher `0x8e967011A1895516B7720903FFF0BF080219946A`.

Stop this example with `docker compose down` from its deployment directory. This preserves Bee data and leaves the existing orchestrator and other runners running. Rebuild the image and use `docker compose up -d` to update it.

## Hosted billing and operator responsibilities

The advertised `PRICE_USD_PER_HOUR` is the **total service price including upload
bandwidth**. The current $0.10/hour is an introductory operator-set price, not a
measured cost or profitability guarantee. There is no separate Bee-wallet payment
step for publishers. Publishers supply their own valid postage batch and sign
postage/feed updates locally; their independently configured Livepeer payer pays
the advertised rate. Playback gateway costs are not included in this upload service.

The orchestrator operator owns, funds, and monitors Bee's node wallet and
chequebook. Our original experiment seeded this operator wallet from the test
publisher wallet; production users must not be asked to repeat that setup.
No automatic transfers, token purchases, or publisher-key imports are performed.

`BEE_MIN_AVAILABLE_BZZ` defaults to `0.01` xBZZ. The entrypoint waits for this
available chequebook balance before registration, and each new broadcast checks
it again before allocating channels. Unavailable accounting or insufficient
funds returns 503 with an operator-funding error. Existing broadcasts are not
terminated by this admission threshold. It is a reserve threshold, not a guarantee
that a session of arbitrary length is funded; operators must monitor and top up.

Inspect the private runner endpoint:

```sh
curl -f http://127.0.0.1:8992/operator/bandwidth
```

Keep this administrative endpoint on loopback, alongside the private Bee API.
It reports available funds (including outstanding-cheque obligations), sent and
received settlement totals in xBZZ, and the operator-paid billing policy. Session
start/release logs include accounting snapshots. For this dedicated, single-session
Bee, compare `settlements_sent_bzz` at start/end and divide by active session hours
for an observed settlement rate. Cheques settle in batches: short intervals can
understate accrued costs. Deposits are not expenses, and elapsed idle time must not
be treated as paid streaming time. Retain logs and compare longer runs before
setting a production price. Do not reset Bee data to reset these counters.

Price the service from compute/hosting plus measured xBZZ/hour converted at the
operator's chosen exchange rate, plus margin. Set the resulting **total** in
`PRICE_USD_PER_HOUR`; do not add an undisclosed client bandwidth surcharge.
