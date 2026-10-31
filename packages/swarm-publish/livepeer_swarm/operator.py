"""Read-only operator funding checks; publisher credentials never enter Bee."""

from decimal import Decimal

import aiohttp

BZZ_UNITS = Decimal(10**16)


class OperatorFunding:
    def __init__(self, bee_url, minimum_bzz):
        self.url = bee_url.rstrip("/")
        self.minimum = Decimal(str(minimum_bzz))
        if not self.minimum.is_finite() or self.minimum <= 0:
            raise ValueError("minimum operator balance must be positive and finite")

    async def snapshot(self):
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as http:

            async def get(path):
                async with http.get(self.url + path) as response:
                    response.raise_for_status()
                    return await response.json()

            balance = await get("/chequebook/balance")
            settlements = await get("/settlements")
        available = Decimal(balance["availableBalance"]) / BZZ_UNITS
        sent = Decimal(settlements["totalSent"]) / BZZ_UNITS
        received = Decimal(settlements["totalReceived"]) / BZZ_UNITS
        if not all(v.is_finite() for v in (available, sent, received)):
            raise ValueError("invalid Bee accounting response")
        return {
            "funded": available >= self.minimum,
            "available_bzz": str(available),
            "minimum_bzz": str(self.minimum),
            "settlements_sent_bzz": str(sent),
            "settlements_received_bzz": str(received),
            "bandwidth_payer": "operator",
            "upload_bandwidth_included": True,
        }

    async def require_funding(self):
        snapshot = await self.snapshot()
        if not snapshot["funded"]:
            raise ValueError("operator Bee chequebook requires funding")
        return snapshot
