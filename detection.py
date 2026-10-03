"""Adaptateur Discord : audit, rattrapage après déconnexion et rôle du conseil."""
import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from governance import emit

log = logging.getLogger(__name__)


class DetectionEngine:
    def __init__(self, bot):
        self.bot = bot
        self.poll_lock = asyncio.Lock()
        self.role_lock = asyncio.Lock()

    async def entry(self, entry):
        state = await self.bot.db.state()
        cfg = state.get("config")
        if not cfg or str(entry.guild.id) not in {cfg["primary_id"], cfg["backup_id"]}:
            return
        owner = entry.guild.owner_id
        for since, owner_id in sorted(cfg.get("owner_history", {}).get(str(entry.guild.id), [])):
            if since <= entry.created_at.timestamp():
                owner = int(owner_id)
        # Un transfert appartient à l'ancien propriétaire, pas au nouveau.
        previous_owner = getattr(entry.before, "owner", None)
        if entry.action == discord.AuditLogAction.guild_update and previous_owner:
            owner = previous_owner.id
        details = {"before": {}, "after": {}}
        for label, diff in (("before", entry.before), ("after", entry.after)):
            for key, value in diff:
                details[label][key] = str(value)[:500]
        await self.bot.governance.audit(
            entry.guild.id, entry.id, entry.user_id, owner, entry.action.name,
            getattr(entry.target, "id", None), entry.created_at.timestamp(), details,
        )
        next_owner = getattr(entry.after, "owner", None)
        if entry.action == discord.AuditLogAction.guild_update and next_owner:
            await self.observe_owner(entry.guild.id, next_owner.id, entry.created_at.timestamp())

    async def poll(self):
        if self.poll_lock.locked():
            return
        async with self.poll_lock:
            snapshot = await self.bot.db.state()
            cfg = snapshot.get("config")
            if not cfg:
                return
            for guild_id in (cfg["primary_id"], cfg["backup_id"]):
                guild = self.bot.get_guild(int(guild_id))
                if guild is None or guild.unavailable:
                    continue
                marker = snapshot["cursors"].get(guild_id)
                after = (discord.Object(id=int(marker)) if marker else
                         datetime.fromtimestamp(cfg["activated_at"], timezone.utc))
                last = None
                try:
                    async for entry in guild.audit_logs(limit=200, after=after, oldest_first=True):
                        await self.entry(entry)
                        last = entry.id
                except discord.HTTPException:
                    await self.warning("audit:" + guild_id, "Journal Discord inaccessible sur " + guild_id + ". Surveillance dégradée.")
                    continue
                if last:
                    def advance(s, events):
                        if s["generation"] == snapshot["generation"]:
                            s["cursors"][guild_id] = str(max(int(s["cursors"].get(guild_id, 0)), last))
                    await self.bot.db.transact(advance)
                # Mettre à jour les propriétaires même si le transfert a eu lieu hors ligne.
                try:
                    actual = await self.bot.fetch_guild(int(guild_id))
                    await self.observe_owner(guild_id, actual.owner_id)
                except discord.HTTPException:
                    pass
                # Vérification indépendante des intents/cache pour le bot gardien.
                if guild_id == cfg["primary_id"] and snapshot["phase"] == "active":
                    try:
                        await guild.fetch_member(int(cfg["guardian_id"]))
                    except discord.NotFound:
                        await self.bot.governance.missing_bot(guild.id, cfg["guardian_id"])
                    except discord.HTTPException:
                        pass

    async def warning(self, key, message):
        def record(s, events):
            emit(s, events, "warning", message)
        await self.bot.db.transact(record, key=f"warning:{key}:{int(time.time() // 300)}")

    async def reconcile_roles(self, member=None):
        async with self.role_lock:
            state = await self.bot.db.state()
            cfg = state.get("config")
            if state["phase"] != "active" or not cfg:
                return
            guild = self.bot.get_guild(int(cfg["primary_id"]))
            if not guild or (member and member.guild.id != guild.id):
                return
            role = guild.get_role(int(cfg["council_role_id"]))
            if role is None:
                await self.warning("role_missing", "Rôle du conseil supprimé. Les cinq identités enregistrées conservent le droit de vote ; proposer une configuration avec un nouveau rôle.")
                return
            members = [member] if member else [m async for m in guild.fetch_members(limit=None)]
            for current in members:
                latest = await self.bot.db.state()
                if latest["generation"] != state["generation"] or latest["phase"] != "active":
                    return
                expected = str(current.id) in cfg["council_ids"]
                actual = role in current.roles
                if expected == actual:
                    continue
                try:
                    if expected:
                        await current.add_roles(role, reason="Sentinelle : restauration du conseil approuvé")
                    else:
                        await current.remove_roles(role, reason="Sentinelle : rôle réservé aux cinq conseillers")
                except discord.HTTPException:
                    await self.warning("role:" + str(current.id), f"Impossible de corriger le rôle du conseil pour {current.id}. Vérifier permissions et hiérarchie.")

    async def ownership_changed(self, before, after):
        if before.owner_id == after.owner_id:
            return
        await self.observe_owner(after.id, after.owner_id)

    async def observe_owner(self, guild_id, owner_id, occurred_at=None):
        at = occurred_at if occurred_at is not None else time.time()
        def change(s, events):
            cfg = s.get("config")
            if not cfg:
                return
            if str(guild_id) == cfg["primary_id"]:
                field = "owner_id"
            elif str(guild_id) == cfg["backup_id"]:
                field = "backup_owner_id"
            else:
                return
            history = cfg.setdefault("owner_history", {}).setdefault(str(guild_id), [])
            if occurred_at is not None and [at, str(owner_id)] not in history:
                history.append([at, str(owner_id)])
                history.sort()
            if cfg[field] == str(owner_id):
                return
            previous = cfg[field]
            cfg[field] = str(owner_id)
            if occurred_at is None:
                history.append([at, str(owner_id)])
            emit(s, events, "ownership", f"Propriétaire du serveur {guild_id} : {previous} → {owner_id}. Aucun changement des droits de vote.")
        await self.bot.db.transact(change)
