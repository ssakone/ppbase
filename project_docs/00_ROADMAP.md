# PPBase - Roadmap & Vue d'Ensemble du Projet

## PocketBase reimagine en Python + PostgreSQL

**Date:** 2026-02-06
**Status:** Phase de planification

---

## Qu'est-ce que PPBase ?

PPBase est une reimplementation de [PocketBase](https://pocketbase.io/) en Python, utilisant **PostgreSQL** au lieu de SQLite. L'objectif est de fournir la meme experience developpeur (BaaS) -- collections dynamiques, API REST auto-generee, auth integree, admin UI -- tout en tirant parti de la puissance de PostgreSQL.

```python
from ppbase import PPBase

app = PPBase(database_url="postgresql+asyncpg://localhost:5432/ppbase")
app.start(host="0.0.0.0", port=8090)
```

---

## Documents de Reference

| # | Document | Contenu | Lignes |
|---|----------|---------|--------|
| 01 | [pocketbase_features.md](./01_pocketbase_features.md) | Fonctionnalites completes de PocketBase (14 types de champs, 3 types de collections, auth, rules, files, realtime) | 1811 |
| 02 | [pocketbase_architecture.md](./02_pocketbase_architecture.md) | Architecture du code source Go (models, DB layer, API, hooks, migrations, auth flow) | 1730 |
| 03 | [api_specification.md](./03_api_specification.md) | Specification API complete (42 endpoints, filtres, tri, pagination, expand) | 2535 |
| 04 | [data_models_and_db.md](./04_data_models_and_db.md) | Modeles de donnees Go (14 field types, Record, Collection, schema sync, DDL) | 1239 |
| 05 | [python_implementation_strategy.md](./05_python_implementation_strategy.md) | Strategie d'implementation Python (tech stack, architecture, DB schema, roadmap) | 1845 |

**Total: ~9160 lignes de documentation**

---

## Stack Technique

| Composant | Technologie | Raison |
|-----------|-------------|--------|
| Framework web | **FastAPI** (Starlette + Uvicorn) | Async natif, OpenAPI auto, Pydantic v2 |
| Base de donnees | **PostgreSQL** via asyncpg | JSONB, arrays, full-text search, LISTEN/NOTIFY |
| ORM (tables systeme) | **SQLAlchemy 2.0 async** | Standard industrie, Alembic |
| Tables dynamiques | **SQLAlchemy Core** | Generation SQL sans modeles statiques |
| Auth / JWT | **PyJWT + passlib[bcrypt]** | Compatible PocketBase |
| Validation | **Pydantic v2** | Integration FastAPI native |
| Parser de filtres | **Lark** | Grammaire EBNF, transformers |
| Migrations (systeme) | **Alembic** | Auto-generation |
| Migrations (dynamiques) | **SchemaManager custom** | ALTER TABLE a la volee |
| Admin UI | **Svelte SPA** | Compatibilite PocketBase |
| Fichiers | **Local + S3** (pluggable) | Production-ready |
| Realtime (Phase 2) | **SSE + PG LISTEN/NOTIFY** | Compatible PocketBase |

---

## Architecture du Projet

```
ppbase/
├── __init__.py              # Classe PPBase principale (facade)
├── __main__.py              # CLI: ppbase serve, ppbase create-admin
├── config.py                # Settings via Pydantic (env vars, .env)
├── app.py                   # FastAPI app factory + lifespan
│
├── core/                    # Coeur du systeme
│   ├── base.py              # PPBase class (init, start, mount)
│   └── id_generator.py      # Generation d'IDs 15-char alphanumeriques
│
├── db/                      # Couche base de donnees
│   ├── engine.py            # Async engine + connection pool
│   ├── schema_manager.py    # DDL dynamique (CREATE/ALTER TABLE)
│   ├── system_tables.py     # Modeles ORM: _collections, _admins, _params
│   └── migrations/          # Migrations Alembic (tables systeme)
│
├── models/                  # Modeles de donnees
│   ├── collection.py        # CollectionModel (type, schema, rules)
│   ├── record.py            # RecordModel (dynamique, dict-based)
│   ├── admin.py             # AdminModel
│   └── field_types.py       # 14 types de champs + validation
│
├── services/                # Logique metier
│   ├── collection_service.py # CRUD collections + schema sync
│   ├── record_service.py    # CRUD records + validation
│   ├── admin_service.py     # CRUD admins + auth
│   ├── auth_service.py      # JWT, password hash, tokens
│   ├── filter_parser.py     # PocketBase filter syntax -> SQL
│   ├── rule_engine.py       # Evaluation des API rules
│   ├── file_service.py      # Upload, storage, thumbnails
│   └── expand_service.py    # Expansion des relations
│
├── api/                     # Routes REST
│   ├── router.py            # Router principal /api
│   ├── collections.py       # /api/collections/*
│   ├── records.py           # /api/collections/{coll}/records/*
│   ├── admins.py            # /api/admins/*
│   ├── settings.py          # /api/settings
│   ├── health.py            # /api/health
│   ├── files.py             # /api/files/*
│   └── deps.py              # Dependencies FastAPI (auth, DB session)
│
├── middleware/               # Middlewares
│   ├── auth.py              # Extraction/validation JWT
│   ├── cors.py              # CORS configuration
│   └── activity_logger.py   # Logging des requetes
│
├── storage/                  # Backends de stockage fichiers
│   ├── base.py              # Protocol StorageBackend
│   ├── local.py             # Stockage local filesystem
│   └── s3.py                # Stockage S3-compatible
│
└── admin/                    # Admin UI (SPA Svelte build)
    └── dist/                 # Fichiers statiques
```

---

## Schema Base de Donnees PostgreSQL

### Tables Systeme

```sql
-- Collections (definition des schemas)
CREATE TABLE _collections (
    id          VARCHAR(15) PRIMARY KEY,
    name        VARCHAR(255) NOT NULL UNIQUE,
    type        VARCHAR(10) NOT NULL DEFAULT 'base',  -- base, auth, view
    schema      JSONB NOT NULL DEFAULT '[]',
    indexes     JSONB NOT NULL DEFAULT '[]',
    list_rule   TEXT,          -- NULL = superuser only, '' = public
    view_rule   TEXT,
    create_rule TEXT,
    update_rule TEXT,
    delete_rule TEXT,
    options     JSONB NOT NULL DEFAULT '{}',
    created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Admins (superusers)
CREATE TABLE _admins (
    id             VARCHAR(15) PRIMARY KEY,
    email          VARCHAR(255) NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    token_key      VARCHAR(50) NOT NULL,
    created        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Parametres globaux
CREATE TABLE _params (
    id    VARCHAR(15) PRIMARY KEY,
    key   VARCHAR(255) NOT NULL UNIQUE,
    value JSONB NOT NULL DEFAULT '{}'
);

-- Auth externes (OAuth2)
CREATE TABLE _external_auths (
    id            VARCHAR(15) PRIMARY KEY,
    collection_id VARCHAR(15) NOT NULL,
    record_id     VARCHAR(15) NOT NULL,
    provider      VARCHAR(100) NOT NULL,
    provider_id   VARCHAR(255) NOT NULL,
    created       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(collection_id, provider, provider_id)
);
```

### Tables Dynamiques (par collection)

```sql
-- Collection de type "base"
CREATE TABLE {collection_name} (
    id      VARCHAR(15) PRIMARY KEY DEFAULT ppbase_id(),
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- + colonnes dynamiques selon le schema
);

-- Collection de type "auth" (colonnes supplementaires)
--   email          VARCHAR(255) UNIQUE
--   email_visibility BOOLEAN DEFAULT FALSE
--   verified       BOOLEAN DEFAULT FALSE
--   password_hash  TEXT NOT NULL
--   token_key      VARCHAR(50) NOT NULL
```

### Mapping Types de Champs -> PostgreSQL

| Type PocketBase | Type PostgreSQL | Default |
|----------------|-----------------|---------|
| text | TEXT | `''` |
| editor | TEXT | `''` |
| number | DOUBLE PRECISION | `0` |
| bool | BOOLEAN | `FALSE` |
| email | VARCHAR(255) | `''` |
| url | TEXT | `''` |
| date | TIMESTAMPTZ | `NULL` |
| autodate | TIMESTAMPTZ | `NOW()` |
| select (single) | TEXT | `''` |
| select (multi) | TEXT[] | `'{}'` |
| file (single) | TEXT | `''` |
| file (multi) | TEXT[] | `'{}'` |
| relation (single) | VARCHAR(15) | `''` |
| relation (multi) | VARCHAR(15)[] | `'{}'` |
| json | JSONB | `'null'::jsonb` |
| password | TEXT | `''` |
| geoPoint | JSONB | `'{"lon":0,"lat":0}'` |

---

## Plan d'Implementation par Phases

### Phase 1 - Core CRUD + Admin (Semaines 1-6) -- PRIORITE ACTUELLE

**Semaine 1: Fondations**
- [ ] Setup projet (pyproject.toml, structure, CI)
- [ ] Engine PostgreSQL async + connection pool
- [ ] Tables systeme (_collections, _admins, _params)
- [ ] Migrations Alembic initiales
- [ ] Generateur d'IDs 15-char

**Semaine 2: Collections API**
- [ ] SchemaManager: CREATE TABLE dynamique
- [ ] SchemaManager: ALTER TABLE (add/rename/drop columns)
- [ ] CRUD endpoints /api/collections
- [ ] Validation du schema de collection

**Semaine 3: Records CRUD**
- [ ] CRUD endpoints /api/collections/{coll}/records
- [ ] Query builder dynamique (SQLAlchemy Core)
- [ ] Filter parser (Lark) : syntaxe PocketBase -> SQL
- [ ] Pagination (page, perPage, totalItems, totalPages)
- [ ] Tri (sort parameter)
- [ ] Selection de champs (fields parameter)

**Semaine 4: Auth Admin + Rules**
- [ ] Admin auth (email/password -> JWT)
- [ ] Middleware d'authentification JWT
- [ ] Moteur d'evaluation des API rules
- [ ] Macros @request.auth.* dans les rules
- [ ] Protection des endpoints par rules

**Semaine 5: Validation + Relations**
- [ ] Validation complete des 14 types de champs
- [ ] Expansion des relations (expand parameter, jusqu'a 6 niveaux)
- [ ] Modificateurs +/- pour updates partiels (select, relation, file)
- [ ] Endpoint fichiers basique (upload local, serving)

**Semaine 6: Polish**
- [ ] /api/health endpoint
- [ ] /api/settings endpoints
- [ ] Tests d'integration (testcontainers[postgres])
- [ ] CLI: `ppbase serve`, `ppbase create-admin`
- [ ] Documentation API

### Phase 2 - Auth Users + Realtime (Semaines 7-12)

- [ ] Auth collection: inscription, login, verification email
- [ ] OAuth2 providers (Google, GitHub, etc.)
- [ ] MFA / OTP
- [ ] SSE realtime via LISTEN/NOTIFY
- [ ] Before/after event hooks
- [ ] Stockage S3
- [ ] Thumbnails d'images
- [ ] View collections (PostgreSQL views)
- [ ] Admin UI (SPA Svelte)
- [ ] Rate limiting

### Phase 3 - Features Avancees (Semaines 13-18)

- [ ] Full-text search (tsvector PostgreSQL)
- [ ] Backup/restore API
- [ ] Logs API + statistics
- [ ] Batch API (operations transactionnelles)
- [ ] Systeme de plugins
- [ ] Multi-tenancy
- [ ] Row-level security
- [ ] Benchmarks de performance
- [ ] Guides de deploiement production

---

## Compatibilite API PocketBase

PPBase vise une compatibilite maximale avec l'API REST de PocketBase. Les clients ecrits pour PocketBase devraient fonctionner avec PPBase avec des modifications minimales.

### Endpoints Phase 1 (32 endpoints)

| Methode | Endpoint | Description |
|---------|----------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/settings` | Lire les parametres |
| PATCH | `/api/settings` | Modifier les parametres |
| GET | `/api/admins` | Lister les admins |
| POST | `/api/admins` | Creer un admin |
| GET | `/api/admins/{id}` | Voir un admin |
| PATCH | `/api/admins/{id}` | Modifier un admin |
| DELETE | `/api/admins/{id}` | Supprimer un admin |
| POST | `/api/admins/auth-with-password` | Login admin |
| POST | `/api/admins/auth-refresh` | Rafraichir token admin |
| GET | `/api/collections` | Lister les collections |
| POST | `/api/collections` | Creer une collection |
| GET | `/api/collections/{id}` | Voir une collection |
| PATCH | `/api/collections/{id}` | Modifier une collection |
| DELETE | `/api/collections/{id}` | Supprimer une collection |
| PUT | `/api/collections/import` | Importer des collections |
| DELETE | `/api/collections/{id}/truncate` | Vider une collection |
| GET | `/api/collections/{coll}/records` | Lister les records |
| POST | `/api/collections/{coll}/records` | Creer un record |
| GET | `/api/collections/{coll}/records/{id}` | Voir un record |
| PATCH | `/api/collections/{coll}/records/{id}` | Modifier un record |
| DELETE | `/api/collections/{coll}/records/{id}` | Supprimer un record |
| GET | `/api/files/{coll}/{record}/{filename}` | Telecharger un fichier |
| POST | `/api/files/token` | Token d'acces fichier |

---

## Decisions Cles

| Decision | Choix | Alternatives | Raison |
|----------|-------|-------------|--------|
| Framework | FastAPI | Flask, Django | Async natif, OpenAPI, Pydantic |
| DB Driver | asyncpg | psycopg3 | Plus rapide, meilleur support types PG |
| Tables dynamiques | Colonnes physiques | JSONB pur | Performance queries, indexes natifs |
| Schema storage | JSONB dans _collections | Table _fields separee | Lecture atomique, compat PocketBase |
| Filter parser | Lark (EBNF) | pyparsing | Grammaire formelle, SQL injection safe |
| IDs | 15-char alphanum | UUID, ULID | Compatibilite PocketBase |
| Passwords | bcrypt (passlib) | argon2 | Compatibilite PocketBase |
| Admin UI | Svelte SPA | React, Vue | Taille bundle, compat PocketBase |

---

## Pour Commencer le Developpement

```bash
# 1. Creer l'environnement
cd ppbase
python -m venv .venv
source .venv/bin/activate

# 2. Installer les dependances
pip install -e ".[dev]"

# 3. Lancer PostgreSQL (Docker)
docker run -d --name ppbase-pg \
  -e POSTGRES_DB=ppbase \
  -e POSTGRES_USER=ppbase \
  -e POSTGRES_PASSWORD=ppbase \
  -p 5432:5432 postgres:17

# 4. Lancer PPBase
ppbase serve --db postgresql+asyncpg://ppbase:ppbase@localhost:5432/ppbase

# 5. Lancer les tests
pytest tests/ -v
```

---

*Ce document est la reference principale du projet. Consultez les documents detailles (01-05) pour les specifications completes.*
