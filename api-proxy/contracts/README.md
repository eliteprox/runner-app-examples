# Generated route contract snapshots

Each child directory models one fal endpoint in the format consumed by the
shared Live Runner adapter and deployment generator:

- `openapi.json` is the committed official queue OpenAPI snapshot;
- `schema.json` contains the exact POST input root, final result root and every
  transitively referenced component;
- `route.json` fixes the Live Runner app identity, submit/status/result routing,
  operator credential boundary, request policy and schema hash.

Every route model requires these endpoint-specific operations:

```text
POST /<endpoint>
GET  /<endpoint>/requests/{request_id}/status
GET  /<endpoint>/requests/{request_id}
PUT  /<endpoint>/requests/{request_id}/cancel
```

`report.json` records hashes, byte sizes and component counts for offline
integrity checking. Its `snapshot_verified` status refers only to the committed
contract artifacts. It does not mean a Runner is registered, available or
provider-tested.

Use `python api-proxy/generate_contracts.py --check` for offline verification.
Use `--refresh --route <capability>` only when intentionally updating from the
official source.
