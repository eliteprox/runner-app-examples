# Hardware comparison deployments

`eliteprd2-4090.json` deploys the five-model catalog on GPU 0 of **eliteprd2**, an
RTX 4090 host whose orchestrator is `https://ai.eliteencoder.net:8936`. GPU 1 is
unassigned. **eliteprd3** serves `https://ai-mdw.eliteencoder.net:8936` and is a
different host.

The catalog shares its image, checkpoints, app names, request schemas and fixed
USD 0.02/request prices with the Ada deployment on eliteprd1
(`https://ai1.eliteencoder.net:8936`). Each host has its own capacity group and
runner IDs. Each host admits one GPU execution at a time across the five models.
The immutable image is installed locally; its digest in the deployment file is
the runtime identity, not a promise that a registry hosts an image under that name.
The shared runtime identity pins the image configuration digest. On eliteprd2's
containerd image store, Docker's local image ID is the OCI manifest digest
`sha256:5f481a70f3022c4d649d0e0476f922053f81fa17e765041662f839a757632e9f`.
Verify that manifest's configuration digest matches the deployment's pinned digest
and that the installed root filesystem layer IDs match that configuration before
launching. The same archive can have different local IDs across Docker image stores.

ComfyStream source is pinned to
`f207a08a17a013cea8b5be8defb5daf0cc75a6de`. Successful requests retain the worker
and model caches. Switching capabilities or live capacity demand evicts the idle
resident; cancellation, worker failure and shutdown also release it. Ordinary
ComfyUI component offloading can still change VRAM usage within a pipeline.

Both orchestrator URLs are already in the production discovery service's static
directory. Verify the new runner IDs, image identity and capacity group through
Engine `/v1/runners?app=flux-klein` and repeat for the other four apps. The same
image/descriptor deployment identity allows the existing Core capability profiles
to match both hosts without duplicating model schemas.

Builder currently chooses a runner automatically. A paid job's attempt record
identifies the orchestrator that served it; the job request has no public
orchestrator-pinning field. Operator comparisons can use each host's loopback
catalog endpoint with identical inputs and record first/repeated requests
separately. These local measurements exclude network payments and selection.

To replace StreamDiffusion on eliteprd2, remove its exact model entry from the
orchestrator's mounted `aiModels.json`, disable restart for its two old containers,
stop them and restart the existing orchestrator. Preserve the old images,
removed model entry and restart policies for rollback. The orchestrator may remove
its old automatically managed containers during restart; restoring the saved warm
model entry lets it recreate them from the original image. Start the catalog only
after the pinned image and model cache have been verified. Keep orchestrator
credentials in a private runtime environment file, outside this repository.
