"""Publication du journal durable. Aucune suppression de salon."""
import asyncio
import logging
from datetime import datetime, timezone

import discord

log = logging.getLogger(__name__)


def writable_channels(guild, preferred=None):
    if guild is None or guild.me is None:
        return []
    candidates = [guild.get_channel(int(preferred)) if preferred else None,
                  guild.system_channel, *sorted(guild.text_channels, key=lambda c: (c.position, c.id))]
    found = []
    for channel in candidates:
        if not isinstance(channel, discord.TextChannel) or channel in found:
            continue
        permissions = channel.permissions_for(guild.me)
        if permissions.view_channel and permissions.send_messages:
            found.append(channel)
    return found


class CrisisCommunicator:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def flush(self):
        async with self.lock:
            for event in await self.bot.db.events(pending=True):
                payload = event["payload"]
                cfg = payload.get("config")
                if not cfg:
                    continue
                # Le secours reçoit les incidents même après exclusion du principal.
                primary = self.bot.get_guild(int(cfg["primary_id"]))
                backup = self.bot.get_guild(int(cfg["backup_id"]))
                groups = [writable_channels(primary, cfg["alert_channel_id"]),
                          writable_channels(backup, cfg["backup_channel_id"])]
                delivered = False
                message = f"[Sentinelle · {event['id']}] {payload['message']}"
                if payload["kind"] == "vote" and payload.get("details", {}).get("request", {}).get("payload"):
                    import json
                    message += "\nConfiguration proposée :\n" + json.dumps(payload["details"]["request"]["payload"], ensure_ascii=False)
                for channels in groups:
                    for channel in channels:
                        try:
                            await channel.send(message[:1950], allowed_mentions=discord.AllowedMentions.none())
                            delivered = True
                            break
                        except discord.HTTPException:
                            continue
                if delivered:
                    await self.bot.db.delivered(event["id"], datetime.now(timezone.utc).isoformat())
                else:
                    log.error("Journal %s non publié ; conservé en base pour réessai.", event["id"])
