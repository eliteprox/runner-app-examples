import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from livepeer_swarm.operator import OperatorFunding
from livepeer_swarm.service import LiveService


@pytest.mark.asyncio
async def test_balance_includes_outstanding_cheques_and_exact_units():
    app = web.Application()

    async def balance(request):
        return web.json_response(
            {"totalBalance": "10000000000000000", "availableBalance": "99999999999999"}
        )

    async def settlements(request):
        return web.json_response({"totalSent": "100000000000001", "totalReceived": "0"})

    app.router.add_get("/chequebook/balance", balance)
    app.router.add_get("/settlements", settlements)
    async with TestServer(app) as server:
        funding = OperatorFunding(str(server.make_url("/")), "0.01")
        snapshot = await funding.snapshot()
        assert snapshot["available_bzz"] == "0.0099999999999999"
        assert snapshot["settlements_sent_bzz"] == "0.0100000000000001"
        with pytest.raises(ValueError, match="requires funding"):
            await funding.require_funding()
        funding.minimum = 0
        assert (await funding.require_funding())["funded"]


@pytest.mark.asyncio
async def test_unfunded_operator_rejects_before_creating_channels():
    class Unfunded:
        async def require_funding(self):
            raise ValueError("empty")

    service = LiveService("http://unused", operator_funding=Unfunded())
    app = web.Application()
    service.attach(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/start",
            headers={"Livepeer-Session-Id": "test"},
            json={
                "owner": "0x" + "11" * 20,
                "batch_id": "22" * 32,
            },
        )
        assert response.status == 503
        assert service.sessions == {}


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_reserve(value):
    with pytest.raises(ValueError):
        OperatorFunding("http://unused", value)
