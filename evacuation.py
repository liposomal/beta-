"""Migration non destructive, avec bilan exact et reprise des échecs."""
import asyncio
import logging
import time
import uuid

import discord
from governance import emit, RuleError, require_council
from oauth2 import OAuthError, RevokedToken

log = logging.getLogger(__name__)


class EvacuationWorker:
    def __init__(self, bot):
        self.bot = bot
        self.worker_id = uuid.uuid4().hex
        self.lock = asyncio.Lock()

    async def retry(self, user, guild):
        def change(s, events):
            require_council(s, user, guild)
            incident = s.get("incident")
            if s["phase"] != "migrating" or not incident or incident["status"] not in {"failed", "partial", "empty"}:
                raise RuleError("Aucune migration incomplète à reprendre.")
            incident["status"] = "pending"
            emit(s, events, "migration", "Reprise demandée ; même destination, membres non transférés uniquement.")
        await self.bot.db.transact(change)

    async def run(self):
        if self.lock.locked():
            return
        async with self.lock:
            now = time.time()
            def claim(s, events):
                item = s.get("incident")
                if s["phase"] != "migrating" or not item:
                    return None
                if item["status"] not in {"pending", "running"} or item["lease_until"] > now:
                    return None
                item.update(status="running", lease_owner=self.worker_id, lease_until=now + 120)
                emit(s, events, "migration", "Évacuation OAuth2 en cours. Aucun salon ne sera supprimé.")
                return item.copy()
            incident = await self.bot.db.transact(claim)
            if not incident:
                return
            heartbeat = asyncio.create_task(self.heartbeat(incident["id"]))
            try:
                result = await self.evacuate(incident)
                status = ("empty" if result["eligible"] == 0 else
                          "partial" if result["failed"] else "completed")
            except Exception:
                log.exception("Migration interrompue ; les résultats par membre sont conservés.")
                status, result = "failed", None
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            def finish(s, events):
                item = s.get("incident")
                if not item or item["id"] != incident["id"] or item["lease_owner"] != self.worker_id:
                    return
                item.update(status=status, result=result, lease_until=0, lease_owner=None)
                if status == "completed":
                    s["phase"] = "awaiting_configuration"
                    s["expected_guild_id"] = item["config"]["backup_id"]
                emit(s, events, "migration_result",
                     f"Bilan migration : {status}, {result}. " +
                     ("Le propriétaire du secours doit lancer !configurer pour activer le nouveau principal."
                      if status == "completed" else "Conseil : !reprendre, ou !finaliser puis cinq approbations pour accepter ce bilan."),
                     incident_id=item["id"], result=result)
            await self.bot.db.transact(finish)

    async def heartbeat(self, incident_id):
        while True:
            await asyncio.sleep(30)
            def renew(s, events):
                item = s.get("incident")
                if item and item["id"] == incident_id and item["lease_owner"] == self.worker_id:
                    item["lease_until"] = time.time() + 120
            await self.bot.db.transact(renew)

    async def evacuate(self, incident):
        cfg = incident["config"]
        backup = self.bot.get_guild(int(cfg["backup_id"]))
        if backup is None or backup.me is None or not backup.me.guild_permissions.create_instant_invite:
            raise RuleError("Bot absent du secours ou permission Créer une invitation manquante.")
        # Une tâche par worker, pas une tâche par membre. Taille de file bornée.
        queue = asyncio.Queue(maxsize=self.bot.config.evacuation_concurrency * 2)
        result = {"eligible": 0, "succeeded": 0, "failed": 0, "skipped": 0}
        async def worker():
            while True:
                row = await queue.get()
                try:
                    if row is None:
                        return
                    outcome = await self.evacuate_member(row, cfg)
                    result[outcome] += 1
                    if outcome != "skipped":
                        result["eligible"] += 1
                finally:
                    queue.task_done()
        workers = [asyncio.create_task(worker()) for _ in range(self.bot.config.evacuation_concurrency)]
        try:
            async for row in self.bot.db.members(cfg["primary_id"], cfg["backup_id"]):
                # Si la base tombe, les workers peuvent échouer : vérifier avant de bloquer.
                for task in workers:
                    if task.done() and task.exception():
                        raise task.exception()
                await asyncio.wait_for(queue.put(row), timeout=120)
            for _ in workers:
                await asyncio.wait_for(queue.put(None), timeout=120)
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return result

    async def evacuate_member(self, row, cfg):
        async with self.bot.oauth.member_locks[row["user_id"]]:
            row = await self.bot.db.member(row["source_id"], row["destination_id"], row["user_id"]) or row
            if row["revoked"]:
                return "skipped"
            if row["joined"]:
                return "succeeded"
            source = self.bot.get_guild(int(cfg["primary_id"]))
            if source and not source.unavailable:
                try:
                    await source.fetch_member(int(row["user_id"]))
                    row["is_active"] = True
                except discord.NotFound:
                    row["is_active"] = False
                    await self.bot.db.save_member(row)
                    return "skipped"
                except discord.HTTPException:
                    # État inconnu pendant un incident : conserver le dernier état connu.
                    pass
            if not row["is_active"]:
                return "skipped"
            try:
                row = await self.bot.oauth.fresh_member(self.bot.db, row)
                try:
                    await self.bot.oauth.add_member_to_guild(row["user_id"], self.bot.db.decrypt(row["access_token"]), cfg["backup_id"])
                except OAuthError as exc:
                    # Code Discord 50025 = invalid OAuth2 access token. Ne pas rafraîchir
                    # en réponse à un token BOT invalide ou à un manque de permissions.
                    if exc.code != "50025":
                        raise
                    row = await self.bot.oauth.fresh_member(self.bot.db, row, force=True)
                    await self.bot.oauth.add_member_to_guild(row["user_id"], self.bot.db.decrypt(row["access_token"]), cfg["backup_id"])
                row.update(joined=True, last_error=None)
                await self.bot.db.save_member(row)
                return "succeeded"
            except RevokedToken:
                return "failed"
            except Exception as exc:
                row["last_error"] = type(exc).__name__
                await self.bot.db.save_member(row)
                return "failed"
