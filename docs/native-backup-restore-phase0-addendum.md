# Backup/restore natif — complément Phase 0

> **Document historique — obsolète.** Il décrit le modèle Phase 0 fondé sur le
> restore de staging/validation, l'activation vers une nouvelle base et le
> rollback durable. Ce modèle **n'est plus implémenté** : le restore livré est
> désormais **destructif et en place** (il remplace la base et le stockage
> actifs puis redémarre). Voir
> [Native Backup & Restore](./native-backup-restore.md) pour le comportement,
> l'exploitation et le format de transport actuels. Les sections ci-dessous sont
> conservées uniquement à titre de référence de conception.

Ce complément fixe les contrats volontairement laissés ouverts par le rapport
Phase 0. La première capacité livrée est appelée **restore de
staging/validation** : elle ne remplace jamais la base PostgreSQL ni le
`data_dir` actifs.

## 1. Provenance du serveur A vers le serveur B

Le serveur A signe le manifeste de chaque backup scellé avec Ed25519. La charge
signée est le JSON canonique, précédé d'un domaine explicite :

```text
"PPBASE-BACKUP-MANIFEST-V1\0" || canonical_manifest_json
```

Le schéma du manifeste n'autorise que des valeurs JSON déterministes : chaînes
UTF-8 normalisées NFC, entiers, booléens, tableaux, objets et `null`. Les clés
d'objet sont triées et les nombres flottants sont interdits. La signature et la
clé publique du signataire sont des ressources de transport distinctes de
`manifest.json`. Le fingerprint est
`SHA-256(raw_ed25519_public_key)`.

Dans la phase transport A→B suivante — non livrée par les étapes locales A et
B — un superuser authentifié sur B devra approuver explicitement la clé publique
exacte et son fingerprint dans le trust store du control-plane backup. Un simple
fingerprint textuel ne suffit pas : B le recalcule depuis la clé approuvée,
vérifie que la clé transportée lui est identique octet pour octet, puis vérifie
la signature Ed25519. Les approbations vivent hors de la DB restaurée et
conservent uniquement le libellé, le superuser approbateur et la date
d'approbation, jamais une clé privée.

La clé privée de signature appartient au control-plane de A, hors du dump
PostgreSQL et du `data_dir` métier, avec des permissions `0600`. Elle n'est
jamais incluse dans un backup ou une réponse. Une signature valide avec une clé
non approuvée produit l'état `authenticated_untrusted`; une signature absente ou
invalide produit `unauthenticated`. Aucun de ces états ne peut créer ou exécuter
un plan de staging dans le cluster de B. Dans cette phase transport, un backup externe
non approuvé peut seulement être mis en quarantaine et inspecté ; la validation
PostgreSQL exige d'abord l'approbation explicite de sa clé.

## 2. Barrière d'écriture commune

Le coordinateur commun est un nouveau service d'advisory locks PostgreSQL,
scopé par base, utilisé par tous les chemins PPBase couplant état DB et fichiers
métier. Sa clé dérive de :

```text
current_database() || ':ppbase:backup-write-barrier'
```

Les mutations DB↔fichiers acquièrent une advisory lock **shared session-level**
avant la première action SQL ou filesystem. Elles la conservent pendant le
commit, la réconciliation d'un résultat ambigu, les compensations et les
suppressions différées. Le backup acquiert la lock **exclusive session-level**
correspondante et la conserve pendant `pg_dump`, la copie locale, les checksums
et le dernier contrôle de lease précédant le scellement.

L'ordre global d'acquisition est toujours :

1. barrière d'écriture backup ;
2. `migration_lock` lorsque l'opération exige aussi un schéma stable.

Les deux locks utilisent la même connexion SQLAlchemy dédiée. Cela évite un
second checkout et garantit le fonctionnement avec `pool_size=1`. Elles sont
libérées dans l'ordre inverse. Le coordinateur mémorise le PID du backend
PostgreSQL et vérifie la lease sur la même session avant le scellement. Une
connexion fermée, invalidée, remplacée ou invérifiable signifie une perte de
lease : le backup est abandonné et jamais scellé. Une annulation attend d'abord
la fin effective de tout worker filesystem ; le set partiel est ensuite supprimé
avant de libérer la barrière ou de publier un statut terminal.

Les extractions génériques sont limitées aux invariants qui doivent être
strictement identiques entre runtime et backup :

- `migration_lock_on_connection(connection, ...)` expose l'advisory lock de
  migration existante sur la connexion déjà réservée, sans changer sa clé ni
  sa sémantique ;
- le collecteur read-only des références fichier persistées est partagé avec
  la réconciliation des mutations record ;
- le mapping pur d'un ID logique collection/record vers son nom de répertoire
  local est partagé avec la validation du staging, y compris le fallback
  legacy exact.

Ces extractions sont nécessaires pour respecter l'ordre global,
`pool_size=1` et une interprétation DB↔fichiers unique. `ArtifactStore` et les
primitives import/export de `00dbe28` ne sont pas réutilisés : leur format,
leur publication et leur modèle de reprise ne correspondent pas à un snapshot
PostgreSQL + fichiers scellé.

Règles de couverture :

- un create/update/delete de record conserve une lease shared unique ;
- un batch entier conserve une seule lease, jamais une lease par sous-requête ;
- les hooks synchrones de records s'exécutent dans la lease record/batch ;
- un hook lançant un travail de fond doit acquérir sa propre lease shared avant
  toute mutation DB↔fichiers ; une lease héritée par `ContextVar` n'est valide
  que dans la tâche `asyncio` propriétaire ;
- la lecture d'une thumbnail reste sans lock, mais sa génération et sa
  publication acquièrent une lease shared ;
- une bascule runtime local↔S3 prend la barrière exclusive afin de ne jamais
  chevaucher une mutation ayant épinglé l'ancien backend ; après commit ambigu,
  annulation ou perte de connexion, elle relit la valeur durable sous une
  nouvelle barrière avant de rendre la main ; le backup compare aussi ce
  snapshot durable au backend runtime épinglé et échoue fermé s'ils divergent ;
- les opérations de schéma continuent d'utiliser `migration_lock`; toute future
  opération ayant besoin des deux respecte l'ordre global ci-dessus ;
- tout point d'entrée builtin modifiant les fichiers métier exige une lease
  explicite et échoue fermé sans elle ; les seuls helpers sans lease sont privés
  et réservés au bootstrap/teardown sans trafic. Les writers filesystem et
  clients SQL externes non coopératifs restent hors garantie.

## 3. Workflow produit A vers B sans terminal

Le stockage canonique sur A est un backup set serveur immuable :

```text
sets/<backup-id>/
  manifest.json
  manifest.sig
  signer.pub
  resources/database.dump
  resources/files/...
  SEALED
```

Les ressources sont stagées, hashées et fsyncées. Elles ne deviennent visibles
qu'après création sans overwrite du marqueur final `SEALED`. Ce répertoire est
le stockage canonique, pas le format transporté par le navigateur.

Le transport désormais livré utilise un ZIP standard `.zip` généré en flux à
partir du set canonique, contenant le même manifeste, la signature et les
ressources. Il est produit et consommé sans conserver un second ZIP géant côté
serveur. L'endpoint d'upload authentifie avant lecture, puis applique les
limites déclarées, par ressource et globales. B reconstruit et vérifie les
ressources canoniques avant de sceller son backup set local.

Scénario produit sans terminal de cette phase suivante :

1. Depuis un client d'administration web, un superuser demande à l'API de A de
   créer un backup local.
2. Le client affiche le statut retourné par A, l'inspection du manifeste, le
   fingerprint et l'action de téléchargement.
3. Le superuser télécharge l'enveloppe dans son navigateur.
4. Sur B, un superuser approuve la clé publique/fingerprint de A dans Backup
   trust.
5. Le superuser upload l'enveloppe sur B ; B vérifie limites, checksums et
   signature avant de la déclarer approuvée.
6. Le superuser crée un plan scellé de staging/validation avec une nouvelle DB,
   un nouveau `data_dir`, une empreinte non secrète du cluster/rôles/owner/
   allowlist préflightés et le mode JWT disaster-recovery ou clone.
7. B restaure et valide uniquement ces nouvelles cibles, puis expose `validated`
   ou un échec quarantainé. L'instance PPBase active reste intacte.

Chaque exécution possède un `attemptId` durable et un lease fichier verrouillé.
Après crash, la perte du verrou est réconciliée en `quarantined` avec
`staging_owner_lost`; un plan orphelin ne reste donc pas indéfiniment `running`.
Le `data_dir` de staging est créé et utilisé via des descripteurs ancrés sous un
`backup_staging_root` privé, avec `mkdirat`/`openat`, `O_NOFOLLOW` et contrôle de
chaque composant avant et après les copies.

### Précondition de sécurité du control-plane local

PPBase doit s'exécuter sous un utilisateur de service dédié, non-root. Le
`backup_control_dir` doit être un répertoire privé appartenant à cet utilisateur
et protégé en `0700` avant le démarrage. Le plan store applique désormais cette
frontière : une racine préexistante qui n'appartient pas à l'utilisateur courant
ou dont le mode n'est pas exactement `0700` est refusée sans être réparée.
La création d'une racine manquante exige que chaque ancêtre appartienne soit à
`root`, soit à l'utilisateur de service. Elle est également refusée sous un
ancêtre group/world-writable non protégé par le sticky bit. Toute la chaîne de
répertoires depuis `/` reste épinglée par des descripteurs et sa propriété, son
mode ainsi que chaque relation parent → enfant sont revérifiés à chaque usage
de la capacité racine.

La racine est conservée comme une capacité par descripteur commune au plan store
et à l'identité Ed25519. `control/plans`, `control/identity`, chaque répertoire de
plan et tous les fichiers, marqueurs et leases sont ensuite ouverts avec
`dir_fd`, `O_DIRECTORY` et `O_NOFOLLOW`. Les identités inode/device sont
vérifiées avant et après les mutations, de la racine jusqu'au plan ; le
descripteur du plan reste épinglé entre `begin_execution` et `finish`. Les
statuts terminaux sont écrits et fsyncés sous un nom temporaire, puis publiés
sans remplacement par hard-link. Après le fsync de commit, un échec du nettoyage
best-effort ne peut plus supprimer le statut durable ; les temporaires orphelins
sont supprimés par lots bornés sans bloquer la réconciliation. Avant de rendre
un résultat terminal, le store relit le JSON canonique effectivement publié et
refuse toute divergence. La clé Ed25519 persistée est également comparée à la
clé mise en cache avant et après ses usages, puis une dernière fois juste avant
le scellement d'un backup.
Un symlink préexistant, une substitution par rename ou un crash avant
publication provoque donc un échec fermé sans fichier terminal partiel et sans
`chmod`, lecture ni écriture hors du control-plane. Cette protection reste
complémentaire à l'exécution sous un utilisateur dédié non-root : elle ne
transforme pas un control-plane partagé en frontière d'administration sûre.
Les descripteurs de la racine, du répertoire d'identité, de la clé, des plans et
des leases sont enfin fermés explicitement à la fin de chaque opération API, y
compris en cas d'erreur. Le store local autonome possède et ferme les mêmes
capacités lorsqu'il crée lui-même son identité.

La tranche actuellement implémentée s'arrête au backup set canonique local et
au staging vers de nouvelles cibles ; elle ne livre pas encore download,
upload, trust store ni Dashboard.

La phase produit suivante est **l'activation sur un serveur B neuf**. Elle est
autorisée seulement si B est déclaré frais/vide. Après validation du staging,
elle mettra atomiquement à jour un pointeur de control-plane externe vers la DB
et le `data_dir` validés, redémarrera le serveur, appliquera un health gate et
restaurera l'ancien pointeur si le démarrage échoue. Elle n'écrasera pas une
installation B déjà active.

## 4. Contrat PostgreSQL

### Modèle owner et ACL

Le backup natif conserve les lignes applicatives PPBase, pas les rôles globaux
du cluster PostgreSQL. Le restore utilise toujours :

```text
--no-owner --no-acl --no-tablespaces
```

Tous les objets restaurés appartiennent à un owner cible dédié. Les owners
source, `GRANT`, `REVOKE` et chemins de tablespaces ne sont pas reproduits.
`pg_dumpall` n'est pas utilisé.

### Rôles séparés et privilèges exacts

Le rôle de dump source est un login dédié, distinct du rôle runtime writable,
non-superuser et possède uniquement :

- `CONNECT` sur la DB source ;
- `USAGE` sur chaque schéma sauvegardé ;
- `SELECT` sur toutes les tables, séquences et large objects, normalement via
  `pg_read_all_data` plus les accès particuliers requis par une extension
  locale ;
- aucun `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS`, rôle d'accès aux
  fichiers serveur ou d'exécution de programmes ;
- aucun `CREATE`/`TEMPORARY` sur la DB, `CREATE` de schéma, privilège d'écriture
  table/séquence/large object, ni `MAINTAIN` sur PostgreSQL 17+.

Sur B, trois responsabilités sont distinctes :

- le rôle de création est `LOGIN CREATEDB NOINHERIT`, non-superuser, avec
  `CONNECT` sur la DB de maintenance et une membership directe vers l'owner
  cible dont les options sont exactement `SET TRUE, ADMIN FALSE, INHERIT
  FALSE` ; il crée la cible depuis `template0` et normalise sa configuration ;
- le rôle de restauration est `LOGIN NOCREATEDB NOINHERIT`, avec la même
  membership directe `SET TRUE, ADMIN FALSE, INHERIT FALSE` vers l'owner cible ;
  la nouvelle DB lui accorde exactement `CONNECT, TEMPORARY`, puis
  `pg_restore --role=<target-owner>` est utilisé ;
- l'owner cible est `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS`.

Aucun de ces rôles ne reçoit `pg_read_server_files`, `pg_write_server_files`
ou `pg_execute_server_program`.

### Extensions

`plpgsql` est la seule extension implicitement autorisée en v1. Toute autre
extension est bloquée sauf présence d'une allowlist exacte `name=version` sur B
et disponibilité sans privilège superuser pour le restore. B n'installe jamais
un paquet d'extension depuis le backup. Une extension manquante, différente ou
exigeant le superuser bloque le plan.

### Encoding, locale et collation

Le manifeste enregistre l'encoding source, `datlocprovider`, `datcollate`,
`datctype`, le locale/rules ICU lorsque présents et les versions de collation
pertinentes. La v1 exige UTF-8, la même version majeure PostgreSQL, le même
provider (`libc` ou `icu`) et la disponibilité exacte du locale sur B. Pour ICU,
locale/rules et version de collation doivent correspondre ; pour libc,
`LC_COLLATE` et `LC_CTYPE` doivent exister et correspondre. Toute divergence
bloque le plan au lieu de reconstruire silencieusement les index sous une autre
sémantique.

La DB cible est créée depuis `template0` avec ce contrat d'encoding et locale.
`pg_restore` n'accepte ni `--create` ni `--clean`.

### DSN libpq et credentials

Les DSN dump, création et restauration sont distincts. Les URLs SQLAlchemy
`postgresql+asyncpg://` ne sont jamais passées directement aux outils
PostgreSQL. PPBase convertit les paramètres approuvés en URI/conninfo libpq sans
mot de passe. Le mot de passe est écrit dans un `PGPASSFILE`
temporaire par opération, créé avec `O_EXCL`, mode `0600`, puis supprimé après le
processus enfant. Le DSN, le passfile, l'environnement et stderr sont expurgés
avant toute journalisation durable.

Pendant un restore de staging, le passfile n'a aucun nom réouvrable : il est
créé comme inode anonyme `0600` via le descripteur ancré du plan, puis transmis
explicitement au processus PostgreSQL par `/proc/self/fd` ou `/dev/fd`. Une
substitution du chemin visible ne peut donc ni déposer le secret dans le
`data_dir` actif ni rediriger ce que libpq relit ; un crash ferme l'inode.

Le DSN dump est lié à la session active par l'identité de l'instance PostgreSQL
observée et par l'import du snapshot exporté. Les DSN creator/restore doivent
désigner un endpoint unique : multi-hôtes, `hostaddr` fourni par l'opérateur et
`target_session_attrs` sont refusés, le nom doit résoudre vers une seule adresse
et les outils libpq sont pinnés avec le `hostaddr` calculé. L'identité de
l'instance, les rôles, DB de maintenance, owner, endpoints et allowlist forment
l'empreinte scellée du plan, recalculée après un nouveau preflight juste avant
toute création.

Après création, un marqueur aléatoire est posé sur la nouvelle DB et vérifié
sur l'instance attendue. Il reste présent pendant `pg_restore`, est revérifié
sur la connexion de validation, puis seulement retiré dans la transaction de
validation exécutée sous `SET LOCAL ROLE <target-owner>`. Le dump signé est
copié en vérifiant son hash vers un inode temporaire anonyme ancré ;
`pg_restore --list` et le restore consomment ce même descripteur par stdin,
jamais un chemin canonique rouvert.

## 5. Modes JWT

Le plan scellé sélectionne exactement un mode :

- **disaster recovery** — copier le `.jwt_secret` local généré par A comme
  ressource secrète signée `0600`; les sessions existantes restent valides. La
  tranche actuelle échoue fermé pour un `PPBASE_JWT_SECRET` externe, car une
  valeur B simplement non vide ne prouve pas son égalité avec A. Le transport
  A→B devra ajouter une preuve sûre avant d'autoriser ce cas.
- **clone** — ne jamais installer le `.jwt_secret` de A ; générer un nouveau
  secret B, puis renouveler transactionnellement les `token_key` de tous les
  records auth et superusers ainsi que les secrets de token des collections
  auth (`authToken`, reset, vérification, changement d'email et fichier). Le
  seul changement de `.jwt_secret` ne suffit pas dans le modèle actuel. Les
  sessions et purpose tokens existants sont invalidés, mais les comptes et
  mots de passe restent valides.

Aucune valeur secrète n'apparaît dans le manifeste, les métadonnées de
signature, les statuts API ou les logs.
