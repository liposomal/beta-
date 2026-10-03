"""Supabase asynchrone, transactions atomiques et journal durable à publier."""
import asyncio
import copy
import uuid
import aiohttp
from cryptography.fernet import Fernet


def initial_state():
    return {"phase": "unconfigured", "config": None, "count": 0,
            "pauses": [], "pause_until": 0, "requests": {}, "incident": None,
            "expected_guild_id": None, "generation": 0, "cursors": {}}


class Database:
    def __init__(self, config):
        self.url = config.supabase_url + "/rest/v1/"
        self.headers = {"apikey": config.supabase_service_role_key,
                        "Authorization": "Bearer " + config.supabase_service_role_key}
        self.cipher = Fernet(config.token_encryption_key.encode())
        self.session = None
        self.lock = asyncio.Lock()

    async def request(self, method, path, *, params=None, data=None, prefer=None):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        async with self.session.request(method, self.url + path, params=params,
                                        json=data, headers=headers) as response:
            if response.status >= 400:
                raise RuntimeError(f"Supabase : HTTP {response.status} ({path})")
            if response.status == 204 or response.content_length == 0:
                return None
            body = await response.read()
            if not body:
                return None
            import json
            return json.loads(body)

    async def read(self):
        rows = await self.request("GET", "sentinel_state", params={"id": "eq.1"})
        if not rows:
            raise RuntimeError("Appliquer la migration SQL Sentinelle v2 avant le démarrage.")
        return rows[0]

    async def state(self):
        return (await self.read())["document"]

    async def transact(self, change, key=None):
        """change(document, events) est pur. État et journal sont commités ensemble."""
        key = key or str(uuid.uuid4())
        async with self.lock:
            for _ in range(8):
                row = await self.read()
                state = copy.deepcopy(row["document"])
                events = []
                result = change(state, events)
                if state == row["document"] and not events:
                    return result
                response = await self.request("POST", "rpc/sentinel_commit", data={
                    "expected_version": row["version"], "operation_key": key,
                    "new_document": state, "new_events": events,
                })
                if response == "duplicate":
                    return None
                if response == "ok":
                    return result
            raise RuntimeError("Configuration modifiée simultanément ; réessayer.")

    async def events(self, pending=False, limit=30):
        params = {"order": "created_at.asc" if pending else "created_at.desc", "limit": str(limit)}
        if pending:
            params["delivered_at"] = "is.null"
        return await self.request("GET", "sentinel_events", params=params)

    async def delivered(self, event_id, timestamp):
        await self.request("PATCH", "sentinel_events", params={"id": f"eq.{event_id}"},
                           data={"delivered_at": timestamp}, prefer="return=minimal")

    async def members(self, source, destination):
        offset = 0
        while True:
            rows = await self.request("GET", "sentinel_members", params={
                "source_id": f"eq.{source}", "destination_id": f"eq.{destination}",
                "order": "user_id.asc", "limit": "500", "offset": str(offset),
            })
            for row in rows:
                yield row
            if len(rows) < 500:
                break
            offset += len(rows)

    async def member(self, source, destination, user):
        rows = await self.request("GET", "sentinel_members", params={
            "source_id": f"eq.{source}", "destination_id": f"eq.{destination}", "user_id": f"eq.{user}",
        })
        return rows[0] if rows else None

    async def save_member(self, row):
        await self.request("POST", "sentinel_members", data=row,
                           prefer="resolution=merge-duplicates,return=minimal")

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.cipher.decrypt(value.encode()).decode()

    async def close(self):
        if self.session:
            await self.session.close()
