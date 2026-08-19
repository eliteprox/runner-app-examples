# Agent guide — Livepeer live-runner example apps

Example **apps** for the Livepeer network. An app is a container you build and put
on the network: an ordinary HTTP / WebSocket service. Orchestrators host it via
the **live runner**; clients call it through the orchestrator using the
[livepeer-gateway](https://github.com/livepeer/livepeer-python-gateway) Python SDK.

## Layout

Each example is a self-contained dir (`hello-world/`, `vllm/`, `ffmpeg/`, …) with:

- `runner.py` — the app (an aiohttp server). Dynamic apps self-register with the
  SDK's `register_runner`; static apps are off-the-shelf servers listed in a
  `runners.json`.
- `client.py` — discovers a runner and calls the app through the orchestrator
  (`runner_selector` → `call_runner` for single-shot; persistent apps also
  reserve/release a session).
- `Dockerfile`, `compose.yml` (offchain), `compose.onchain.yml` (paid overlay),
  `pyproject.toml`, `README.md`.

The orchestrator and signer services are defined **once** at the repo root
(`compose.orchestrator.yml`, `compose.onchain.yml`) and pulled into each example
with Docker Compose `extends` — don't duplicate them.

## Conventions

- **SDK pin:** `livepeer-gateway>=1.0.0` from PyPI (see each `pyproject.toml` /
  `Dockerfile`). Keep new examples on the same pin. Extra packages an example
  needs (e.g. `livepeer-gateway-client` for ffmpeg MCP signer auth) stay local to
  that example.
- **Registration:** _dynamic_ (app self-registers via the SDK; for apps with
  custom code) vs _static_ (operator lists an off-the-shelf server in a
  `runners.json`).
- **Mode:** set explicitly at registration. HTTP request/response apps use
  **single-shot** (one request → one result, billed once with `unit="fixed"`).
  Realtime / streaming apps use **persistent**.
- **Transport:** HTTP request/response for batch jobs; SSE for streaming tokens;
  trickle for realtime media. Pick the simplest that fits.

## Running an example (offchain, free)

```sh
cd <example>
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm it registered
uv run client.py ...                                                  # see the example README
docker compose down
```

## The `ffmpeg` example (tool → agent)

`ffmpeg/` is the **tool-runner** pattern: one app (`livepeer/ffmpeg`), many ops
(`transcode` / `clip` / `thumbnail`) chosen per request via vetted command
templates. Copy it for other CLI tools (imagemagick, yt-dlp, …).

- `ffmpeg/SKILL.md` — the agent-facing capability contract (how to call each op).
- `ffmpeg/mcp_server.py` + `ffmpeg/README-mcp.md` — a local MCP server exposing the
  ops to Claude as tools (offchain by default; Clearinghouse signer-session auth
  via env).

When changing an example, keep it self-contained (no cross-imports between
examples) so it stays runnable — and extractable — on its own.
