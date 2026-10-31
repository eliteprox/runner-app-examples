import asyncio
import io
import json
import os
from types import SimpleNamespace

import av
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from eth_account import Account
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import register_runner
from livepeer_gateway.selection import reserve_session
from livepeer_gateway.trickle_subscriber import TrickleSubscriber
from livepeer_swarm.bmt import chunk_address, pyramid
from livepeer_swarm.feeds import sign_feed
from livepeer_swarm.stub import CHUNKS, build_app
from livepeer_swarm_radio.audio import capture
from livepeer_swarm_radio.hls import master_playlist, media_playlist

from service import APP_ID, Gateway

OWNER = "0x" + "11" * 20


@pytest.mark.asyncio
async def test_validation_and_operator_failure_before_allocation():
    class Unfunded:
        async def require_funding(self):
            raise ValueError("empty")

    gateway = Gateway("http://unused", funding=Unfunded())
    app = web.Application()
    gateway.attach(app)
    async with TestClient(TestServer(app)) as client:
        assert (
            await client.post("/start", json={"owner": OWNER, "station": "x"})
        ).status == 400
        for payload in (
            {"owner": OWNER, "station": "x", "bee_url": "http://internal"},
            {"owner": OWNER, "station": ""},
            {"owner": "bad", "station": "x"},
            [],
        ):
            response = await client.post(
                "/start", json=payload, headers={"Livepeer-Session-Id": "s"}
            )
            assert response.status == 400
        response = await client.post(
            "/start",
            json={"owner": OWNER, "station": "x"},
            headers={"Livepeer-Session-Id": "s"},
        )
        assert response.status == 503
        assert gateway.playback is None


@pytest.mark.asyncio
async def test_session_idempotency_isolation_and_release(monkeypatch):
    class FakePlayback:
        def __init__(self, sid, owner, station, *args):
            self.sid, self.owner, self.station = sid, owner, station
            self.closed = False

        def response(self):
            return {"session": self.sid}

        async def close(self):
            self.closed = True

    monkeypatch.setattr("service.Playback", FakePlayback)

    class Registration:
        calls = 0

        async def create_trickle_channels(self, request, channels):
            self.calls += 1
            assert channels == [{"name": "out", "mime_type": "audio/aac"}]
            return [{"name": "out", "url": "http://channel"}]

    gateway = Gateway("http://unused")
    app = web.Application()
    gateway.attach(app)
    app["registration"] = registration = Registration()
    async with TestClient(TestServer(app)) as client:

        async def start(sid, station="x"):
            return await client.post(
                "/start",
                json={"owner": OWNER, "station": station},
                headers={"Livepeer-Session-Id": sid},
            )

        assert (await start("one")).status == 200
        assert (await start("one")).status == 200
        assert registration.calls == 1
        assert (await start("two")).status == 409
        assert (await start("one", "different")).status == 409
        await gateway.released(SimpleNamespace(session_id="two"))
        assert gateway.playback is not None
        await gateway.released(SimpleNamespace(session_id="one"))
        assert gateway.playback is None
        assert (await start("two")).status == 200


@pytest.mark.asyncio
async def test_real_trickle_swarm_to_decodable_aac(tmp_path):
    url = os.environ.get("SWARM_TEST_ORCHESTRATOR")
    if not url:
        pytest.skip("requires dedicated offchain go-livepeer")
    source = tmp_path / "tone.wav"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "4",
        str(source),
    )
    assert await proc.wait() == 0
    segments = [s async for s in capture(str(source), paced=False)]
    bee_app = build_app()
    chunks = bee_app[CHUNKS]

    def store(data):
        for span, payload in pyramid(data):
            address = chunk_address(span, payload).hex()
            chunks[address] = span.to_bytes(8, "little") + payload
        return address

    init = store(segments[0].init)
    entries = [
        dict(
            sequence=i,
            duration=s.duration,
            ref=store(s.data),
            captured_at=s.captured_at,
            discontinuity=False,
            discontinuity_sequence=0,
        )
        for i, s in enumerate(segments)
    ]
    key = Account.from_key(bytes([1]) * 32)
    for topic, data in [
        ("station", json.dumps({"topic": "broadcast"})),
        ("broadcast", master_playlist()),
        ("broadcast#source", media_playlist(entries, init, end=True)),
    ]:
        signed = sign_feed(topic, 0, data.encode(), key)
        chunks[signed.address.hex()] = signed.data
    async with TestServer(bee_app) as bee:
        gateway = Gateway(str(bee.make_url("")).rstrip("/"))
        app = web.Application()
        gateway.attach(app)
        async with TestServer(app) as runner:
            registration = await register_runner(
                url,
                secret="swarm-test-only",
                runner_url=str(runner.make_url("")).rstrip("/"),
                app=APP_ID,
                mode="persistent",
                price=0,
                auto_detect_gpu=False,
                on_session_release=gateway.released,
            )
            app["registration"] = registration
            try:
                async with asyncio.timeout(25):
                    async with await reserve_session(
                        orchestrators=[url], app=APP_ID
                    ) as session:
                        response = await post_json(
                            session.app_url.rstrip("/") + "/start",
                            {"owner": key.address, "station": "station"},
                        )
                        assert response["mime_type"] == "audio/aac"
                        audio = bytearray()
                        async with TrickleSubscriber(
                            response["out"], start_seq=0
                        ) as sub:
                            while (segment := await sub.next()) is not None:
                                try:
                                    reader = segment.make_reader()
                                    while data := await reader.read():
                                        audio.extend(data)
                                finally:
                                    await segment.close()
                        with av.open(io.BytesIO(audio), format="aac") as media:
                            frames = list(media.decode(audio=0))
                            assert frames and frames[0].sample_rate == 48000
                            assert sum(f.samples for f in frames) / 48000 > 1.9
            finally:
                await registration.close()


@pytest.mark.asyncio
async def test_release_before_sender_runs_closes_all_resources(monkeypatch):
    from service import Playback

    class Bee:
        closed = False

        def __init__(self, url):
            pass

        async def close(self):
            self.closed = True

    class Publisher(Bee):
        def __init__(self, *args):
            pass

    monkeypatch.setattr("service.BeeClient", Bee)
    monkeypatch.setattr("service.TricklePublisher", Publisher)
    playback = Playback(
        "s", OWNER, "x", [{"url": "http://unused"}], "http://unused", 30
    )
    await playback.close()
    assert playback.bee.closed and playback.publisher.closed
    assert playback.poller.done() and playback.sender.done()
    assert not playback.radio.listeners


@pytest.mark.asyncio
async def test_vlc_bridge_disconnect_closes_paid_generator(
    monkeypatch, unused_tcp_port
):
    import aiohttp
    import client

    started, closed = asyncio.Event(), asyncio.Event()

    async def fake_audio(args):
        started.set()
        try:
            yield b"fake-aac"
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(client, "audio_stream", fake_audio)
    args = SimpleNamespace(
        listen_port=unused_tcp_port, listen_host="127.0.0.1", seconds=10
    )
    task = asyncio.create_task(client.run(args))
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(100):
                try:
                    response = await http.get(
                        f"http://127.0.0.1:{unused_tcp_port}/stream.aac"
                    )
                    break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.01)
            else:
                pytest.fail("bridge did not start")
            assert started.is_set()
            assert response.headers["Content-Type"] == "audio/aac"
            assert await response.content.read(8) == b"fake-aac"
            second = await http.get(f"http://127.0.0.1:{unused_tcp_port}/stream.aac")
            assert second.status == 409
            response.close()
            await asyncio.wait_for(closed.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnect_reuses_authenticated_frontiers_without_cross_owner_leak(
    monkeypatch,
):
    from collections import OrderedDict
    from livepeer_swarm.bee import BeeClient
    from service import Playback

    class Publisher:
        def __init__(self, *args):
            pass

        async def close(self):
            pass

    monkeypatch.setattr("service.TricklePublisher", Publisher)
    cache = OrderedDict()
    first = Playback(
        "one", OWNER, "station", [{"url": "http://unused"}], "http://unused", 30, cache
    )
    # Simulate the result already authenticated by BeeClient.verify_soc.
    first.bee.frontiers[(OWNER.lower(), "station")] = (42, b"authenticated")
    await first.close()
    second = Playback(
        "two", OWNER, "station", [{"url": "http://unused"}], "http://unused", 30, cache
    )
    called = []

    async def missing(owner, topic, index):
        called.append(index)
        return None

    second.bee.feed_at = missing
    try:
        result = await BeeClient.latest_feed(second.bee, OWNER, "station")
        assert result == (42, b"authenticated")
        assert called == [
            43
        ], "reconnect should probe the frontier, not replay discovery"
        assert ("0x" + "22" * 20, "station") not in cache
        assert second.bee.frontiers is cache
    finally:
        await second.close()
