# Modifications — Sentinelle v2

- Configuration dynamique dans Supabase ; disparition des IDs obligatoires du `.env`.
- Initialisation réservée au propriétaire réel ; bot inactif avant validation.
- Propriétaires principal/secours détectés, changements documentés, historique d'identité pour les audits retardés.
- Conseil fixé à cinq IDs distincts et rôle corrigé ; le rôle seul ne permet pas d'usurper une voix.
- Votes persistants à 5/5 : reset, pause 300 secondes, réactivation, reconfiguration et acceptation d'un bilan incomplet.
- Demandes expirables et non rejouables ; contrôle de version et transaction SQL pour éviter les votes perdus.
- Journal des actions owner pendant et hors pause, plus décisions du conseil et maintenance.
- Comparaison correcte des actions Discord et déduplication des événements Gateway/journal d'audit.
- Rattrapage périodique des journaux après déconnexion, sans bloquer la boucle Discord sur des requêtes synchrones.
- Migration directe à la disparition d'un bot protégé, y compris pendant une pause ; suppression complète de la purge des salons.
- Ajouts OAuth2 correctement authentifiés, reprises bornées et gestion partagée des réponses 429.
- Serveur OAuth2 `/authorize` et `/callback`, contrôle state/cookie, consentement associé à une destination.
- Jetons chiffrés, expiration enregistrée et révocation distincte d'une erreur temporaire.
- Pagination des membres, maintenance horaire, prise en compte des retours et retraits d'autorisation.
- Migration avec progression par membre, bilan partiel/échec/vide, reprise après interruption et annonce sur le secours.
- Reconfiguration du nouveau principal par son propriétaire après migration, avec historique conservé.
- Migration SQL fermant les anciennes politiques publiques ; nouvelles tables réservées au backend.
- Remplacement des tests simulant l'ancien comportement par des régressions métier, OAuth HTTP et adaptateurs.
- Guide et exemple d'environnement réécrits ; plus de promesse de restauration de rôles/messages non implémentée.

## Mise en service nécessaire

Le code ne modifie pas automatiquement votre Supabase ni votre `.env` et ne démarre pas le bot en
production. Appliquer la migration SQL, installer les dépendances, renseigner les secrets et HTTPS,
puis effectuer la recette Discord décrite dans `GUIDE.md`.

Les données historiques sont conservées. Les inscriptions OAuth2 anciennes nécessitent un nouveau
consentement ; aucune destination ne leur est attribuée arbitrairement.

## Vérifications effectuées

`python -B -m unittest -v` : **47 tests réussis**. Couverture : gouvernance réelle, déduplication,
pause persistante et événements retardés, identité des propriétaires, correction des rôles, reprise
de migration, autorisation HTTP OAuth2/state/cookie, chiffrement, pagination et repli des journaux.

Ces tests sont locaux et utilisent des adaptateurs simulés pour Discord/Supabase. La migration SQL
n'a pas été exécutée sur une base réelle et aucune migration de membres Discord n'a été déclenchée.
La recette d'intégration sur une infrastructure de test reste nécessaire avant mise en production.
