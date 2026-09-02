# Modeled fal routes

[`index.json`](index.json) contains the 73 fixed endpoint identities modeled by
this example. It is build-time source data for generating contract packs and
operator-selected Runner configuration. It is not served as a catalog and is
not evidence that any endpoint is currently available on an orchestrator.

[`INVENTORY.md`](INVENTORY.md) is the generated human-readable view for
reviewing all 73 mappings and opening each route, schema, and OpenAPI artifact.
It is regenerated and drift-checked from `index.json`; it adds no runtime
catalog behavior.

Every entry fixes:

- the example capability key and Live Runner app ID;
- the exact fal submit endpoint and queue app used for status and result calls;
- the blocking queue transport profile;
- removal of caller authorization, pinned endpoint selection, and
  provider-native JSON without semantic transformation.

The corresponding generated pack lives in `contracts/<capability>/`. Adapter
limits, price, capacity and the set of routes an operator offers belong in a
deployment selection under `deployments/`; they are not properties of a route
model.

Generate every pack from the committed snapshots and verify them offline:

```sh
python api-proxy/generate_contracts.py --generate
python api-proxy/generate_contracts.py --check
```

Refresh one official OpenAPI snapshot only as an intentional networked action:

```sh
python api-proxy/generate_contracts.py --refresh --route flux-schnell
```
