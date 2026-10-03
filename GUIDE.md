# Déployer et utiliser Sentinelle

## 1. Décisions appliquées

Le seuil est de **trois infractions**. Toutes les actions du propriétaire exposées dans les journaux
d'audit Discord sont surveillées, y compris l'ajout/retrait d'un rôle. Les messages, connexions au compte
et autres activités non exposées par cette API ne sont pas observables par Sentinelle.

L'accès au propriétaire obtenu via un coffre n'accorde aucune exemption. Seule une pause approuvée
par les cinq conseillers suspend le comptage. L'expulsion/bannissement ou la disparition confirmée
d'un bot protégé reste un déclencheur d'urgence, quelle que soit l'identité de l'auteur. Une indisponibilité
temporaire du serveur Gateway n'est pas traitée comme une exclusion.

La migration ajoute les membres ayant consenti au secours ; elle ne les retire pas du principal et ne
copie ni messages, ni rôles, ni salons. Il n'existe plus de purge/destruction automatique.

## 2. Préparer Discord

Dans le portail développeur, créer ou utiliser l'application de Sentinelle. Son secret OAuth2 et son
jeton de bot doivent appartenir à la **même application**. L'ID d'application est découvert au démarrage.

Activer **Server Members Intent** et **Message Content Intent**. Les commandes utilisent le préfixe `!`.
Installer Sentinelle sur le principal et sur le secours. Gardien doit être présent sur le principal.

Permissions minimales de Sentinelle :

| Serveur | Permissions |
| --- | --- |
| Principal | Voir les salons, Envoyer des messages, Voir les logs du serveur, Gérer les rôles |
| Secours | Voir les salons, Envoyer des messages, Voir les logs du serveur, Créer une invitation |

Placer le rôle de Sentinelle au-dessus du rôle du conseil et des rôles les plus hauts des conseillers.
Choisir un rôle ordinaire pour le conseil, pas `@everyone`, ni un rôle géré par une intégration.
La permission Administrateur n'est pas nécessaire au bot.

Discord ne permet pas à un bot de neutraliser les droits suprêmes du propriétaire. Si celui-ci retire
les permissions de Sentinelle, certaines corrections deviennent impossibles ; elles sont signalées
et conservées en base. Un rôle supprimé n'est pas recréé silencieusement : le conseil peut approuver
une configuration pointant vers un nouveau rôle, y compris depuis son identité sans le rôle disparu.

## 3. Préparer Supabase

Appliquer dans l'éditeur SQL de Supabase :

```text
supabase/migrations/20261003000000_sentinel_v2.sql
```

Sur une nouvelle base, cette migration suffit. Sur une ancienne installation, elle ferme également
les politiques publiques des tables historiques sans effacer leurs données.

Elle crée `sentinel_state`, `sentinel_operations`, `sentinel_events`, `sentinel_members` et la fonction
transactionnelle `sentinel_commit`. Seul `service_role` y accède. État, déduplication et événements sont
enregistrés dans une même transaction avec contrôle de version.

Ne jamais donner la clé `service_role` au navigateur, à une commande Discord ou à un dépôt public.
Les jetons v2 sont chiffrés avec Fernet avant stockage. Sauvegarder séparément la clé de chiffrement.
Les anciennes tables peuvent encore contenir des jetons en clair : leur accès public est fermé par
la migration, mais leur archivage/suppression éventuelle doit être décidé séparément.

## 4. Hébergement et OAuth2

Installer Python 3.11+ et les dépendances de `requirements.txt`. Remplir un `.env` à partir de
`.env.example`, sans écraser les secrets déjà présents.

Générer une clé de chiffrement :

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Le service HTTP écoute par défaut sur `127.0.0.1:8080`. Un reverse proxy doit exposer en HTTPS
les routes `/authorize` et `/callback`. Enregistrer exactement `OAUTH2_REDIRECT_URI` dans Discord
(exemple `https://sentinelle.exemple.org/callback`). HTTP est accepté uniquement pour localhost.

Désactiver les journaux contenant les query strings de `/callback` dans le reverse proxy :
ils contiennent le code OAuth2 temporaire. Le serveur Python désactive déjà ses access logs.

Lancer :

```sh
python run.py
```

Exécuter **une instance de bot par communauté/base**. Les transactions protègent les votes et incidents
contre les courses, mais deux instances actives ne sont pas une architecture haute disponibilité
validée, notamment pour le renouvellement des jetons OAuth2. Les baux de migration permettent la
reprise après arrêt ; une migration `running` abandonnée est récupérée après expiration du bail (120 s).

Utiliser un superviseur de processus de l'hébergement pour redémarrer le bot après un crash. Le callback
et le bot tournent dans le même processus. Un parcours OAuth commencé avant un redémarrage doit être
recommencé ; les inscriptions terminées et les votes sont persistants.

## 5. Configuration initiale

Le propriétaire tape dans le principal :

```text
!configurer SECOURS_ID GARDIEN_ID ROLE_ID ID1,ID2,ID3,ID4,ID5 SALON_ID SALON_SECOURS_ID
```

Utiliser des IDs numériques réels (mode développeur Discord). Les cinq personnes doivent être
distinctes et présentes sur le principal. Le bot attribue/restaure le rôle pour ces cinq identités
après validation ; une personne supplémentaire portant ce rôle est retirée du rôle.

Les salons sont facultatifs. L'ordre de sélection est : salon explicitement configuré, salon système
accessible, puis premier salon textuel accessible dans l'ordre du serveur. Un salon explicitement
fourni mais invalide fait échouer la configuration : l'utilisateur voit l'erreur avant activation.

Le bot affiche les salons choisis. En fonctionnement, si l'envoi échoue, il essaie les autres salons
accessibles, puis le secours. Le journal est également publié sur le secours lorsqu'il est accessible.
Si aucun envoi n'est possible, l'événement reste dans la file de publication en base et les logs de
l'hébergement signalent l'échec. Prévoir ces salons en conséquence : ne pas y mettre de secrets.

Le journal est livré **au moins une fois** : un crash entre l'envoi Discord et sa confirmation en base
peut produire un doublon de message, identifié par le même UUID. Le compteur, lui, reste dédupliqué.

## 6. Inscrire les membres existants

Publier `!inscription`, expliquer la destination affichée et épingler le message. Chaque membre clique
sur le bouton puis accorde `identify` et `guilds.join` dans Discord. Le callback vérifie le navigateur
(`state` + cookie HttpOnly), l'expiration, les scopes et l'appartenance au principal avant d'enregistrer
les jetons chiffrés. Aucun jeton n'est affiché dans Discord.

`!moninscription` indique si une autorisation est enregistrée ; ce n'est pas une vérification en temps
réel de la validité chez Discord. `!retirer` supprime les jetons locaux de cette destination et empêche
les futurs ajouts. Une révocation globale reste disponible dans les applications autorisées Discord.

Une autorisation est liée au couple **principal → secours**. Changer de secours exige un nouveau
consentement pour cette nouvelle destination. Les membres non inscrits ne sont pas ajoutés par le bot.

La maintenance s'exécute au démarrage puis chaque heure. Elle récupère la liste complète avant de
marquer un membre absent, réactive les membres revenus et renouvelle les jetons proches de l'expiration.
Une erreur réseau/serveur ne devient jamais une révocation ; seul `invalid_grant` invalide le jeton.

## 7. Votes du conseil

Un conseiller lance `!pause`, `!reset`, `!reactiver` ou une proposition `!configurer ...`.
Le bot fournit un identifiant ; chacun des cinq membres tape `!approuver IDENTIFIANT`.
Le demandeur doit lui aussi approuver. Le vote expire au bout de dix minutes.

Les droits se fondent sur les cinq IDs enregistrés, pas sur le rôle actuellement affiché. Perdre son
rôle ne prive donc pas un conseiller du droit de vote. Les commandes peuvent être utilisées sur le
principal ou sur le secours (sauf proposition de configuration, faite depuis le principal).

La pause commence à la cinquième approbation et dure exactement 300 secondes. Pendant la pause, le bot
continue à corriger le rôle du conseil et à journaliser les actions ; il ne compte pas les infractions
ordinaires. Une deuxième pause ne prolonge pas une pause en cours. La réactivation automatique est
annoncée par la supervision dans les secondes suivantes, même après redémarrage.

Les événements d'audit reçus en retard utilisent leur date réelle : une action effectuée pendant la
pause reste non comptée après sa fin. Après un reset, les actions antérieures au reset restent dans
l'historique mais ne recréent pas les anciennes infractions.

## 8. Migration et nouveau principal

La destination est figée dans l'incident. Les autres votes en cours sont invalidés. Les membres encore
éligibles et inscrits pour cette destination sont ajoutés, avec gestion des limites Discord et des
erreurs. Chaque succès est enregistré. En cas d'exclusion de Sentinelle, le processus hébergé continue
à utiliser les jetons et le bot présent sur le secours ; il n'existe pas de second programme autonome.

Si le principal devient inaccessible, la dernière appartenance connue est utilisée. Si Discord
permet encore sa consultation, l'appartenance est vérifiée avant chaque ajout. Un ajout réussi peut
encore nécessiter le règlement/filtrage d'accueil du secours ; le bot ne contourne pas ces règles.

Le bilan distingue :

- `completed` : tous les membres éligibles traités ont réussi ; le secours attend sa configuration.
- `partial` : certains ajouts ont échoué ; un conseiller peut lancer `!reprendre`.
- `failed` : un incident technique a interrompu l'exécution ; les succès déjà enregistrés restent acquis.
- `empty` : aucun membre éligible ; aucune réussite générale n'est annoncée.

Pour accepter un bilan incomplet, les cinq conseillers votent `!finaliser`. Le propriétaire du serveur
de destination peut ensuite utiliser `!configurer` pour désigner un nouveau secours, les salons, le
gardien, le rôle et cinq conseillers. C'est un **nouveau mandat**, pas une conservation obligatoire de
l'ancien conseil. Aucune configuration sur un serveur tiers ne peut remplacer cette étape.

Le journal antérieur est conservé. Les actions observables restent documentées pendant l'attente,
sans nouveau comptage. Après activation, le nouveau propriétaire est surveillé et un nouveau cycle
d'inscription vers la nouvelle destination commence.

## 9. Recette sur une infrastructure de test

Commencer par `python -B -m unittest -v` (aucun accès à vos services).
Puis, sur une base dédiée et deux serveurs de test :

1. Vérifier que `anon`/`authenticated` ne peuvent ni lire les jetons, ni modifier l'état.
2. Installer le bot : les actions du propriétaire ne déclenchent rien avant configuration.
3. Configurer depuis un autre compte : refus ; depuis le propriétaire : activation et journal.
4. Inscrire un compte de test via OAuth2, puis vérifier `!moninscription`.
5. Faire voter quatre conseillers pour une pause : pas d'effet ; le cinquième active la pause.
6. Faire une action owner pendant la pause : journal, compteur inchangé ; attendre la fin et vérifier.
7. Attribuer le rôle à une sixième personne : correction du rôle et une seule infraction owner.
8. Tester une remise à zéro unanime ; réutiliser le même vote doit échouer.
9. Expulser le gardien sur le serveur de test : une seule migration, aucun salon supprimé.
10. Reconfigurer sur la destination avec son propriétaire et une nouvelle destination prête.

Une expulsion réelle de Sentinelle doit être testée séparément : le journal doit préciser quand
l'auteur est inconnu et le message de crise doit parvenir au secours. Tester aussi un jeton révoqué,
un défaut de permission, une interruption du processus et la reprise d'une migration partielle.

Sources techniques : [OAuth2 Discord](https://docs.discord.com/developers/topics/oauth2),
[ajout de membres](https://docs.discord.com/developers/resources/guild#add-guild-member),
[RLS Supabase](https://supabase.com/docs/guides/database/postgres/row-level-security).
