"""Keyless publisher pipeline. Each stamp request covers one bounded upload."""

import asyncio
import time
import uuid
from collections import OrderedDict

from eth_hash.auto import keccak

from .bee import verify_stamp
from .bmt import chunk_address, chunk_data, pyramid

MAX_UPLOAD_BYTES = 256 * 1024


class UploadPipeline:
    def __init__(self, bee, owner, batch, emit):
        self.bee, self.owner, self.batch, self.emit = bee, owner, batch, emit
        self.pending = {}
        self.completed = OrderedDict()

    def answer(self, message):
        future = self.pending.get(message.get("request_id"))
        if future and not future.done():
            if message.get("error"):
                future.set_exception(ValueError("publisher refused stamp request"))
            else:
                future.set_result(message["stamps"])

    async def publish(self, upload_id, payload):
        if not payload or len(payload) > MAX_UPLOAD_BYTES:
            raise ValueError("media upload must be 1..262144 bytes")
        digest = keccak(payload)
        if upload_id in self.completed:
            old_digest, result = self.completed[upload_id]
            if digest != old_digest:
                raise ValueError("upload ID reused with different bytes")
            return result
        started = time.monotonic()
        # BMT and signing work must not starve health/payment/channel readers.
        chunks = await asyncio.to_thread(
            lambda: [
                (chunk_address(span, body), chunk_data(span, body))
                for span, body in pyramid(payload)
            ]
        )
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.emit(
                {
                    "type": "stamp-request",
                    "request_id": request_id,
                    "upload_id": upload_id,
                    "addresses": [address.hex() for address, _ in chunks],
                }
            )
            stamps = await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(request_id, None)
        signing_s = time.monotonic() - started
        if len(stamps) != len(chunks):
            raise ValueError("stamp count mismatch")

        def validate():
            for (address, _), stamp in zip(chunks, stamps):
                verify_stamp(address, bytes.fromhex(stamp), self.owner, self.batch)

        await asyncio.to_thread(validate)
        await asyncio.gather(
            *(
                self.bee.put_chunk(body, stamp)
                for (_, body), stamp in zip(chunks, stamps)
            )
        )
        result = {
            "type": "uploaded",
            "upload_id": upload_id,
            "ref": chunks[-1][0].hex(),
            "bytes": len(payload),
            "chunks": len(chunks),
            "signing_s": signing_s,
            "publish_s": time.monotonic() - started,
        }
        self.completed[upload_id] = (digest, result)
        if len(self.completed) > 64:
            self.completed.popitem(last=False)
        return result
