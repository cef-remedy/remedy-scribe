# Graph Report - .  (2026-08-18)

## Corpus Check
- Corpus is ~31,957 words - fits in a single context window. You may not need a graph.

## Summary
- 380 nodes · 762 edges · 30 communities (24 shown, 6 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 49 edges (avg confidence: 0.57)
- Token cost: 0 input · 159,998 output

## Community Hubs (Navigation)
- Core Config & ASR Interface
- Backend Dependency Manifest
- Note Editing & Signing Routes
- Migrations & Audit Log
- Mobile App Dependencies
- Patient Matching Routes
- Auth Deps & Consent Routes
- Auth Login & Initial Migration
- Expo App Config
- Encounter Lifecycle Routes
- Project Docs & Tech Stack Decision
- PHI Field Encryption
- Mobile TypeScript Config
- Mobile App Entry
- Deferred Scope Items
- Android Icon Background Asset
- Android Icon Foreground Asset
- Android Monochrome Icon Asset
- Mobile App Icon Asset
- Splash Screen Placeholder

## God Nodes (most connected - your core abstractions)
1. `Clinician` - 27 edges
2. `get_settings()` - 20 edges
3. `Base` - 18 edges
4. `NoteStatus` - 17 edges
5. `Note` - 17 edges
6. `UUIDPrimaryKeyMixin` - 16 edges
7. `TimestampMixin` - 15 edges
8. `TranscriptSegment` - 13 edges
9. `match_patient()` - 13 edges
10. `EncryptedString` - 12 edges

## Surprising Connections (you probably didn't know these)
- `PHI_ENCRYPTION_KEY (Fernet)` --semantically_similar_to--> `PostgreSQL Database Decision`  [INFERRED] [semantically similar]
  README.md → docs/tech-stack.md
- `Stubbed ASR & Note-Generation Provider Interfaces` --semantically_similar_to--> `NoteGenerator Provider Interface Pattern`  [INFERRED] [semantically similar]
  README.md → docs/tech-stack.md
- `alembic==1.13.3` --references--> `Remedy Scribe README`  [EXTRACTED]
  apps/api/requirements.txt → README.md
- `SQLAlchemy==2.0.35` --conceptually_related_to--> `PostgreSQL Database Decision`  [INFERRED]
  apps/api/requirements.txt → docs/tech-stack.md
- `alembic==1.13.3` --conceptually_related_to--> `PostgreSQL Database Decision`  [INFERRED]
  apps/api/requirements.txt → docs/tech-stack.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **P0-8 Security Baseline Implementation Stack** — docs_tech_stack_auth_security_baseline, apps_api_requirements_python_jose, apps_api_requirements_passlib, apps_api_requirements_bcrypt, apps_api_requirements_pyotp, remedy_scribe_prd_security_baseline [INFERRED 0.85]
- **Remedy Scribe Local Dev Infrastructure Stack** — infra_docker_compose_postgres_service, infra_docker_compose_redis_service, infra_docker_compose_minio_service, infra_docker_compose_api_service, infra_docker_compose_worker_service [EXTRACTED 1.00]
- **Celery Async Transcription-to-Note Pipeline Chain** — docs_tech_stack_celery_redis_pipeline, docs_tech_stack_note_generator_provider_interface, remedy_scribe_prd_asr_transcription, remedy_scribe_prd_ai_note_generation, remedy_scribe_roadmap_vendor_bakeoff_risk [EXTRACTED 1.00]

## Communities (30 total, 6 thin omitted)

### Community 0 - "Core Config & ASR Interface"
Cohesion: 0.08
Nodes (37): get_settings(), Application settings, loaded from environment variables (.env in dev). See…, Settings, health(), get, ASRProvider, ABC, One method, one contract, so a provider swap (see get_asr_provider) never… (+29 more)

### Community 1 - "Backend Dependency Manifest"
Cohesion: 0.06
Nodes (47): alembic==1.13.3, bcrypt==4.0.1 (pinned below 4.1), boto3==1.35.24, celery==5.4.0, cryptography==43.0.1, fastapi==0.115.0, httpx==0.27.2, passlib[bcrypt]==1.7.4 (+39 more)

### Community 2 - "Note Editing & Signing Routes"
Cohesion: 0.13
Nodes (33): edit_section(), get_note(), _get_note_or_404(), get, post, Session, P0-5: "Doctor can freely edit any section before signing; edits are tracked for…, Drives the P0-5 state machine one step at a time. Signing (to_status ==… (+25 more)

### Community 3 - "Migrations & Audit Log"
Cohesion: 0.14
Nodes (24): _add_custom_type_imports(), Autogenerate renders EncryptedString columns as…, run_migrations_offline(), run_migrations_online(), Base, Declarative base. Every model module must import this Base and be imported from…, AuditLog, P0-8: "Access and change logs retained and reviewable." One table, written… (+16 more)

### Community 4 - "Mobile App Dependencies"
Cohesion: 0.06
Nodes (31): dependencies, expo, expo-audio, expo-dev-client, expo-secure-store, expo-sqlite, expo-status-bar, react (+23 more)

### Community 5 - "Patient Matching Routes"
Cohesion: 0.14
Nodes (25): create(), match(), post, Session, P0-6: exact match links silently; near match needs a one-tap confirmation…, Creates a new record (P0-6: "no match creates a new record with name +…, PatientLookupRequest, PatientMatchResult (+17 more)

### Community 6 - "Auth Deps & Consent Routes"
Cohesion: 0.12
Nodes (21): get_current_clinician(), get_db(), Session, P0-8: role-based access control, need-to-know. Usage: `clinician: Clinician =…, require_role(), post, Session, P0-1: appends one row to the immutable consent ledger. Never updates a prior… (+13 more)

### Community 7 - "Auth Login & Initial Migration"
Cohesion: 0.14
Nodes (18): login(), post, Session, P0-8: multi-factor authentication for clinician access — password AND a valid…, create_access_token(), generate_mfa_secret(), hash_password(), Auth primitives and PHI field-level encryption. Covers P0-8 (security… (+10 more)

### Community 8 - "Expo App Config"
Cohesion: 0.09
Nodes (22): backgroundColor, backgroundImage, foregroundImage, monochromeImage, adaptiveIcon, predictiveBackGestureEnabled, expo, android (+14 more)

### Community 9 - "Encounter Lifecycle Routes"
Cohesion: 0.18
Nodes (20): confirm_upload(), link_patient(), list_loose_sessions(), get, post, Session, Get-or-create on upload_idempotency_key (P0-2: "an idempotency key that…, P0-6: "a persistent 'loose sessions' tray with a one-tap linking action" —… (+12 more)

### Community 10 - "Project Docs & Tech Stack Decision"
Cohesion: 0.31
Nodes (9): API Dev Dependencies (pytest, mypy, ruff), API Runtime Dependencies, Monorepo Repo Layout Decision, Tech Stack Decision Record, Remedy Scribe Local Dev Docker Compose Stack, Remedy Scribe README, Remedy Scribe PRD, product-plan-v0.1.md (external research artifact) (+1 more)

### Community 11 - "PHI Field Encryption"
Cohesion: 0.38
Nodes (4): EncryptedString, A String column that is Fernet-encrypted at rest. Requires…, Fernet, TypeDecorator

### Community 12 - "Mobile TypeScript Config"
Cohesion: 0.40
Nodes (4): compilerOptions, strict, extends, expo/tsconfig.base

### Community 15 - "Deferred Scope Items"
Cohesion: 0.67
Nodes (3): Deliberately Deferred Items (KMS, managed MQ, multi-tenant), PRD Non-Goals, Roadmap: Later (Post-pilot)

## Knowledge Gaps
- **62 isolated node(s):** `styles`, `name`, `slug`, `version`, `orientation` (+57 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Clinician` connect `Note Editing & Signing Routes` to `Migrations & Audit Log`, `Patient Matching Routes`, `Auth Deps & Consent Routes`, `Auth Login & Initial Migration`, `Encounter Lifecycle Routes`?**
  _High betweenness centrality (0.076) - this node is a cross-community bridge._
- **Why does `get_settings()` connect `Core Config & ASR Interface` to `Migrations & Audit Log`, `Auth Deps & Consent Routes`, `Auth Login & Initial Migration`, `Encounter Lifecycle Routes`, `PHI Field Encryption`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Why does `Note` connect `Note Editing & Signing Routes` to `PHI Field Encryption`, `Core Config & ASR Interface`, `Migrations & Audit Log`?**
  _High betweenness centrality (0.023) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Clinician` (e.g. with `Base` and `TimestampMixin`) actually correct?**
  _`Clinician` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `Base` (e.g. with `AuditLog` and `Clinician`) actually correct?**
  _`Base` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `NoteStatus` (e.g. with `EncryptedString` and `Base`) actually correct?**
  _`NoteStatus` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `Note` (e.g. with `EncryptedString` and `Base`) actually correct?**
  _`Note` has 6 INFERRED edges - model-reasoned connections that need verification._