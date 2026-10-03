"""Maintenance périodique : erreurs transitoires distinctes des révocations."""
import asyncio
import logging
import discord
from governance import emit
from oauth2 import RevokedToken, OAuthError

log = logging.getLogger(__name__)


class MaintenanceJob:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def run(self):
        if self.lock.locked():
            return
        async with self.lock:
            state = await self.bot.db.state()
            if state["phase"] != "active":
                return
            cfg = state["config"]
            guild = self.bot.get_guild(int(cfg["primary_id"]))
            if guild is None or guild.unavailable:
                return
            # Une récupération complète doit réussir avant de désactiver un membre.
            current = {str(member.id) async for member in guild.fetch_members(limit=None)}
            stats = {"refreshed": 0, "revoked": 0, "temporary_errors": 0}
            rows = [row async for row in self.bot.db.members(cfg["primary_id"], cfg["backup_id"])]
            for row in rows:
                latest = await self.bot.db.state()
                if latest["phase"] != "active" or latest["generation"] != state["generation"]:
                    break
                async with self.bot.oauth.member_locks[row["user_id"]]:
                    row = await self.bot.db.member(row["source_id"], row["destination_id"], row["user_id"]) or row
                    row["is_active"] = row["user_id"] in current
                    await self.bot.db.save_member(row)
                    if not row["is_active"] or row["revoked"]:
                        continue
                    try:
                        before = row["expires_at"]
                        row = await self.bot.oauth.fresh_member(self.bot.db, row)
                        stats["refreshed"] += row["expires_at"] != before
                    except RevokedToken:
                        stats["revoked"] += 1
                    except OAuthError:
                        stats["temporary_errors"] += 1
            def record(s, events):
                emit(s, events, "maintenance", f"Maintenance : {stats}.")
            await self.bot.db.transact(record)
            return stats
