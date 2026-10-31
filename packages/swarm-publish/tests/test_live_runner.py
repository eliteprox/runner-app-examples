import json
from pathlib import Path

import pytest
from eth_account import Account


def test_soc_matches_bee_oracle():
    from livepeer_swarm.feeds import sign_feed, verify_soc

    v = json.loads((Path(__file__).parent / "fixtures/swarm_vectors.json").read_text())
    f = v["feed"]
    signed = sign_feed(
        f["topic_name"],
        f["index"],
        b"#EXTM3U\n#EXT-X-VERSION:7\n",
        Account.from_key(v["key"]),
    )
    assert signed.address.hex() == f["soc_address"].removeprefix("0x")
    assert signed.data.hex() == f["soc_data"].removeprefix("0x")
    assert verify_soc(signed.data, signed.address) == b"#EXTM3U\n#EXT-X-VERSION:7\n"


def test_soc_tampering_is_rejected():
    from livepeer_swarm.feeds import sign_feed, verify_soc

    signed = sign_feed("radio", 0, b"valid payload", Account.from_key(bytes([1]) * 32))
    with pytest.raises(ValueError):
        verify_soc(signed.data[:-1] + b"X", signed.address)


@pytest.mark.asyncio
async def test_media_reference_is_verified_before_playback():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from livepeer_swarm.bee import BeeClient

    app = web.Application()

    async def corrupted(request):
        return web.Response(body=b"corrupted audio")

    app.router.add_get("/bytes/{ref}", corrupted)
    async with (
        TestServer(app) as server,
        BeeClient(str(server.make_url("")).rstrip("/")) as bee,
    ):
        with pytest.raises(ValueError, match="reference"):
            await bee.read_bytes("ab" * 32)


@pytest.mark.asyncio
async def test_frontier_timeout_preserves_authenticated_payload(tmp_path):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from livepeer_swarm.bee import BeeClient
    from livepeer_swarm.feeds import sign_feed

    signed = sign_feed(
        "slow-frontier", 0, b"available", Account.from_key(bytes([1]) * 32)
    )
    reads = []

    async def chunks(request):
        reads.append(request.match_info["address"])
        if request.match_info["address"] == signed.address.hex():
            return web.Response(body=signed.data)
        await asyncio.sleep(0.2)
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/chunks/{address}", chunks)
    async with (
        TestServer(app) as server,
        BeeClient(str(server.make_url("")).rstrip("/"), timeout=0.03) as bee,
    ):
        assert await bee.latest_feed(signed.owner, "slow-frontier") == (0, b"available")
        assert await bee.latest_feed(signed.owner, "slow-frontier") == (0, b"available")
        assert reads.count(signed.address.hex()) == 1


@pytest.mark.asyncio
async def test_pipeline_acknowledges_after_upload_and_rejects_id_reuse():
    from aiohttp.test_utils import TestServer
    from livepeer_swarm.bee import BeeClient
    from livepeer_swarm.live import UploadPipeline
    from livepeer_swarm.stamp import StampIssuer
    from livepeer_swarm.stub import build_app

    account = Account.from_key(bytes([1]) * 32)
    batch = bytes([2]) * 32
    issuer = StampIssuer(batch)
    requests = []
    async with (
        TestServer(build_app()) as server,
        BeeClient(str(server.make_url("")).rstrip("/")) as bee,
    ):

        async def emit(request):
            requests.append(request)
            pipeline.answer(
                {
                    "request_id": request["request_id"],
                    "stamps": [
                        issuer.stamp(bytes.fromhex(address), account).hex()
                        for address in request["addresses"]
                    ],
                }
            )

        pipeline = UploadPipeline(bee, account.address, batch, emit)
        payload = b"audio" * 2000
        result = await pipeline.publish("one", payload)
        assert await bee.read_bytes(result["ref"]) == payload
        assert await pipeline.publish("one", payload) == result
        assert len(requests) == 1
        with pytest.raises(ValueError, match="different bytes"):
            await pipeline.publish("one", b"other audio")
