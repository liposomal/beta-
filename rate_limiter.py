"""Limiteur partagé par toutes les requêtes OAuth et REST directes."""
import asyncio
import time


class RateLimiter:
    def __init__(self, delay=0.25):
        self.delay = delay
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def acquire(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next_at - time.monotonic()))
            self.next_at = time.monotonic() + self.delay

    async def handle_429(self, retry_after):
        async with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + max(0, retry_after))
