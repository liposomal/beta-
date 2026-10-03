"""Client Discord OAuth2 : erreurs explicites, authentification et reprises bornées."""
import asyncio
import time
from collections import defaultdict

import aiohttp

API = "https://discord.com/api/v10"


class OAuthError(RuntimeError):
    def __init__(self, status, code="api_error"):
        super().__init__(f"Discord HTTP {status} ({code})")
        self.status, self.code = status, code


class RevokedToken(OAuthError):
    pass


class OAuth2Manager:
    def __init__(self, config, client_id, limiter):
        self.config, self.client_id, self.limiter = config, str(client_id), limiter
        self.session = None
        self.member_locks = defaultdict(asyncio.Lock)

    async def request(self, method, path, *, data=None, body=None, headers=None):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        for attempt in range(4):
            await self.limiter.acquire()
            try:
                async with self.session.request(method, API + path, data=data, json=body, headers=headers) as response:
                    status = response.status
                    try:
                        payload = await response.json() if status != 204 else {}
                    except (ValueError, aiohttp.ContentTypeError):
                        payload = {}
                    if status == 429:
                        await self.limiter.handle_429(float(payload.get("retry_after", 1)))
                        continue
                    if status >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if status >= 400:
                        code = str(payload.get("error", payload.get("code", "api_error")))
                        if code == "invalid_grant":
                            raise RevokedToken(status, code)
                        raise OAuthError(status, code)
                    return status, payload
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 3:
                    raise OAuthError(503, "network_error") from None
                await asyncio.sleep(2 ** attempt)
        raise OAuthError(503, "retry_exhausted")

    async def tokens(self, grant):
        _, payload = await self.request("POST", "/oauth2/token", data={
            "client_id": self.client_id, "client_secret": self.config.oauth2_client_secret, **grant,
        })
        for name in ("access_token", "refresh_token", "expires_in"):
            if name not in payload:
                raise OAuthError(502, "incomplete_token_response")
        return payload

    async def exchange_code(self, code):
        return await self.tokens({"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": self.config.oauth2_redirect_uri})

    async def refresh_token(self, token):
        return await self.tokens({"grant_type": "refresh_token", "refresh_token": token})

    async def identity(self, access):
        return (await self.request("GET", "/users/@me", headers={"Authorization": "Bearer " + access}))[1]

    async def add_member_to_guild(self, user, access, guild):
        status, _ = await self.request("PUT", f"/guilds/{guild}/members/{user}",
            body={"access_token": access},
            headers={"Authorization": "Bot " + self.config.sentinelle_token})
        return status in (201, 204)

    async def fresh_member(self, db, row, force=False):
        """Appelé sous member_locks[user] par maintenance/évacuation/callback."""
        current = await db.member(row["source_id"], row["destination_id"], row["user_id"])
        row = current or row
        if row["revoked"]:
            raise RevokedToken(400, "invalid_grant")
        if force or row["expires_at"] <= time.time() + 3600:
            try:
                tokens = await self.refresh_token(db.decrypt(row["refresh_token"]))
            except RevokedToken:
                row["revoked"] = True
                await db.save_member(row)
                raise
            row.update(access_token=db.encrypt(tokens["access_token"]),
                       refresh_token=db.encrypt(tokens["refresh_token"]),
                       expires_at=time.time() + int(tokens["expires_in"]))
            await db.save_member(row)
        return row

    async def close(self):
        if self.session:
            await self.session.close()
