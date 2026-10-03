"""Régressions hors réseau. Exécution : python -m unittest -v."""
import asyncio
import copy
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from database import Database, initial_state
from governance import Governance, RuleError
from oauth2 import OAuth2Manager, OAuthError, RevokedToken
from rate_limiter import RateLimiter


CFG = {"primary_id": "10", "backup_id": "20", "owner_id": "100", "backup_owner_id": "200",
       "guardian_id": "800", "sentinel_id": "900", "council_role_id": "300",
       "council_ids": ["1", "2", "3", "4", "5"], "alert_channel_id": "11", "backup_channel_id": "21"}


class MemoryDB:
    """Même interface transactionnelle ; tests des règles réelles, pas réécriture des règles."""
    def __init__(self):
        self.document = initial_state()
        self.journal, self.keys, self.rows = [], set(), {}
        self.lock = asyncio.Lock()

    async def state(self):
        return copy.deepcopy(self.document)

    async def transact(self, change, key=None):
        async with self.lock:
            if key and key in self.keys:
                return None
            state, events = copy.deepcopy(self.document), []
            result = change(state, events)
            self.document = copy.deepcopy(state)
            self.journal.extend(copy.deepcopy(events))
            if key:
                self.keys.add(key)
            return copy.deepcopy(result)

    async def member(self, source, destination, user):
        return copy.deepcopy(self.rows.get((source, destination, user)))

    async def save_member(self, row):
        self.rows[(row["source_id"], row["destination_id"], row["user_id"])] = copy.deepcopy(row)

    async def members(self, source, destination):
        for row in list(self.rows.values()):
            if row["source_id"] == source and row["destination_id"] == destination:
                yield copy.deepcopy(row)

    def encrypt(self, value):
        return "encrypted:" + value

    def decrypt(self, value):
        return value.removeprefix("encrypted:")


def settings():
    return SimpleNamespace(sentinelle_token="BOT_SECRET", oauth2_client_secret="APP_SECRET",
        oauth2_redirect_uri="http://localhost/callback", http_host="127.0.0.1", http_port=8080,
        rate_limit_delay=0.1, evacuation_concurrency=2,
        supabase_url="https://invalid.example", supabase_service_role_key="SERVICE_SECRET",
        token_encryption_key=Fernet.generate_key().decode())


def member_row(user="1"):
    return {"source_id": "10", "destination_id": "20", "user_id": user,
            "access_token": "encrypted:access", "refresh_token": "encrypted:refresh",
            "expires_at": time.time() + 7200, "is_active": True, "revoked": False,
            "joined": False, "last_error": None}


class GovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MemoryDB()
        self.now = 1000.0
        self.rules = Governance(self.db, lambda: self.now)
        await self.rules.configure(CFG, "100", "100", 0)
        self.now += 1

    async def vote(self, kind, payload=None):
        rid = await self.rules.propose(kind, "1", "10", payload)
        for user in CFG["council_ids"]:
            await self.rules.approve(rid, user, "10")
        return rid

    async def action(self, event="123", actor="100", action="channel_delete", target="99", at=None, guild="10"):
        await self.rules.audit(guild, event, actor, "100", action, target, self.now if at is None else at)

    async def test_unconfigured_is_dormant(self):
        db = MemoryDB()
        rules = Governance(db)
        await rules.audit("10", "1", "100", "100", "ban", "800", time.time())
        await rules.missing_bot("10", "900")
        self.assertEqual(db.document["phase"], "unconfigured")
        self.assertFalse(db.journal)

    async def test_only_owner_initializes(self):
        rules = Governance(MemoryDB())
        with self.assertRaises(RuleError):
            await rules.configure(CFG, "1", "100", 0)

    async def test_five_distinct_councillors_required(self):
        for ids in (["1"] * 5, ["1", "2", "3", "4"], [str(i) for i in range(6)]):
            with self.assertRaises(RuleError):
                Governance.validate_config({**CFG, "council_ids": ids})

    async def test_owner_cannot_reconfigure_alone(self):
        with self.assertRaises(RuleError):
            await self.rules.configure(CFG, "100", "100", 1)

    async def test_real_discord_enum_is_recognized(self):
        await self.action(action=discord.AuditLogAction.member_role_update.name)
        self.assertEqual(self.db.document["count"], 1)

    async def test_others_and_other_guild_do_not_count(self):
        await self.action(actor="4")
        await self.action(event="124", actor="900")
        await self.action(event="125", guild="30")
        self.assertEqual(self.db.document["count"], 0)

    async def test_event_is_deduplicated_concurrently(self):
        await asyncio.gather(self.action(), self.action(), self.action())
        self.assertEqual(self.db.document["count"], 1)
        self.assertEqual(len([e for e in self.db.journal if e["kind"] == "owner_action"]), 1)

    async def test_three_actions_trigger_single_migration(self):
        for i in range(5):
            await self.action(event=str(i))
        self.assertEqual(self.db.document["count"], 3)
        self.assertEqual(self.db.document["phase"], "migrating")
        self.assertEqual(len([e for e in self.db.journal if e["kind"] == "migration"]), 1)

    async def test_kick_bypasses_counter_and_deduplicates_two_bots(self):
        await self.action(action="kick", target="800")
        await self.action(event="124", action="kick", target="900")
        await self.rules.missing_bot("10", "900")
        self.assertEqual(self.db.document["count"], 0)
        self.assertEqual(self.db.document["phase"], "migrating")
        self.assertEqual(len([e for e in self.db.journal if e["kind"] == "migration"]), 1)

    async def test_kick_by_other_actor_still_protects_bots(self):
        await self.action(actor="4", action="kick", target="800")
        self.assertEqual(self.db.document["phase"], "migrating")

    async def test_vote_needs_all_five_not_roles_or_duplicate_votes(self):
        rid = await self.rules.propose("pause", "1", "10")
        with self.assertRaises(RuleError):
            await self.rules.approve(rid, "6", "10")
        for user in ["1", "2", "3", "4"]:
            await self.rules.approve(rid, user, "10")
        self.assertEqual(self.db.document["pause_until"], 0)
        with self.assertRaises(RuleError):
            await self.rules.approve(rid, "1", "10")
        await self.rules.approve(rid, "5", "20")
        self.assertEqual(self.db.document["pause_until"], self.now + 300)

    async def test_pause_logs_actions_without_counting(self):
        await self.vote("pause")
        await self.action()
        event = self.db.journal[-1]
        self.assertEqual(event["kind"], "owner_action")
        self.assertTrue(event["details"]["paused"])
        self.assertFalse(event["details"]["counted"])

    async def test_pause_survives_restart_and_expires_once(self):
        await self.vote("pause")
        self.rules = Governance(self.db, lambda: self.now)
        self.now += 301
        await self.rules.tick()
        await self.rules.tick()
        await self.action()
        self.assertEqual(self.db.document["count"], 1)
        self.assertEqual(len([e for e in self.db.journal if e["kind"] == "resume"]), 1)

    async def test_delayed_pause_event_is_not_counted_after_resume(self):
        await self.vote("pause")
        original_time = self.now + 1
        self.now += 400
        await self.rules.tick()
        await self.action(at=original_time)
        self.assertEqual(self.db.document["count"], 0)

    async def test_expulsion_during_pause_still_migrates(self):
        await self.vote("pause")
        await self.action(action="ban", target="800")
        self.assertEqual(self.db.document["phase"], "migrating")

    async def test_reset_cannot_be_replayed_and_history_remains(self):
        await self.action()
        rid = await self.vote("reset")
        self.now += 1
        await self.action(event="124")
        with self.assertRaises(RuleError):
            await self.rules.approve(rid, "1", "10")
        self.assertEqual(self.db.document["count"], 1)
        self.assertTrue(any(e["kind"] == "reset" for e in self.db.journal))

    async def test_expired_vote_cannot_execute(self):
        rid = await self.rules.propose("reset", "1", "10")
        self.now += 601
        with self.assertRaises(RuleError):
            await self.rules.approve(rid, "2", "10")

    async def test_config_change_requires_original_five_and_invalidates_old_votes(self):
        old_vote = await self.rules.propose("reset", "1", "10")
        new = {**CFG, "council_ids": ["1", "2", "3", "4", "6"]}
        await self.vote("configure", new)
        with self.assertRaises(RuleError):
            await self.rules.approve(old_vote, "1", "10")
        with self.assertRaises(RuleError):
            await self.rules.propose("reset", "5", "10")

    async def test_migration_configuration_restricted_to_destination_owner(self):
        await self.rules.missing_bot("10", "900")
        self.db.document["incident"]["status"] = "empty"
        await self.vote("finalize")
        new = {**CFG, "primary_id": "20", "backup_id": "30", "owner_id": "200"}
        with self.assertRaises(RuleError):
            await self.rules.configure(new, "100", "200", 1)
        await self.rules.configure(new, "200", "200", 1)
        self.assertEqual(self.db.document["phase"], "active")
        self.assertEqual(self.db.document["config"]["primary_id"], "20")
        self.assertTrue(any(e["kind"] == "migration" for e in self.db.journal))


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.oauth = OAuth2Manager(settings(), "900", RateLimiter(0))

    async def asyncTearDown(self):
        await self.oauth.close()

    async def test_add_member_uses_bot_auth_and_user_access_token(self):
        self.oauth.request = AsyncMock(return_value=(201, {}))
        self.assertTrue(await self.oauth.add_member_to_guild("1", "USER_ACCESS", "20"))
        args = self.oauth.request.call_args
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bot BOT_SECRET")
        self.assertEqual(args.kwargs["body"], {"access_token": "USER_ACCESS"})

    async def test_transient_refresh_does_not_revoke(self):
        db = MemoryDB()
        row = member_row()
        row["expires_at"] = 0
        await db.save_member(row)
        self.oauth.refresh_token = AsyncMock(side_effect=OAuthError(503))
        with self.assertRaises(OAuthError):
            await self.oauth.fresh_member(db, row)
        self.assertFalse((await db.member("10", "20", "1"))["revoked"])

    async def test_invalid_grant_really_revokes(self):
        db = MemoryDB()
        row = member_row()
        row["expires_at"] = 0
        await db.save_member(row)
        self.oauth.refresh_token = AsyncMock(side_effect=RevokedToken(400, "invalid_grant"))
        with self.assertRaises(RevokedToken):
            await self.oauth.fresh_member(db, row)
        self.assertTrue((await db.member("10", "20", "1"))["revoked"])

    async def test_tokens_are_encrypted(self):
        db = Database(settings())
        value = db.encrypt("my-secret")
        self.assertNotIn("my-secret", value)
        self.assertEqual(db.decrypt(value), "my-secret")

    async def test_http_retries_are_bounded_and_global_429_used(self):
        response = MagicMock(status=429)
        response.json = AsyncMock(return_value={"retry_after": 0})
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        self.oauth.session = MagicMock()
        self.oauth.session.request.return_value = ctx
        self.oauth.session.close = AsyncMock()
        with self.assertRaises(OAuthError):
            await self.oauth.request("GET", "/example")
        self.assertEqual(self.oauth.session.request.call_count, 4)


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from oauth_server import OAuthServer
        self.db = MemoryDB()
        await Governance(self.db).configure(CFG, "100", "100", 0)
        self.guild = SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False)))
        self.oauth = OAuth2Manager(settings(), "900", RateLimiter(0))
        self.oauth.exchange_code = AsyncMock(return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "identify guilds.join"})
        self.oauth.identity = AsyncMock(return_value={"id": "1"})
        self.bot = SimpleNamespace(db=self.db, config=settings(), oauth=self.oauth, get_guild=lambda _: self.guild)
        self.server = OAuthServer(self.bot)
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.oauth.close()

    async def authorize(self):
        response = await self.client.get("/authorize?source=10", allow_redirects=False)
        self.assertEqual(response.status, 302)
        return next(iter(self.server.pending))

    async def test_cookie_state_is_required(self):
        await self.authorize()
        response = await self.client.get("/callback?state=wrong&code=code")
        self.assertEqual(response.status, 400)
        self.oauth.exchange_code.assert_not_awaited()

    async def test_callback_stores_scoped_consent_and_rejects_replay(self):
        nonce = await self.authorize()
        response = await self.client.get(f"/callback?state={nonce}&code=code")
        self.assertEqual(response.status, 200)
        row = await self.db.member("10", "20", "1")
        self.assertEqual(row["access_token"], "encrypted:a")
        self.assertIsNone(await self.db.member("10", "30", "1"))
        replay = await self.client.get(f"/callback?state={nonce}&code=code")
        self.assertEqual(replay.status, 400)

    async def test_changed_configuration_invalidates_authorization(self):
        nonce = await self.authorize()
        self.db.document["generation"] += 1
        response = await self.client.get(f"/callback?state={nonce}&code=code")
        self.assertEqual(response.status, 409)
        self.oauth.exchange_code.assert_not_awaited()

    async def test_missing_scopes_are_rejected(self):
        nonce = await self.authorize()
        self.oauth.exchange_code.return_value["scope"] = "identify"
        response = await self.client.get(f"/callback?state={nonce}&code=code")
        self.assertEqual(response.status, 400)
        self.assertFalse(self.db.rows)


class EvacuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from evacuation import EvacuationWorker
        self.db = MemoryDB()
        await Governance(self.db).configure(CFG, "100", "100", 0)
        self.oauth = OAuth2Manager(settings(), "900", RateLimiter(0))
        self.oauth.add_member_to_guild = AsyncMock(return_value=True)
        self.backup = SimpleNamespace(me=SimpleNamespace(guild_permissions=SimpleNamespace(create_instant_invite=True)))
        self.bot = SimpleNamespace(db=self.db, oauth=self.oauth, config=settings(), get_guild=lambda gid: self.backup if gid == 20 else None)
        self.worker = EvacuationWorker(self.bot)
        await Governance(self.db).missing_bot("10", "900")

    async def asyncTearDown(self):
        await self.oauth.close()

    async def test_success_awaits_new_configuration_and_no_second_run(self):
        await self.db.save_member(member_row())
        await self.worker.run()
        self.assertEqual(self.db.document["phase"], "awaiting_configuration")
        self.assertEqual(self.db.document["expected_guild_id"], "20")
        await self.worker.run()
        self.oauth.add_member_to_guild.assert_awaited_once()

    async def test_partial_results_retry_only_failed_members(self):
        await self.db.save_member(member_row("1"))
        await self.db.save_member(member_row("2"))
        async def add(user, access, guild):
            if user == "2":
                raise OAuthError(403)
            return True
        self.oauth.add_member_to_guild.side_effect = add
        await self.worker.run()
        self.assertEqual(self.db.document["incident"]["status"], "partial")
        self.assertEqual(self.db.document["phase"], "migrating")
        self.oauth.add_member_to_guild.reset_mock(side_effect=True)
        self.oauth.add_member_to_guild.return_value = True
        await self.worker.retry("1", "20")
        await self.worker.run()
        self.oauth.add_member_to_guild.assert_awaited_once()
        self.assertEqual(self.oauth.add_member_to_guild.call_args.args[0], "2")

    async def test_empty_migration_not_reported_as_success(self):
        await self.worker.run()
        self.assertEqual(self.db.document["incident"]["status"], "empty")
        self.assertEqual(self.db.document["phase"], "migrating")

    async def test_live_lease_prevents_second_worker(self):
        self.db.document["incident"].update(status="running", lease_owner="other", lease_until=time.time() + 100)
        await self.worker.run()
        self.oauth.add_member_to_guild.assert_not_awaited()

    async def test_expired_lease_recovers_after_restart(self):
        await self.db.save_member(member_row())
        self.db.document["incident"].update(status="running", lease_owner="old", lease_until=0)
        await self.worker.run()
        self.assertEqual(self.db.document["incident"]["status"], "completed")


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_compare_and_swap_retries_conflicts(self):
        db = Database(settings())
        db.read = AsyncMock(return_value={"version": 0, "document": initial_state()})
        db.request = AsyncMock(side_effect=["conflict", "ok"])
        def change(s, events):
            s["count"] += 1
            return s["count"]
        self.assertEqual(await db.transact(change), 1)
        self.assertEqual(db.request.await_count, 2)

    async def test_pagination_does_not_stop_at_1000(self):
        db = Database(settings())
        db.request = AsyncMock(side_effect=[[{"user_id": str(i)} for i in range(500)],
                                          [{"user_id": str(i)} for i in range(500, 1000)],
                                          [{"user_id": "1000"}]])
        rows = [row async for row in db.members("10", "20")]
        self.assertEqual(len(rows), 1001)
        self.assertEqual(db.request.call_args.kwargs["params"]["offset"], "1000")


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_registers_commands_without_connecting(self):
        from bot import create_bot
        bot = create_bot(settings())
        for name in ("configurer", "pause", "reset", "approuver", "inscription", "retirer", "reprendre", "finaliser"):
            self.assertIsNotNone(bot.get_command(name))
        self.assertTrue(bot.intents.members)
        self.assertTrue(bot.intents.message_content)
        await bot.close()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from detection import DetectionEngine
        self.db = MemoryDB()
        self.now = time.time()
        self.rules = Governance(self.db, lambda: self.now)
        await self.rules.configure(CFG, "100", "100", 0)
        self.role = object()
        self.guild = SimpleNamespace(id=10, owner_id=100, get_role=lambda _: self.role)
        self.bot = SimpleNamespace(db=self.db, governance=self.rules, get_guild=lambda _: self.guild)
        self.detector = DetectionEngine(self.bot)

    async def test_sixth_role_holder_is_removed_without_gaining_vote(self):
        member = SimpleNamespace(id=6, guild=self.guild, roles=[self.role],
                                 add_roles=AsyncMock(), remove_roles=AsyncMock())
        await self.detector.reconcile_roles(member)
        member.remove_roles.assert_awaited_once()
        with self.assertRaises(RuleError):
            await self.rules.propose("reset", "6", "10")

    async def test_councillor_losing_role_is_restored_and_can_still_vote(self):
        member = SimpleNamespace(id=1, guild=self.guild, roles=[],
                                 add_roles=AsyncMock(), remove_roles=AsyncMock())
        await self.detector.reconcile_roles(member)
        member.add_roles.assert_awaited_once()
        self.assertTrue(await self.rules.propose("pause", "1", "10"))

    async def test_role_repair_is_not_applied_on_backup(self):
        member = SimpleNamespace(id=1, guild=SimpleNamespace(id=20), roles=[],
                                 add_roles=AsyncMock(), remove_roles=AsyncMock())
        await self.detector.reconcile_roles(member)
        member.add_roles.assert_not_awaited()

    async def test_actual_audit_adapter_counts_role_change_only_once(self):
        entry = SimpleNamespace(guild=self.guild, id=111, user_id=100,
            action=discord.AuditLogAction.member_role_update, target=SimpleNamespace(id=1),
            before=[], after=[], created_at=datetime.fromtimestamp(self.now + 1, timezone.utc))
        await self.detector.entry(entry)
        await self.detector.entry(entry)
        self.assertEqual(self.db.document["count"], 1)

    async def test_owner_history_attributes_delayed_action_to_previous_owner(self):
        await self.detector.observe_owner(10, 200, self.now + 10)
        self.guild.owner_id = 200
        entry = SimpleNamespace(guild=self.guild, id=112, user_id=100,
            action=discord.AuditLogAction.channel_delete, target=SimpleNamespace(id=30),
            before=[], after=[], created_at=datetime.fromtimestamp(self.now + 1, timezone.utc))
        await self.detector.entry(entry)
        self.assertEqual(self.db.document["count"], 1)
        entry.id = 113
        entry.created_at = datetime.fromtimestamp(self.now + 20, timezone.utc)
        await self.detector.entry(entry)
        self.assertEqual(self.db.document["count"], 1)


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_member_fetch_never_deactivates_everyone(self):
        from maintenance import MaintenanceJob
        db = MemoryDB()
        await Governance(db).configure(CFG, "100", "100", 0)
        await db.save_member(member_row())
        async def broken_fetch(**kwargs):
            yield SimpleNamespace(id=2)
            raise RuntimeError("network interruption")
        guild = SimpleNamespace(unavailable=False, fetch_members=broken_fetch)
        job = MaintenanceJob(SimpleNamespace(db=db, get_guild=lambda _: guild))
        with self.assertRaises(RuntimeError):
            await job.run()
        self.assertTrue((await db.member("10", "20", "1"))["is_active"])

    async def test_returned_member_is_reactivated(self):
        from maintenance import MaintenanceJob
        db = MemoryDB()
        await Governance(db).configure(CFG, "100", "100", 0)
        row = member_row()
        row["is_active"] = False
        await db.save_member(row)
        async def fetch(**kwargs):
            yield SimpleNamespace(id=1)
        guild = SimpleNamespace(unavailable=False, fetch_members=fetch)
        oauth = OAuth2Manager(settings(), "900", RateLimiter(0))
        try:
            await MaintenanceJob(SimpleNamespace(db=db, oauth=oauth, get_guild=lambda _: guild)).run()
            self.assertTrue((await db.member("10", "20", "1"))["is_active"])
        finally:
            await oauth.close()


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_configured_channel_falls_back_and_marks_delivery(self):
        from crisis import CrisisCommunicator
        first = MagicMock(spec=discord.TextChannel)
        first.id, first.position = 11, 0
        first.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
        first.send = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied"))
        second = MagicMock(spec=discord.TextChannel)
        second.id, second.position = 12, 1
        second.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
        second.send = AsyncMock()
        guild = SimpleNamespace(me=object(), get_channel=lambda _: first, system_channel=second, text_channels=[first, second])
        event = {"id": "uuid", "payload": {"config": CFG, "kind": "reset", "message": "reset"}}
        db = SimpleNamespace(events=AsyncMock(return_value=[event]), delivered=AsyncMock())
        await CrisisCommunicator(SimpleNamespace(db=db, get_guild=lambda gid: guild if gid == 10 else None)).flush()
        first.send.assert_awaited_once()
        second.send.assert_awaited_once()
        db.delivered.assert_awaited_once()

    async def test_no_channel_keeps_event_pending(self):
        from crisis import CrisisCommunicator
        event = {"id": "uuid", "payload": {"config": CFG, "kind": "reset", "message": "reset"}}
        db = SimpleNamespace(events=AsyncMock(return_value=[event]), delivered=AsyncMock())
        with self.assertLogs("crisis", level="ERROR"):
            await CrisisCommunicator(SimpleNamespace(db=db, get_guild=lambda _: None)).flush()
        db.delivered.assert_not_awaited()


class ConfigTests(unittest.TestCase):
    def test_config_does_not_require_static_discord_ids(self):
        from config import Config
        env = {"SENTINELLE_TOKEN": "token", "OAUTH2_CLIENT_SECRET": "secret",
               "OAUTH2_REDIRECT_URI": "https://example.org/callback", "SUPABASE_URL": "https://db.example",
               "SUPABASE_SERVICE_ROLE_KEY": "service", "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}
        with patch.dict("os.environ", env, clear=True), patch("config.load_dotenv"):
            config = Config.load()
        self.assertEqual(config.evacuation_concurrency, 3)

    def test_public_http_callback_is_rejected(self):
        from config import Config
        env = {"SENTINELLE_TOKEN": "token", "OAUTH2_CLIENT_SECRET": "secret",
               "OAUTH2_REDIRECT_URI": "http://example.org/callback", "SUPABASE_URL": "https://db.example",
               "SUPABASE_SERVICE_ROLE_KEY": "service", "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}
        with patch.dict("os.environ", env, clear=True), patch("config.load_dotenv"), self.assertRaises(ValueError):
            Config.load()


if __name__ == "__main__":
    unittest.main(verbosity=2)
