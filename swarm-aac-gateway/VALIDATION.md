# Deployment validation — 2026-10-31

The `livepeer-example/swarm-aac-gateway` capability is deployed on eliteprd4 behind
`https://hky.eliteencoder.net:8936`, independently of the publisher capability on
eliteprd1 (`https://ai1.eliteencoder.net:8936`). Capture, publisher signing,
payment-signing access, and the VLC HTTP bridge remain on elitedev.

- Five gateway tests pass, including real go-livepeer v0.9.3 offchain Trickle
  transport, AAC decode, input/session isolation, immediate release cleanup, and
  HTTP-listener disconnect cleanup. The 15 shared supply-side tests pass too.
- Docker image builds; repository formatting/lint and Compose validation pass.
- A paid playback session retrieved live content through eliteprd4's private Bee,
  returned it over the orchestrator's Trickle channel, and served it through the
  local HTTP bridge. FFmpeg decoded eight seconds of AAC-LC, 48 kHz stereo with
  exit code 0. This is a short public-network smoke test, not an endurance result.
- A cold retrieval session encountered an initial timeout before producing audio.
  Sample capture-to-runner-emission latency was approximately 7–11 seconds; the
  public network and VLC's additional buffering can increase end-to-end delay.
- Each capability advertises an introductory USD 0.10/hour price. Upload and
  playback are separate reservations; concurrent use costs USD 0.20/hour before
  publisher postage. Operator margins have not been established.

## eliteprd4 setup

Deployment directory: `/home/elite/livepeer-swarm-aac-gateway/source/swarm-aac-gateway`.
The existing orchestrator runs in Docker with host-firewall restrictions, so the
supported `compose.container.yml` profile is used. The gateway shares the
orchestrator container's network namespace; Bee joins its private Docker network.
The orchestrator container and its firewall were not modified or restarted.

Bee is version 2.8.3 in light mode, with persistent volume
`livepeer-swarm-aac-gateway_bee-data`. Its operator address is
`0x44843277590a9f5cCA0138d8f3dEEcE25C8ab272`, and its chequebook is
`0x7505ca99Ce69e60C0A5f32Edce91E3DC4186b6F6`. Initial synchronization took roughly
six minutes. The write/admin API is bound to host loopback port 11634.

The user authorized funding this playback gateway from the publisher wallet:

| Allocation                          | Amount                                                   | Transaction                                                          |
| ----------------------------------- | -------------------------------------------------------- | -------------------------------------------------------------------- |
| Operator bandwidth funds            | 0.1 xBZZ                                                 | `0xf0ee53018ecc33765a11c36b99f1272c32b0bd8dfe64df608917b3c233faf8f3` |
| Operator gas funds                  | 0.003 xDAI                                               | `0x9e4c52bdf0d4753033cd2174efbaed62fa46d17aee236912ac4c0557f6ddc04b` |
| Move allocated xBZZ into chequebook | Same 0.1 xBZZ, not an additional transfer from publisher | `0x7faf8a6ec311830ddbf087229ec80e5085ee8301bd523f920744bf8cc6143fa0` |

Publisher-side transfer gas was 0.000080747 xDAI. Bee's deployment/deposit gas comes
from its allocated operator gas funds. No new postage batch was purchased for
playback, and no publisher key was copied to the runner.

## Local VLC test

The bridge listens at `http://10.10.7.61:8788/stream.aac` on the trusted LAN.
Its temporary user service is `livepeer-swarm-prd4-vlc-bridge.service`; capture is
`livepeer-swarm-publisher-vlc-test.service`. Both were bounded to 30 minutes starting
approximately 01:18 Eastern on October 31. This URL is therefore a temporary test,
not an indefinitely running public station.

Use the documented client command in README to start another bounded session.
Do not restart an old publisher against expired postage or discard its counter
state. The local radio on port 8787 is a different path; this test specifically
uses port 8788 to route retrieval through eliteprd4.
