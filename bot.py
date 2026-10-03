"""Sentinelle : une communauté et son secours, configurés dans Discord."""
import asyncio
import logging
import time
from urllib.parse import urlencode

import discord
from discord.ext import commands

from config import get_config
from crisis import CrisisCommunicator, writable_channels
from database import Database
from detection import DetectionEngine
from evacuation import EvacuationWorker
from governance import Governance, RuleError, require_council
from maintenance import MaintenanceJob
from oauth2 import OAuth2Manager
from oauth_server import OAuthServer
from rate_limiter import RateLimiter

log = logging.getLogger("sentinelle")


class Sentinelle(commands.Bot):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.moderation = True
        super().__init__(command_prefix="!", intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.db = Database(config)
        self.governance = Governance(self.db)
        self.detection = DetectionEngine(self)
        self.crisis = CrisisCommunicator(self)
        self.evacuation = EvacuationWorker(self)
        self.maintenance = MaintenanceJob(self)
        self.oauth = None
        self.web = OAuthServer(self)
        self.jobs = set()
        self.last_poll = self.last_roles = self.last_maintenance = 0

    async def setup_hook(self):
        await self.db.state()  # Échouer clairement si la migration SQL n'a pas été appliquée.
        application = await self.application_info()
        self.oauth = OAuth2Manager(self.config, application.id, RateLimiter(self.config.rate_limit_delay))
        await self.web.start()
        self.spawn(self.supervise())

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        def finished(done):
            self.jobs.discard(done)
            if not done.cancelled() and done.exception():
                log.error("Tâche échouée : %s", type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def supervise(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.governance.tick()
                state = await self.db.state()
                if state["phase"] == "active":
                    cfg = state["config"]
                    if self.get_guild(int(cfg["primary_id"])) is None:
                        # Après READY le cache des guildes est constitué. Vérifier REST
                        # avant de considérer une absence comme exclusion.
                        try:
                            await self.fetch_guild(int(cfg["primary_id"]))
                        except (discord.NotFound, discord.Forbidden):
                            await self.governance.missing_bot(cfg["primary_id"], self.user.id)
                self.spawn_if_idle("migration_task", self.evacuation.run)
                now = time.monotonic()
                if now - self.last_poll >= 15:
                    self.last_poll = now
                    self.spawn_if_idle("audit_task", self.detection.poll)
                if now - self.last_roles >= 60:
                    self.last_roles = now
                    self.spawn_if_idle("roles_task", self.detection.reconcile_roles)
                if now - self.last_maintenance >= 3600:
                    self.last_maintenance = now
                    self.spawn_if_idle("maintenance_task", self.maintenance.run)
                self.spawn_if_idle("journal_task", self.crisis.flush)
            except Exception:
                log.exception("Supervision momentanément indisponible ; nouvel essai dans 5 secondes.")
            await asyncio.sleep(5)

    def spawn_if_idle(self, name, factory):
        task = getattr(self, name, None)
        if task is None or task.done():
            setattr(self, name, self.spawn(factory()))

    async def on_ready(self):
        log.info("Sentinelle connectée (%s). Configuration via !configurer.", self.user.id)

    async def on_guild_join(self, guild):
        channels = writable_channels(guild)
        if channels:
            await channels[0].send("Sentinelle est présente. Elle reste inactive tant que la configuration n'est pas validée. Propriétaire : consultez `!aide`.")

    async def on_audit_log_entry_create(self, entry):
        await self.detection.entry(entry)
        self.spawn_if_idle("migration_task", self.evacuation.run)

    async def on_member_remove(self, member):
        state = await self.db.state()
        cfg = state.get("config")
        if cfg and str(member.guild.id) == cfg["primary_id"] and str(member.id) == cfg["guardian_id"]:
            # Le journal peut arriver après l'événement ; laisser le polling compléter l'auteur.
            await self.governance.missing_bot(member.guild.id, member.id)
            self.spawn_if_idle("migration_task", self.evacuation.run)
        await self.membership(member, False)

    async def on_member_join(self, member):
        await self.membership(member, True)
        await self.detection.reconcile_roles(member)

    async def membership(self, member, active):
        state = await self.db.state()
        cfg = state.get("config")
        if not cfg or str(member.guild.id) != cfg["primary_id"]:
            return
        async with self.oauth.member_locks[str(member.id)]:
            row = await self.db.member(cfg["primary_id"], cfg["backup_id"], str(member.id))
            if row:
                row["is_active"] = active
                await self.db.save_member(row)

    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            await self.detection.reconcile_roles(after)

    async def on_guild_remove(self, guild):
        await self.governance.missing_bot(guild.id, self.user.id)
        self.spawn_if_idle("migration_task", self.evacuation.run)

    async def on_guild_update(self, before, after):
        await self.detection.ownership_changed(before, after)

    async def close(self):
        for task in list(self.jobs):
            task.cancel()
        await asyncio.gather(*list(self.jobs), return_exceptions=True)
        await self.web.close()
        if self.oauth:
            await self.oauth.close()
        await self.db.close()
        await super().close()


async def validate_discord_config(bot, cfg):
    Governance.validate_config(cfg)
    guild = bot.get_guild(int(cfg["primary_id"]))
    backup = bot.get_guild(int(cfg["backup_id"]))
    if not guild or not backup or guild.unavailable or backup.unavailable:
        raise RuleError("Sentinelle doit être présente sur les deux serveurs accessibles.")
    primary_info, backup_info = await asyncio.gather(bot.fetch_guild(guild.id), bot.fetch_guild(backup.id))
    cfg["owner_id"], cfg["backup_owner_id"] = str(primary_info.owner_id), str(backup_info.owner_id)
    me = await guild.fetch_member(bot.user.id)
    backup_me = await backup.fetch_member(bot.user.id)
    if not me.guild_permissions.view_audit_log or not me.guild_permissions.manage_roles:
        raise RuleError("Sentinelle doit pouvoir Voir les logs du serveur et Gérer les rôles.")
    if not backup_me.guild_permissions.create_instant_invite or not backup_me.guild_permissions.view_audit_log:
        raise RuleError("Sur le secours : Créer une invitation et Voir les logs du serveur sont nécessaires.")
    roles = await guild.fetch_roles()
    role = next((r for r in roles if str(r.id) == cfg["council_role_id"]), None)
    if role is None or role.is_default() or role.managed or role >= me.top_role:
        raise RuleError("Choisir un rôle ordinaire situé sous le rôle le plus haut de Sentinelle.")
    guardian = await guild.fetch_member(int(cfg["guardian_id"]))
    if not guardian.bot:
        raise RuleError("Le gardien doit être un bot présent sur le principal.")
    for user_id in cfg["council_ids"]:
        member = await guild.fetch_member(int(user_id))
        if member.bot:
            raise RuleError("Les cinq conseillers doivent être des personnes.")
        if member.id != guild.owner_id and member.top_role >= me.top_role:
            raise RuleError("Sentinelle doit être au-dessus des conseillers dans la hiérarchie pour corriger leurs rôles.")
    for target, field in ((guild, "alert_channel_id"), (backup, "backup_channel_id")):
        preferred = cfg.get(field)
        channels = writable_channels(target, preferred)
        if not channels:
            raise RuleError(f"Aucun salon accessible en écriture sur {target.id}.")
        if preferred and str(channels[0].id) != preferred:
            raise RuleError(f"Le salon {preferred} n'est pas un salon textuel accessible sur {target.id}.")
        cfg[field] = str(channels[0].id)
    return cfg


def create_bot(config):
    bot = Sentinelle(config)

    @bot.check
    async def guild_only(ctx):
        if not ctx.guild:
            raise RuleError("Utilisez les commandes dans le serveur, pas en message privé.")
        return True

    @bot.command(name="configurer")
    async def configure(ctx, secours: int, gardien: int, role: int, conseillers: str,
                        salon: int = 0, salon_secours: int = 0):
        """secours_id gardien_id role_id id1,id2,id3,id4,id5 [salon_id] [salon_secours_id]"""
        state = await bot.db.state()
        if state["phase"] in {"unconfigured", "awaiting_configuration"}:
            actual = await bot.fetch_guild(ctx.guild.id)
            if ctx.author.id != actual.owner_id:
                raise RuleError("Seul le propriétaire actuel peut initialiser ce serveur.")
            if state["expected_guild_id"] and str(ctx.guild.id) != state["expected_guild_id"]:
                raise RuleError("Configurer depuis le serveur de secours devenu destination.")
        else:
            require_council(state, ctx.author.id, ctx.guild.id)
            if str(ctx.guild.id) != state["config"]["primary_id"]:
                raise RuleError("Proposer la configuration depuis le principal.")
        ids = [str(int(value.strip())) for value in conseillers.split(",")]
        cfg = await validate_discord_config(bot, {
            "primary_id": str(ctx.guild.id), "backup_id": str(secours), "guardian_id": str(gardien),
            "sentinel_id": str(bot.user.id), "council_role_id": str(role), "council_ids": ids,
            "alert_channel_id": str(salon) if salon else None,
            "backup_channel_id": str(salon_secours) if salon_secours else None,
        })
        if state["phase"] in {"unconfigured", "awaiting_configuration"}:
            await bot.governance.configure(cfg, ctx.author.id, cfg["owner_id"], state["generation"])
            await ctx.send(f"Surveillance activée. Principal {cfg['primary_id']}, secours {cfg['backup_id']}. Salon {cfg['alert_channel_id']}, secours {cfg['backup_channel_id']}. Utilisez !inscription pour les consentements OAuth2.")
            bot.spawn_if_idle("roles_task", bot.detection.reconcile_roles)
        else:
            rid = await bot.governance.propose("configure", ctx.author.id, ctx.guild.id, cfg)
            await ctx.send(f"Configuration proposée : `{cfg}`\nLes cinq conseillers doivent chacun utiliser `!approuver {rid}`.")

    async def propose(ctx, kind):
        rid = await bot.governance.propose(kind, ctx.author.id, ctx.guild.id)
        await ctx.send(f"Demande `{rid}` ({kind}). Chacun des cinq conseillers, y compris le demandeur, doit utiliser `!approuver {rid}` dans les 10 minutes.")

    @bot.command(name="reset")
    async def reset(ctx):
        await propose(ctx, "reset")

    @bot.command(name="pause")
    async def pause(ctx):
        await propose(ctx, "pause")

    @bot.command(name="reactiver")
    async def resume(ctx):
        await propose(ctx, "resume")

    @bot.command(name="finaliser")
    async def finalize(ctx):
        await propose(ctx, "finalize")

    @bot.command(name="approuver", aliases=["approve"])
    async def approve(ctx, request_id: str):
        state = await bot.db.state()
        require_council(state, ctx.author.id, ctx.guild.id)
        request = state["requests"].get(request_id)
        if request and request["kind"] == "configure":
            # Revalider avant chaque vote ; le payload voté demeure immuable.
            checked = await validate_discord_config(bot, dict(request["payload"]))
            if checked != request["payload"]:
                raise RuleError("La configuration proposée a changé (propriétaire, salons). Recréer la demande.")
        count = await bot.governance.approve(request_id, ctx.author.id, ctx.guild.id)
        await ctx.send(f"Approbation enregistrée : {count}/5." + (" Décision exécutée." if count == 5 else ""))
        if count == 5:
            bot.spawn_if_idle("roles_task", bot.detection.reconcile_roles)

    @bot.command(name="reprendre")
    async def retry(ctx):
        await bot.evacuation.retry(ctx.author.id, ctx.guild.id)
        await ctx.send("Reprise de la migration programmée vers la même destination.")
        bot.spawn_if_idle("migration_task", bot.evacuation.run)

    @bot.command(name="statut")
    async def status(ctx):
        state = await bot.db.state()
        cfg = state.get("config")
        if cfg and str(ctx.guild.id) not in {cfg["primary_id"], cfg["backup_id"]}:
            await ctx.send("Ce serveur n'est pas configuré pour cette communauté.")
            return
        remaining = max(0, int(state["pause_until"] - time.time()))
        await ctx.send(f"État : {state['phase']} · infractions : {state['count']}/3 · pause restante : {remaining}s.\n"
                       + (f"Principal {cfg['primary_id']} (owner {cfg['owner_id']}), secours {cfg['backup_id']} (owner {cfg['backup_owner_id']})." if cfg else "Le propriétaire doit lancer !configurer."))

    @bot.command(name="infractions")
    async def history(ctx):
        state = await bot.db.state()
        require_council(state, ctx.author.id, ctx.guild.id)
        rows = await bot.db.events(limit=10)
        await ctx.send("\n".join(f"{row['created_at']} : {row['payload']['message']}" for row in rows)[:1950] or "Aucun événement.")

    @bot.command(name="inscription")
    async def registration(ctx):
        state = await bot.db.state()
        cfg = state.get("config")
        if state["phase"] != "active" or not cfg or str(ctx.guild.id) != cfg["primary_id"]:
            raise RuleError("L'inscription nécessite un principal configuré et actif.")
        backup = bot.get_guild(int(cfg["backup_id"]))
        url = config.oauth2_redirect_uri.removesuffix("/callback") + "/authorize?" + urlencode({"source": cfg["primary_id"]})
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Autoriser mon ajout au secours", url=url))
        await ctx.send(f"Secours : **{discord.utils.escape_markdown(backup.name) if backup else cfg['backup_id']}** (ID {cfg['backup_id']}).\n"
                       "En autorisant l'application, vous consentez à y être ajouté en cas d'incident. "
                       "L'inscription est volontaire. Pour consulter votre état : !moninscription ; pour retirer votre consentement local : !retirer.", view=view)

    @bot.command(name="moninscription")
    async def my_registration(ctx):
        state = await bot.db.state()
        cfg = state.get("config")
        if not cfg or str(ctx.guild.id) != cfg["primary_id"]:
            raise RuleError("Commande réservée au principal.")
        row = await bot.db.member(cfg["primary_id"], cfg["backup_id"], str(ctx.author.id))
        await ctx.send("Votre autorisation est enregistrée." if row and not row["revoked"] else "Aucune autorisation active. Utilisez !inscription.")

    @bot.command(name="retirer")
    async def revoke(ctx):
        state = await bot.db.state()
        cfg = state.get("config")
        if not cfg or str(ctx.guild.id) not in {cfg["primary_id"], cfg["backup_id"]}:
            raise RuleError("Commande réservée aux serveurs configurés.")
        async with bot.oauth.member_locks[str(ctx.author.id)]:
            row = await bot.db.member(cfg["primary_id"], cfg["backup_id"], str(ctx.author.id))
            if row:
                row.update(revoked=True, access_token="", refresh_token="")
                await bot.db.save_member(row)
        await ctx.send("Autorisation locale retirée pour cette destination. Vous pouvez aussi révoquer l'application dans Discord > Applications autorisées.")

    @bot.command(name="maintenance")
    async def maintenance(ctx):
        require_council(await bot.db.state(), ctx.author.id, ctx.guild.id)
        await ctx.send("Maintenance programmée.")
        bot.spawn_if_idle("maintenance_task", bot.maintenance.run)

    @bot.command(name="aide")
    async def help_command(ctx):
        await ctx.send("**Configuration propriétaire (IDs numériques)**\n"
                       "`!configurer secours_id gardien_id role_id id1,id2,id3,id4,id5 [salon_id] [salon_secours_id]`\n"
                       "**Conseil** : !pause (5 min), !reset, !reactiver, !approuver ID, !infractions, !maintenance. "
                       "Les décisions exigent cinq votes. Après activation, !configurer propose un vote.\n"
                       "**Migration incomplète** : !reprendre ; !finaliser nécessite cinq votes.\n"
                       "**Membres** : !statut, !inscription, !moninscription, !retirer.\n"
                       "Seuil : trois infractions. La disparition d'un bot protégé déclenche directement la migration, même pendant une pause.")

    @bot.event
    async def on_command_error(ctx, error):
        original = getattr(error, "original", error)
        if isinstance(original, commands.CommandNotFound):
            return
        if isinstance(original, (RuleError, ValueError, commands.UserInputError)):
            await ctx.send(str(original)[:1500] + "\nVoir !aide.")
        elif isinstance(original, discord.HTTPException):
            await ctx.send("Discord a refusé l'opération. Vérifiez les IDs, les permissions et la présence des membres/bots.")
        else:
            log.error("Commande échouée : %s", type(original).__name__)
            await ctx.send("Opération indisponible. Consulter les journaux de l'hébergement ; aucun succès n'est présumé.")

    return bot


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = get_config()
    create_bot(config).run(config.sentinelle_token)


if __name__ == "__main__":
    main()
