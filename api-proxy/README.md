# fal queue API proxy (73 pinned Live Runner route contracts)

This example shows how an orchestrator operator can offer selected hosted fal
queue endpoints as separately registered Live Runner applications. The checked-in
worked deployment offers only Flux Schnell. The repository also contains 72 more
route contracts in exactly the same implementation format so an operator can
select and configure additional applications deliberately.

The 73 entries are a curated, closed inventory for this example. They are not
every fal endpoint, a live fal catalog, a dynamic importer, or evidence that all
73 applications are currently deployed.

|              |                                                              |
| ------------ | ------------------------------------------------------------ |
| Worked app   | `livepeer-example/fal-flux-schnell`                          |
| Runner mode  | single-shot                                                  |
| Registration | static (orchestrator config + health poll)                   |
| Transport    | blocking adapter over fal queue submit/status/result         |
| Pricing      | fixed per application; checked-in value is local-demo policy |
| Adapter port | 8989                                                         |

Prerequisites (Docker, `uv`, and the
[`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the
shared on-chain/payment setup live in the [repository README](../README.md).
This example additionally needs a fal API key supplied by the operator as
`FAL_KEY`.

## How it is wired

[`routes/index.json`](routes/index.json) is build-time source data for the 73
fixed route identities. Each route generates a self-contained contract pack in
`contracts/<capability>/`. One adapter image serves every pack, but each adapter
process starts with exactly one `--endpoint-key`, one deadline, and one request
size limit.

**A capability is a registration, not a caller parameter.** A caller can choose
among the separately registered Live Runner app IDs that an operator publishes.
After the caller selects an app, that app's adapter process is pinned to one
provider endpoint; the request cannot override the endpoint, supply another
provider URL, or reuse the operator credential for another model.

The orchestrator reads the generated `runners.json` through
`-liveRunnerConfig`. Each selected entry supplies the app identity, price,
capacity, availability, and route to its dedicated adapter service. The adapter
owns the upstream credential and exposes only:

- `GET /health`: local credential, route, and schema readiness;
- `GET /schema`: deterministic schema bytes for the pinned route;
- `POST /proxy`: blocking execution through the pinned fal queue.

`POST /proxy` drops caller authorization and query parameters, adds the
operator-held `Authorization: Key <FAL_KEY>`, submits once, polls the original
request ID, and returns the final provider output in a receipt envelope. The
adapter does not validate inputs, apply defaults, rewrite prompts, choose
models, retry submissions, or expose arbitrary provider routing.

## Run offchain (free Livepeer path)

Run these commands from this `api-proxy` directory:

```sh
cp .env.example .env.local
# Set FAL_KEY in .env.local.
docker compose --env-file .env.local up -d --build
```

Confirm the checked-in Flux Schnell registration:

```sh
curl -sk https://localhost:8935/discovery |
  jq '.[].runners[] | select(.app == "livepeer-example/fal-flux-schnell") |
      {app, mode, metadata, capacity_available, price_info}'
```

Save a complete provider-native input object as `request.json`, for example:

```json
{
  "prompt": "A lighthouse in a winter storm, editorial photograph",
  "image_size": "landscape_4_3",
  "num_inference_steps": 4,
  "num_images": 1,
  "output_format": "jpeg",
  "seed": 12345
}
```

Call the application through Livepeer discovery:

```sh
uv run python client.py run flux-schnell \
  --input-json request.json \
  --discovery https://localhost:8935/discovery \
  --insecure \
  --receipt-output receipt.json
```

The client selects the exact app ID from `route.json`, reads compact route
metadata from discovery, fetches `/schema`, verifies its SHA-256, and then calls
`/proxy`. A successful response has this shape; the object under `output` keeps
the provider's result fields without renaming or selection:

```json
{
  "request_id": "provider-request-id",
  "endpoint_id": "fal-ai/flux/schnell",
  "schema_sha256": "64-lowercase-hex-characters",
  "output": {
    "images": [
      {
        "url": "https://provider-result.example/image.jpeg",
        "width": 1024,
        "height": 768,
        "content_type": "image/jpeg"
      }
    ],
    "timings": {},
    "seed": 12345,
    "has_nsfw_concepts": [false],
    "prompt": "A lighthouse in a winter storm, editorial photograph"
  }
}
```

Stop the example with:

```sh
docker compose --env-file .env.local down
```

## Run onchain (paid Livepeer path)

Fill the on-chain variables in the same ignored `.env.local`, then layer the
payment overlay onto the offchain Compose file:

```sh
docker compose --env-file .env.local \
  -f compose.yml -f compose.onchain.yml up -d --build
uv run python client.py run flux-schnell \
  --input-json request.json \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 \
  --insecure \
  --receipt-output receipt.json
docker compose --env-file .env.local \
  -f compose.yml -f compose.onchain.yml down
```

Each request is one paid single-shot session at the fixed price in the selected
Runner registration. The checked-in `0.0001 USD` price and capacity `4` are
local demonstration policy, not production economics or provider facts.

## The 73 modeled route contracts

Every mapping in [`routes/index.json`](routes/index.json) has a generated
directory under `contracts/<capability>/`:

- `openapi.json`: committed official queue OpenAPI snapshot;
- `schema.json`: request/result schemas and transitive OpenAPI components;
- `route.json`: Live Runner app identity, fixed queue identity, credential
  ownership, request policy, Runner paths, and schema hash.

All 73 `route.json` files use the same format as Flux Schnell. They contain no
capacity, price, publication status, or deployment-readiness label. Those are
operator deployment decisions, not properties of an API contract.

The snapshots were retrieved on 2026-09-01 using:

```text
https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<endpoint_id>
```

`contracts/report.json` verifies only committed endpoint identity, queue
operations, schema projection, and generated bytes. It does not prove current
provider behavior or orchestrator availability.

## Select additional applications for an operator deployment

A deployment file chooses which modeled routes to offer and supplies
operator-owned policy for each instance:

```json
{
  "capability": "flux-schnell",
  "service": "fal-flux-schnell",
  "label": "fal-flux-schnell",
  "deadline_seconds": 180,
  "max_request_bytes": 1048576,
  "capacity": 4,
  "price_info": {
    "currency": "usd",
    "price": 0.0001,
    "unit": "fixed"
  }
}
```

To select more of the modeled routes, add entries to a deployment's `routes`
array and regenerate it:

```sh
uv run python generate_deployment.py \
  deployments/local-offchain-flux/deployment.json
```

The generator writes:

- `generated/compose.json`: one pinned adapter service per selected route;
- `generated/runners.json`: one static Live Runner registration per service;
- `generated/deployment.json`: resolved route identity and operator policy.

The checked-in deployment intentionally selects only Flux Schnell. The adapter
never registers the entire 73-route inventory on its own.

To work through another route:

1. Inspect `contracts/<capability>/route.json` and `schema.json`.
2. Add the capability to an operator deployment's `routes` array.
3. Choose that instance's deadline, request limit, capacity, price, service,
   and label.
4. Regenerate and inspect the generated Compose and `runners.json` files.
5. Exercise it with a schema-valid full JSON object through the same discovery
   and Runner path used by Flux Schnell.

File uploads and provider-accessible URL preparation remain caller
responsibilities. Long-running routes need operator and client deadlines
appropriate to that model.

## Input and error behavior

Livepeer clients and the gateway may parse and reserialize caller JSON before
it reaches the adapter. The adapter forwards the bytes it receives unchanged
with `Content-Type: application/json`, but the public contract guarantees
provider-native JSON fields without semantic transformation, not byte identity
with the caller's source file.

The adapter requires the provider `request_id`, derives status and result URLs
from the pinned route, and polls until completion. Errors retain endpoint and
schema identity and use `error.stage=submit|status|result|timeout`. A timeout
does not resubmit or cancel the upstream job. Logs contain only request ID,
stage, status, and elapsed time.

## Credential ownership

`FAL_KEY` belongs to the orchestrator operator. Keep it in the ignored
`.env.local`; it is never accepted from callers, written to route contracts, or
placed in discovery metadata.

```sh
cp .env.example .env.local
# Set FAL_KEY in .env.local.
```

## Refresh and verify

Normal verification is offline and runs through the declared project
environment. From this `api-proxy` directory:

```sh
uv run --frozen python generate_contracts.py --check
uv run --frozen python generate_schema.py --check
uv run --frozen python generate_deployment.py \
  deployments/local-offchain-flux/deployment.json --check
uv run --frozen python -m unittest discover -s tests -v
docker compose --env-file .env.local config --quiet
docker compose --env-file .env.example \
  -f compose.yml -f compose.onchain.yml config --quiet
docker build .
```

Regenerate committed derived artifacts only after changing their source inputs:

```sh
uv run python generate_contracts.py --generate
```

Refresh official OpenAPI only as an intentional network operation. Refresh one
route while developing; omit `--route` to refresh all 73:

```sh
uv run python generate_contracts.py --refresh --route flux-schnell
```

## Scope

This repository demonstrates how this curated set of 73 fal queue endpoints
maps to pinned Live Runner applications. It does not implement a public
capability catalog, dynamic endpoint import, categorization, ranking, model
selection, prompt rewriting, fallback, webhook handling, durable recovery after
adapter restart, production pricing, or arbitrary provider routing.
