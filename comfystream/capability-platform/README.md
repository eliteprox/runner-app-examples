# Candidate capability images

These targets install ComfyStream from a Git commit into a uv environment and
provision the same embedded ComfyUI revision with comfy-cli. No Conda is used.
Set `COMFYSTREAM_REF` to the reviewed candidate commit and choose `PROFILE=media`
or `PROFILE=chat`; their complete dependency locks live in that package revision.
Qwen TTS and newer chat Transformers are isolated rather than force-installed.

The image defaults to the installed `comfystream-live-runner` command. The candidate
Compose binds loopback port 18991 and disables hosted fal routes; it must not replace
an existing deployment until that deployment's full route set is verified. Reuse
existing model caches read-only and give candidate state a separate mounted directory.

The current deployment selects FLUX, BiRefNet, Whisper, ACE-Step and TripoSR. Qwen
(including TTS) and Wan remain excluded. Music uses core ACE-Step nodes and mesh
generation uses the pinned TripoSR reference package. Keep the previous image ID
and Compose configuration before any production replacement.

Build the selected package revision with:

```sh
docker build --target media --build-arg PROFILE=media \
  --build-arg COMFYSTREAM_REF=f207a08a17a013cea8b5be8defb5daf0cc75a6de \
  -t comfystream-catalog:f207a08 .
```

`comfystream-catalog-runner --config /state/deployment.json` provides synchronous
network media applications in addition to the existing live runner. Run it on GPU 0
with host networking; it binds loopback port 8993 for the same-host orchestrator.
The existing live service can remain on GPU 1. Mount read-only model caches and an
independent state volume. Use the same capacity lock for every process assigned to
one physical GPU. The gateway dispatches through Builder Engine/orchestrators and
does not install this runtime or call private runner URLs.

The deployment file requires `image` (immutable digest), `capacity_group` and `models`.
Each selected capability maps to distinct `app` and `runner_id` values and a positive
fixed USD `price`; the approved price is `"0.02"` for each model. Required environment:
`LIVEPEER_ORCH_URL`, `LIVEPEER_ORCH_SECRET`, `COMFYUI_CWD`, `COMFYSTREAM_CAPACITY_FILE`.
Never commit the orchestrator secret. Startup validates workflows before registering;
shutdown unregisters models. Roll back by stopping only this catalog container.

`compose.catalog.yaml` captures this deployment separately from the live service.
`CATALOG_ENV_FILE` supplies orchestrator credentials and must be mode 0600. Create
the empty nested FLUX cache mountpoint inside `HF_CACHE` before mounting the parent
read-only. The native ACE symlink must resolve under `/model-cache` in the container.

`smoke_catalog.py` exercises the installed image without provider credentials or paid
network calls, including real OpenAI SDK image/transcription methods. It writes model
HTTP acceptance results and public registration declarations to the state volume.
The fixture volume contains `inputs/subject.png` and `inputs/reference.wav`. These
checks establish runner behavior; gateway/operator admission and paid accounting
remain separate acceptance steps. Not all nine models are deployed or certified.
