"""Règles métier sans dépendance Discord : votes, pauses, détection, migration."""
import copy
import time
import uuid

THRESHOLD = 3
PAUSE_SECONDS = 300
VOTE_SECONDS = 600


class RuleError(ValueError):
    pass


def emit(state, events, kind, message, **details):
    events.append({"kind": kind, "message": message, "details": details,
                   "config": copy.deepcopy(state.get("config")), "at": time.time()})


def require_council(state, user, guild):
    cfg = state.get("config")
    if not cfg or str(user) not in cfg["council_ids"]:
        raise RuleError("Réservé aux cinq conseillers enregistrés, indépendamment de leur rôle actuel.")
    if str(guild) not in {cfg["primary_id"], cfg["backup_id"]}:
        raise RuleError("Commande réservée aux serveurs de cette communauté.")


def begin_migration(state, events, reason, now):
    if state["phase"] != "active" or state.get("incident"):
        return
    state["phase"] = "migrating"
    state["requests"] = {}
    if state["pause_until"] > now:
        state["pauses"][-1][1] = now
        state["pause_until"] = 0
        emit(state, events, "pause_ended", "Pause interrompue par l'incident ; migration en cours, journalisation maintenue.")
    state["incident"] = {"id": str(uuid.uuid4()), "reason": reason, "at": now,
                         "config": copy.deepcopy(state["config"]), "status": "pending",
                         "lease_owner": None, "lease_until": 0, "result": None}
    emit(state, events, "migration", "Migration déclenchée : " + reason)


class Governance:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    async def configure(self, cfg, actor, owner, expected_generation):
        now = self.clock()
        def change(s, events):
            if s["generation"] != expected_generation:
                raise RuleError("La configuration a changé ; recommencer.")
            if s["phase"] not in {"unconfigured", "awaiting_configuration"}:
                raise RuleError("Configuration déjà active. Utiliser une proposition unanime.")
            if str(actor) != str(owner):
                raise RuleError("Seul le propriétaire actuel peut effectuer la configuration initiale.")
            if s["expected_guild_id"] and cfg["primary_id"] != s["expected_guild_id"]:
                raise RuleError("Configurer le serveur de destination de la migration.")
            self.validate_config(cfg)
            cfg_copy = copy.deepcopy(cfg)
            cfg_copy["activated_at"] = now
            cfg_copy["owner_history"] = {
                cfg_copy["primary_id"]: [[now, cfg_copy["owner_id"]]],
                cfg_copy["backup_id"]: [[now, cfg_copy["backup_owner_id"]]],
            }
            s.update(phase="active", config=cfg_copy, count=0, count_since=now,
                     pauses=[], pause_until=0, requests={}, incident=None,
                     expected_guild_id=None, generation=s["generation"] + 1, cursors={})
            emit(s, events, "configuration", "Configuration validée par le propriétaire ; surveillance activée.", actor=str(actor))
        return await self.db.transact(change)

    @staticmethod
    def validate_config(cfg):
        if len(cfg["council_ids"]) != 5 or len(set(cfg["council_ids"])) != 5:
            raise RuleError("Il faut exactement cinq personnes distinctes.")
        if cfg["primary_id"] == cfg["backup_id"]:
            raise RuleError("Le secours doit être différent du principal.")
        if cfg["guardian_id"] == cfg["sentinel_id"]:
            raise RuleError("Le gardien doit être un autre bot.")

    async def propose(self, kind, user, guild, payload=None):
        now = self.clock()
        rid = uuid.uuid4().hex[:12]
        def change(s, events):
            require_council(s, user, guild)
            if kind not in {"reset", "pause", "resume", "configure", "finalize"}:
                raise RuleError("Type de vote inconnu.")
            if kind == "finalize":
                if s["phase"] != "migrating" or s["incident"]["status"] not in {"partial", "failed", "empty"}:
                    raise RuleError("Finalisation disponible après un bilan incomplet.")
            elif s["phase"] != "active":
                raise RuleError("Cette commande exige une surveillance configurée et hors migration.")
            if kind == "pause" and s["pause_until"] > now:
                raise RuleError("Une pause est déjà en cours ; aucune prolongation implicite.")
            if kind == "configure":
                self.validate_config(payload)
                if payload["primary_id"] != s["config"]["primary_id"]:
                    raise RuleError("Le principal ne peut changer que par migration.")
            s["requests"] = {k: v for k, v in s["requests"].items()
                             if v["status"] == "pending" and v["expires_at"] > now}
            if len(s["requests"]) >= 20:
                raise RuleError("Trop de demandes en cours.")
            request = {"kind": kind, "payload": copy.deepcopy(payload), "requester": str(user),
                       "electorate": list(s["config"]["council_ids"]), "approvals": [],
                       "expires_at": now + VOTE_SECONDS, "generation": s["generation"], "status": "pending"}
            s["requests"][rid] = request
            emit(s, events, "vote", f"Demande {rid} : {kind}. Cinq approbations requises, expiration dans 10 minutes.", request=request)
            return rid
        return await self.db.transact(change)

    async def approve(self, rid, user, guild):
        now = self.clock()
        def change(s, events):
            require_council(s, user, guild)
            req = s["requests"].get(rid)
            if not req or req["status"] != "pending" or req["expires_at"] <= now:
                raise RuleError("Demande absente, terminée ou expirée.")
            if req["generation"] != s["generation"] or set(req["electorate"]) != set(s["config"]["council_ids"]):
                raise RuleError("Le conseil a changé ; nouveau vote nécessaire.")
            user_id = str(user)
            if user_id in req["approvals"]:
                raise RuleError("Votre approbation est déjà enregistrée.")
            req["approvals"].append(user_id)
            emit(s, events, "vote", f"Demande {rid} approuvée par {user_id} ({len(req['approvals'])}/5).")
            if set(req["approvals"]) != set(req["electorate"]):
                return len(req["approvals"])
            kind = req["kind"]
            if kind == "finalize":
                if s["phase"] != "migrating" or s["incident"]["status"] not in {"partial", "failed", "empty"}:
                    raise RuleError("Migration encore en cours.")
                s["phase"] = "awaiting_configuration"
                s["expected_guild_id"] = s["config"]["backup_id"]
            else:
                if s["phase"] != "active":
                    raise RuleError("Une migration a invalidé ce vote.")
                if kind == "reset":
                    s["count"] = 0
                    s["count_since"] = now
                elif kind == "pause":
                    if s["pause_until"] > now:
                        raise RuleError("Une autre pause est déjà en cours.")
                    s["pause_until"] = now + PAUSE_SECONDS
                    s["pauses"].append([now, s["pause_until"]])
                elif kind == "resume":
                    if s["pause_until"] > now:
                        s["pauses"][-1][1] = now
                    s["pause_until"] = 0
                elif kind == "configure":
                    old = copy.deepcopy(s["config"])
                    s["config"] = copy.deepcopy(req["payload"])
                    s["config"]["activated_at"] = old["activated_at"]
                    s["config"]["owner_history"] = copy.deepcopy(old.get("owner_history", {}))
                    s["config"]["owner_history"].setdefault(s["config"]["backup_id"], [[now, s["config"]["backup_owner_id"]]])
                    s["generation"] += 1
                    emit(s, events, "configuration", "Nouvelle configuration approuvée à l'unanimité.", previous=old)
            req["status"] = "approved"
            emit(s, events, kind, {
                "reset": "Compteur remis à zéro ; historique conservé.",
                "pause": "Comptage suspendu pendant 5 minutes. Journalisation et protection des bots maintenues.",
                "resume": "Comptage réactivé par les cinq conseillers.",
                "configure": "Configuration exécutée.",
                "finalize": "Bilan incomplet accepté par les cinq conseillers. Le nouveau propriétaire doit configurer Sentinelle.",
            }[kind], approvals=req["approvals"], pause_until=s["pause_until"])
            return 5
        return await self.db.transact(change)

    async def tick(self):
        now = self.clock()
        def change(s, events):
            if s["pause_until"] and s["pause_until"] <= now:
                s["pause_until"] = 0
                emit(s, events, "resume", "Les 5 minutes sont écoulées : comptage automatiquement réactivé.")
        await self.db.transact(change)

    async def audit(self, guild, event_id, actor, owner, action, target, occurred_at, details=None):
        now = self.clock()
        def change(s, events):
            cfg = s.get("config")
            if not cfg or str(guild) not in {cfg["primary_id"], cfg["backup_id"]}:
                return
            is_primary = str(guild) == cfg["primary_id"]
            if occurred_at < cfg["activated_at"]:
                return
            emergency = is_primary and action in {"kick", "ban"} and str(target) in {cfg["guardian_id"], cfg["sentinel_id"]}
            is_owner = str(actor) == str(owner)
            if not emergency and not is_owner:
                return
            paused = any(start <= occurred_at < end for start, end in s["pauses"])
            counted = (is_owner and is_primary and not emergency and s["phase"] == "active"
                       and not paused and occurred_at >= s.get("count_since", 0))
            if counted:
                s["count"] += 1
            emit(s, events, "owner_action" if is_owner else "bot_removed",
                 f"Action {action} par {actor or 'auteur inconnu'} sur {target or 'serveur'} : "
                 + (f"infraction {s['count']}/{THRESHOLD}." if counted else "non comptabilisée.")
                 + (" Pause de cinq minutes." if paused else ""),
                 audit_id=str(event_id), guild_id=str(guild), actor_id=str(actor),
                 occurred_at=occurred_at, counted=counted, paused=paused, action=action, changes=details or {})
            if emergency:
                begin_migration(s, events, "expulsion/bannissement d'un bot protégé", now)
            elif counted and s["count"] >= THRESHOLD:
                begin_migration(s, events, "trois infractions du propriétaire", now)
        return await self.db.transact(change, key="audit:" + str(event_id))

    async def missing_bot(self, guild, bot_id):
        now = self.clock()
        def change(s, events):
            cfg = s.get("config")
            if not cfg or str(guild) != cfg["primary_id"] or str(bot_id) not in {cfg["guardian_id"], cfg["sentinel_id"]}:
                return
            if s["phase"] == "active":
                emit(s, events, "bot_missing", f"Bot {bot_id} absent ; auteur non confirmé. Migration de précaution.")
                begin_migration(s, events, "disparition d'un bot protégé, auteur inconnu", now)
        await self.db.transact(change)
