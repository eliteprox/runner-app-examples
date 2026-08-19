# ffmpeg tool app (tool → agent capability)

Expose a **tool** as a Livepeer capability an **agent** can call. `ffmpeg` is the
worked example: one app, `livepeer/ffmpeg`, with several **operations**
(`transcode`, `clip`, `thumbnail`, …). The client picks an op and sends a file;
the runner runs a vetted ffmpeg command and returns the result. Plain HTTP
request/response — no live media plane. The app **self-registers** (dynamic).

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer/ffmpeg`                    |
| Runner mode  | single-shot                          |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | HTTP (JSON request/response)         |
| Pricing      | fixed (one price per call)           |
| Port         | 5000                                 |

Prerequisites (Docker, `uv`, and the [`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup live in the [repo README](../README.md).

## The pattern (why one app, many ops)

This is the reusable **tool-runner** shape — copy it for `imagemagick`, `yt-dlp`,
`pandoc`, a code sandbox, etc.:

- **One app, op-as-param.** Register _at the granularity clients pay at_. Models are
  shopped individually → one app per model (see `vllm`). Tool _operations_ aren't —
  they share a binary, hardware, and pricing → **one app, op chosen per request**.
- **`GET /ops`** advertises the machine-readable op/param schema.
- **[`SKILL.md`](SKILL.md)** is the agent-facing contract — when and how to call each op.
- **Command templates are the security boundary.** Each op maps validated params to a
  fixed ffmpeg argv; the app never passes through arbitrary flags (and `encoder` is
  allowlisted). This is the real work — the live-runner proxying is free.

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via
`register_runner` ([runner.py](runner.py)) and exposes `GET /ops` plus `POST /run`,
reverse-proxied through the orchestrator. The client calls it with
`runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one
call. Because the runner is **single-shot**, there is no session to manage: the
orchestrator reserves one for the call and releases it when the response returns,
and on the paid path `call_runner` answers the 402 payment challenge inline. Grep
`# Livepeer:` in either file to see the exact calls.

The input file is base64'd into a JSON request, so it rides the **standard buffered
call path** (the same `call_runner` + payment flow as hello-world). The app runs
`ffmpeg` and returns the output bytes, base64'd, in the JSON response. Fine for short
clips; **production passes URLs + object storage** instead of inlining bytes.

Wire protocol on `POST /run`:

- request: `{"op": "clip", "input_b64": "...", "start": 3, "end": 8}`
- response: `{"output_b64": "...", "bytes": N, "media_type": "video/mp4"}`

Ops + params: see [`SKILL.md`](SKILL.md), or `GET /ops` for the JSON schema.

A local MCP server ([`mcp_server.py`](mcp_server.py)) exposes the same ops to Claude
as tools — see [README-mcp.md](README-mcp.md).

## Run offchain (free)

> [!TIP]
> Built locally by the compose file below, or run the published
> [`ghcr.io/livepeer/runner-example-ffmpeg`](https://github.com/livepeer/runner-app-examples/pkgs/container/runner-example-ffmpeg)
> with `docker compose up -d --pull always` — see [Images](../README.md#images).

```sh
docker compose up -d --build
# make a tiny test clip if you don't have one:
ffmpeg -f lavfi -i testsrc=duration=3:size=640x480:rate=24 -pix_fmt yuv420p clip.mp4

uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4
uv run client.py --op clip --start 1 --end 3   --input clip.mp4 --output cut.mp4
uv run client.py --op thumbnail --at 1.5        --input clip.mp4 --output thumb.jpg

docker compose down
```

`client.py` discovers the runner, sends the file through the orchestrator to the app,
and writes the result. Inspect with `ffprobe out.mp4` / open `thumb.jpg`.

Confirm the runner registered and list its ops:

```sh
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # livepeer/ffmpeg
```

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator on-chain,
so the app advertises a price and the SDK pays per call. This needs an Ethereum RPC,
a funded signer wallet (deposit + reserve), and an orchestrator wallet — see
[On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README. Copy
`.env.example` to `.env` and fill it in, then:

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4 \
  --discovery https://localhost:8935/discovery --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

The app registers with a price (`--price` from `.env`, in USD billed once per call —
**fixed pricing**, the natural fit for bounded request/response work) and the
orchestrator advertises it in `/discovery`.

## GPU

CPU (`libx264`) by default so it runs anywhere. To run on an **NVIDIA GPU**, an
operator just layers the GPU overlay — no code or image change:

```sh
docker compose -f compose.yml -f compose.gpu.yml up -d --build
```

That gives the container GPU access and sets `FFMPEG_DEFAULT_ENCODER=h264_nvenc`, so
transcodes use the GPU by default (callers can still pass `encoder` explicitly).
Requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host. The image's `ffmpeg` already includes `h264_nvenc`, so **no CUDA base
image is needed** for plain nvenc — only CUDA _filters_ (e.g. `scale_npp`) would
require one. Combine with payments by adding `-f compose.onchain.yml`.
