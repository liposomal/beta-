"""Consentement OAuth2 avec state, cookie navigateur et destination explicite."""
import asyncio
import secrets
import time
from urllib.parse import urlencode

import discord
from aiohttp import web
from oauth2 import OAuthError


@web.middleware
async def security_headers(request, handler):
    try:
        response = await handler(request)
    except (RuntimeError, asyncio.TimeoutError):
        response = web.Response(status=503, text="Service temporairement indisponible. Réessayez plus tard.")
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                             "X-Content-Type-Options": "nosniff"})
    return response


class OAuthServer:
    def __init__(self, bot):
        self.bot = bot
        self.pending = {}
        self.runner = None
        self.app = web.Application(middlewares=[security_headers], client_max_size=8192)
        self.app.add_routes([web.get("/authorize", self.authorize), web.get("/callback", self.callback)])

    @property
    def secure(self):
        return self.bot.config.oauth2_redirect_uri.startswith("https:")

    async def authorize(self, request):
        state = await self.bot.db.state()
        cfg = state.get("config")
        if state["phase"] != "active" or not cfg or request.query.get("source") != cfg["primary_id"]:
            return web.Response(status=409, text="Aucune inscription active pour ce serveur.")
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        if len(self.pending) >= 4096:
            return web.Response(status=429, text="Trop de demandes ; réessayez plus tard.")
        nonce = secrets.token_urlsafe(32)
        self.pending[nonce] = {"expires": now + 600, "generation": state["generation"],
                               "source": cfg["primary_id"], "destination": cfg["backup_id"]}
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": self.bot.oauth.client_id, "redirect_uri": self.bot.config.oauth2_redirect_uri,
            "response_type": "code", "scope": "identify guilds.join", "state": nonce,
            "prompt": "consent",
        })
        response = web.Response(status=302, headers={"Location": url})
        response.set_cookie("sentinel_oauth_state", nonce, max_age=600, httponly=True,
                            secure=self.secure, samesite="Lax", path="/callback")
        return response

    async def callback(self, request):
        nonce = request.query.get("state", "")
        cookie = request.cookies.get("sentinel_oauth_state", "")
        if not nonce or not cookie or not secrets.compare_digest(nonce, cookie):
            return web.Response(status=400, text="Session OAuth invalide. Recommencez depuis Discord.")
        pending = self.pending.pop(nonce, None)
        if not pending or pending["expires"] <= time.time():
            return web.Response(status=400, text="Autorisation expirée ou déjà utilisée.")
        if "error" in request.query or not request.query.get("code"):
            return web.Response(status=400, text="Autorisation refusée. Aucune inscription effectuée.")
        state = await self.bot.db.state()
        if state["generation"] != pending["generation"] or state["phase"] != "active":
            return web.Response(status=409, text="La destination a changé ; recommencez l'inscription.")
        try:
            tokens = await self.bot.oauth.exchange_code(request.query["code"])
            if not {"identify", "guilds.join"} <= set(tokens.get("scope", "").split()):
                return web.Response(status=400, text="Les autorisations nécessaires n'ont pas été accordées.")
            user = await self.bot.oauth.identity(tokens["access_token"])
            guild = self.bot.get_guild(int(pending["source"]))
            if guild is None:
                return web.Response(status=409, text="Le serveur principal est inaccessible.")
            member = await guild.fetch_member(int(user["id"]))
            if member.bot:
                return web.Response(status=400, text="Inscription réservée aux membres humains.")
            async with self.bot.oauth.member_locks[user["id"]]:
                # Vérifier à nouveau après les appels réseau.
                latest = await self.bot.db.state()
                if latest["generation"] != pending["generation"] or latest["phase"] != "active":
                    return web.Response(status=409, text="Configuration modifiée ; recommencez.")
                await self.bot.db.save_member({
                    "source_id": pending["source"], "destination_id": pending["destination"],
                    "user_id": user["id"], "access_token": self.bot.db.encrypt(tokens["access_token"]),
                    "refresh_token": self.bot.db.encrypt(tokens["refresh_token"]),
                    "expires_at": time.time() + int(tokens["expires_in"]),
                    "revoked": False, "is_active": True, "joined": False, "last_error": None,
                })
        except discord.NotFound:
            return web.Response(status=403, text="Vous devez appartenir au serveur principal.")
        except (OAuthError, discord.HTTPException, RuntimeError, asyncio.TimeoutError):
            return web.Response(status=503, text="Inscription indisponible ; réessayez depuis Discord.")
        response = web.Response(text="Inscription enregistrée pour le serveur de secours choisi. Vous pouvez fermer cette page.")
        response.del_cookie("sentinel_oauth_state", path="/callback")
        return response

    async def start(self):
        # Aucun access log : les URL de callback contiennent un code secret.
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.bot.config.http_host, self.bot.config.http_port).start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
