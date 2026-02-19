# Comparison: Auth, Realtime & Rules -- PocketBase vs PPBase

## 1. Authentication Flows

### 1.1 Password Authentication

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Auth with email + password | POST `/api/collections/{c}/auth-with-password` | `record_auth.py:174` - Full implementation | ✅ Implemented |
| Configurable identity fields (email, username, etc.) | `identityFields` in collection options | `record_auth_service.py:39-47` reads `passwordAuth.identityFields` | ✅ Implemented |
| `identityField` body param override | Allows specifying which field to match | `record_auth.py:203` reads `identityField` from body | ✅ Implemented |
| Password hashing (bcrypt) | bcrypt | `auth_service.py:17-21` bcrypt with 12 rounds | ✅ Implemented |
| Token response format (`{token, record}`) | Returns token + record data | `record_auth_service.py:160` returns same format | ✅ Implemented |
| `expand` query param on auth response | Expands relation fields in response | `record_auth.py:246-251` applies expand_records | ✅ Implemented |
| `fields` query param on auth response | Filters response fields | `record_auth.py:254-256` applies fields filter | ✅ Implemented |
| Per-collection token secrets | Each auth collection has its own `authToken.secret` | `auth_service.py:83-111` per-collection via `get_collection_token_config` | ✅ Implemented |
| Block `_superusers` from this endpoint | Superusers use admin auth | `record_auth.py:186-190` checks and returns 404 | ✅ Implemented |
| Enabled/disabled toggle per collection | `passwordAuth.enabled` option | Config exists in options but **not enforced** at the endpoint level | ⚠️ Partial |

### 1.2 OAuth2 Authentication

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Auth-methods listing endpoint | GET `/api/collections/{c}/auth-methods` | `record_auth.py:69-166` - Returns password + OAuth2 info | ✅ Implemented |
| Auth-with-OAuth2 code exchange | POST `/api/collections/{c}/auth-with-oauth2` | `record_auth.py:266-401` - Full flow | ✅ Implemented |
| PKCE support (S256) | Code verifier / code challenge | `oauth2_service.py:372-384` generates PKCE pairs | ✅ Implemented |
| Google provider | Yes | `oauth2_service.py:46-104` GoogleProvider class | ✅ Implemented |
| GitHub provider | Yes | `oauth2_service.py:107-182` GitHubProvider class | ✅ Implemented |
| GitLab provider | Yes | `oauth2_service.py:185-241` GitLabProvider class | ✅ Implemented |
| Discord provider | Yes | `oauth2_service.py:244-307` DiscordProvider class | ✅ Implemented |
| Facebook provider | Yes | `oauth2_service.py:310-369` FacebookProvider class | ✅ Implemented |
| Microsoft provider | Yes | Not implemented | ❌ Missing |
| Apple provider | Yes (with POST redirect support) | Not implemented | ❌ Missing |
| Twitter/X provider | Yes | Not implemented | ❌ Missing |
| Spotify, Twitch, Kakao, etc. | Various providers supported | Not implemented | ❌ Missing |
| OAuth2 redirect page (`/api/oauth2-redirect`) | Built-in redirect handler | Not found in PPBase routes | ❌ Missing |
| Mapped fields from provider to record | `mappedFields` in OAuth2 config | `oauth2_service.py:498-506` supports mapped fields | ✅ Implemented |
| Auto-create user on first OAuth2 login | Creates record + external auth link | `oauth2_service.py:494-528` link_or_create_oauth_user | ✅ Implemented |
| External auth records (`_externalAuths`) | System collection for OAuth2 links | Bootstrap creates it; used in oauth2_service | ✅ Implemented |
| Credential config from collection options | Per-collection provider credentials | `oauth2_service.py:407-448` checks options then env vars | ✅ Implemented |
| OAuth2 "all-in-one" flow via realtime | Uses SSE popup for seamless auth | Not implemented (requires realtime integration) | ❌ Missing |

### 1.3 OTP (One-Time Password) Authentication

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Request OTP endpoint | POST `/api/collections/{c}/request-otp` | No endpoint exists | ❌ Missing |
| Auth with OTP endpoint | POST `/api/collections/{c}/auth-with-otp` | No endpoint exists | ❌ Missing |
| `_otps` system collection | Stores OTP records | `bootstrap.py:236-280` creates the table | ⚠️ Partial (table exists, no API) |
| OTP email sending | Sends OTP code via email | Not implemented | ❌ Missing |
| OTP config (enabled, duration, length) | Per-collection OTP settings | `auth_service.py:73-77` defines default config only | ⚠️ Partial (config only) |
| Anti-enumeration (returns otpId even if user doesn't exist) | Privacy protection | Not implemented | ❌ Missing |
| Auto-verify email on successful OTP | Sets `verified=true` | Not implemented | ❌ Missing |

### 1.4 Multi-Factor Authentication (MFA)

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| MFA option toggle | `mfa.enabled` in collection options | `auth_service.py:69-72` defines default config only | ⚠️ Partial (config only) |
| `_mfas` system collection | Stores MFA sessions | `bootstrap.py` creates the table | ⚠️ Partial (table exists, no API) |
| 401 response with `mfaId` on first auth | Forces second factor | Not implemented | ❌ Missing |
| `mfaId` param on second auth call | Completes MFA flow | Not implemented | ❌ Missing |
| Support any 2 different auth methods | Password + OTP, Password + OAuth2, etc. | Not implemented | ❌ Missing |

### 1.5 Auth Token Management

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Auth refresh endpoint | POST `/api/collections/{c}/auth-refresh` | `record_auth.py:409-464` - Full implementation | ✅ Implemented |
| JWT HS256 tokens | Standard JWT with HS256 | `auth_service.py:114-128` uses PyJWT HS256 | ✅ Implemented |
| Token invalidation via `token_key` rotation | Changing password rotates token_key | `record_auth_service.py:464` rotates token_key on password reset | ✅ Implemented |
| Stateless tokens (no server-side storage) | Tokens not stored in DB | Same approach - verify on the fly | ✅ Implemented |
| Per-collection token duration | Configurable `authToken.duration` | `auth_service.py:41-42` per-collection durations | ✅ Implemented |

### 1.6 Email Verification

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Request verification endpoint | POST `/api/collections/{c}/request-verification` | `record_auth.py:472-508` | ✅ Implemented |
| Confirm verification endpoint | POST `/api/collections/{c}/confirm-verification` | `record_auth.py:516-556` | ✅ Implemented |
| Verification JWT with email claim | Purpose-specific token | `auth_service.py:204-218` creates verification token | ✅ Implemented |
| Anti-enumeration (always 204) | Returns 204 regardless | `record_auth.py:508` returns 204 always | ✅ Implemented |
| Email sending | Sends verification email | Delegates to `mail_service.send_verification_email` | ✅ Implemented |
| Sets `verified=true` on confirm | Updates record | `record_auth_service.py:335` updates verified column | ✅ Implemented |

### 1.7 Password Reset

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Request password reset endpoint | POST `/api/collections/{c}/request-password-reset` | `record_auth.py:564-599` | ✅ Implemented |
| Confirm password reset endpoint | POST `/api/collections/{c}/confirm-password-reset` | `record_auth.py:607-653` | ✅ Implemented |
| Password validation (min 8, max 72) | Length constraints | `record_auth_service.py:393-400` validates 8-72 | ✅ Implemented |
| `passwordConfirm` mismatch check | Validates passwords match | `record_auth_service.py:386-392` | ✅ Implemented |
| Token_key rotation on reset | Invalidates old tokens | `record_auth_service.py:463-464` new token_key + hash | ✅ Implemented |

### 1.8 User Impersonation

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Impersonate endpoint | POST `/api/collections/{c}/impersonate` | Not implemented | ❌ Missing |
| Superuser-only access | Only superusers can impersonate | Not implemented | ❌ Missing |
| Custom token duration | Configurable impersonation token TTL | Not implemented | ❌ Missing |
| Non-renewable tokens | Impersonation tokens cannot be refreshed | Not implemented | ❌ Missing |

### 1.9 Email Change

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Request email change endpoint | POST `/api/collections/{c}/request-email-change` | Not implemented | ❌ Missing |
| Confirm email change endpoint | POST `/api/collections/{c}/confirm-email-change` | Not implemented | ❌ Missing |
| `emailChangeToken` secret config | Per-collection token config | `auth_service.py:52-55` defines default config | ⚠️ Partial (config only) |

---

## 2. Realtime SSE

### 2.1 Connection Protocol

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| SSE connection endpoint | GET `/api/realtime` | `realtime.py:34-84` StreamingResponse | ✅ Implemented |
| `PB_CONNECT` event with `clientId` | Initial event on connection | `realtime.py:45` sends PB_CONNECT with clientId | ✅ Implemented |
| SSE `id:` field for SDK compatibility | Event IDs for reconnection | `realtime.py:45` and `realtime.py:63` include `id:` field | ✅ Implemented |
| 5-minute idle disconnect | Disconnects inactive clients | `realtime.py:57` uses 300s timeout, sends keepalive | ⚠️ Partial (sends keepalive instead of disconnect) |
| Auto-reconnection support | Client reconnects automatically | Server-side keepalive supports this; no disconnect signal sent | ⚠️ Partial |
| Proper SSE headers | `Cache-Control`, `Connection`, etc. | `realtime.py:79-83` sets proper headers including `X-Accel-Buffering: no` | ✅ Implemented |

### 2.2 Subscription Management

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Set subscriptions endpoint | POST `/api/realtime` | `realtime.py:87-166` | ✅ Implemented |
| Collection-wide subscription (`collection/*`) | Subscribe to all records in collection | `realtime.py:131-141` parses `collection/*` format | ✅ Implemented |
| Single-record subscription (`collection/id`) | Subscribe to specific record | `realtime.py:131-141` parses `collection/id` format | ✅ Implemented |
| Replace previous subscriptions on new POST | Replaces all subs atomically | **Not implemented** - PPBase adds subscriptions incrementally | ❌ Missing |
| Empty subscriptions = unsubscribe all | Clear all subs | `realtime.py:118-120` clears on empty array | ✅ Implemented |
| `clientId` validation (400 if missing) | Required field | `realtime.py:103-108` validates clientId | ✅ Implemented |
| Client not found (404) | Returns 404 for invalid clientId | `realtime.py:111-113` returns 404 | ✅ Implemented |
| `options` query parameter on topic | Attach query/header params to subscription | Not implemented | ❌ Missing |
| Authorization consistency check (403) | Previous and current auth must match | Not implemented (`TODO` comment at line 157-161) | ❌ Missing |

### 2.3 Event Format and Delivery

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Create events | SSE event on record create | `record_service.py:450-458` sends NOTIFY on create | ✅ Implemented |
| Update events | SSE event on record update | `record_service.py:680-688` sends NOTIFY on update | ✅ Implemented |
| Delete events | SSE event on record delete | `record_service.py:779-787` sends NOTIFY on delete | ✅ Implemented |
| Event data format `{action, record}` | Standard event payload | `realtime_service.py:132-135` matches format | ✅ Implemented |
| Event name matches subscription topic | SSE `event:` field = topic | `realtime_service.py:146-154` uses topic as event name | ✅ Implemented |
| PostgreSQL LISTEN/NOTIFY backend | Database-level event propagation | `realtime_service.py:164-267` uses asyncpg LISTEN | ✅ Implemented |
| Record data fetch on create/update | Fetches full record for event payload | `realtime_service.py:208-220` fetches record data | ✅ Implemented |

### 2.4 Authorization on Realtime Events

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| ListRule enforcement for `collection/*` | Checks listRule before sending event | `TODO` at `realtime.py:155-161` - not enforced | ❌ Missing |
| ViewRule enforcement for `collection/id` | Checks viewRule before sending event | `TODO` at `realtime.py:155-161` - not enforced | ❌ Missing |
| Auth token on subscription POST | Authorization header sets client auth | `realtime.py:123-127` extracts token, stores in subscription | ⚠️ Partial (stored but not used) |
| Superuser bypass | Superusers receive all events | Not implemented (no rule checking at all) | ❌ Missing |

---

## 3. Filters and API Rules

### 3.1 Filter Syntax

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Basic operators (`=`, `!=`, `>`, `>=`, `<`, `<=`) | Standard comparison | `filter_parser.py:79-86` all 6 operators | ✅ Implemented |
| LIKE operators (`~`, `!~`) | Case-insensitive contains | `filter_parser.py:88-91` maps to ILIKE | ✅ Implemented |
| ANY operators (`?=`, `?!=`, `?>`, `?>=`, `?<`, `?<=`) | Array element matching | `filter_parser.py:93-100` all 6 ANY operators | ✅ Implemented |
| ANY LIKE operators (`?~`, `?!~`) | Array element LIKE matching | `filter_parser.py:102-105` | ✅ Implemented |
| Logical AND (`&&`) | Conjunction | `filter_parser.py:27` grammar rule | ✅ Implemented |
| Logical OR (`\|\|`) | Disjunction | `filter_parser.py:25` grammar rule | ✅ Implemented |
| Parentheses grouping `(...)` | Expression grouping | `filter_parser.py:30` grammar rule | ✅ Implemented |
| Single-line comments (`// ...`) | Filter comments | `filter_parser.py:70` ignores `//` comments | ✅ Implemented |
| String literals (single/double quotes) | String operands | `filter_parser.py:44-48` both quote types | ✅ Implemented |
| Number literals (signed) | Numeric operands | `filter_parser.py:49` SIGNED_NUMBER | ✅ Implemented |
| Boolean literals (`true`, `false`) | Boolean operands | `filter_parser.py:51-53` | ✅ Implemented |
| NULL handling (`null`, `""`, `''`) | NULL comparisons | `filter_parser.py:54-56` + IS NULL/IS NOT NULL | ✅ Implemented |
| Auto-wrap `%` for LIKE | Adds wildcards automatically | `filter_parser.py:342-344` wraps with `%` | ✅ Implemented |
| Lark EBNF parser | Grammar-based parsing | `filter_parser.py:20-71` Earley parser | ✅ Implemented |
| Parameterized SQL output | Prevents SQL injection | `filter_parser.py:198-200` all values bound | ✅ Implemented |

### 3.2 Special Identifiers and Macros

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `@now` | Current datetime | `filter_parser.py:671` maps to `NOW()` | ✅ Implemented |
| `@request.auth.id` | Authenticated user ID | `filter_parser.py:673-683` resolves from context | ✅ Implemented |
| `@request.auth.collectionId` | Auth collection ID | `filter_parser.py:677` resolves from context | ✅ Implemented |
| `@request.auth.collectionName` | Auth collection name | `filter_parser.py:679` resolves from context | ✅ Implemented |
| `@request.auth.*` (arbitrary fields) | Any auth record field | `filter_parser.py:681-682` generic fallback | ⚠️ Partial (uses context dict, not full record) |
| `@request.body.*` / `@request.data.*` | Request body fields | `filter_parser.py:685-692` supports both v0.22/v0.23 syntax | ✅ Implemented |
| `@request.query.*` | Query string parameters | `filter_parser.py:693-698` resolves from context | ✅ Implemented |
| `@request.context` | Request context (default, oauth2, otp, etc.) | Not implemented | ❌ Missing |
| `@request.method` | HTTP method | Not implemented | ❌ Missing |
| `@request.headers.*` | Request headers | Not implemented | ❌ Missing |
| `@second`, `@minute`, `@hour`, `@weekday`, `@day`, `@month`, `@year` | Datetime part macros | Not implemented (falls back to empty string) | ❌ Missing |
| `@yesterday`, `@tomorrow` | Relative date macros | Not implemented | ❌ Missing |
| `@todayStart`, `@todayEnd`, `@monthStart`, `@monthEnd`, `@yearStart`, `@yearEnd` | Date range macros | Not implemented | ❌ Missing |
| `@random` (sort only) | Random sort order | `filter_parser.py:766` maps to `RANDOM()` | ✅ Implemented |
| `@rowid` (sort only) | Physical row order | `filter_parser.py:769-770` maps to `ctid` | ✅ Implemented |

### 3.3 Field Modifiers

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `:isset` modifier | Check if field was submitted | Not implemented | ❌ Missing |
| `:changed` modifier | Check if field was changed | Not implemented | ❌ Missing |
| `:length` modifier | Array field length check | Not implemented | ❌ Missing |
| `:each` modifier | Apply condition to each array element | Not implemented | ❌ Missing |
| `:lower` modifier | Case-insensitive comparison | Not implemented | ❌ Missing |

### 3.4 Functions

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `geoDistance(lonA, latA, lonB, latB)` | Haversine distance calculation | Not implemented | ❌ Missing |
| `strftime(format, [time-value, modifiers...])` | Date formatting in filters | Not implemented | ❌ Missing |

### 3.5 Cross-Collection References (`@collection.*`)

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| `@collection.name.field` | Reference fields from other collections | `filter_parser.py:238-252` generates EXISTS subquery | ✅ Implemented |
| Same-collection AND grouping | Multiple conditions in single EXISTS | `filter_parser.py:612-632` groups by collection name | ✅ Implemented |
| OR clause handling | Separate EXISTS per condition | `filter_parser.py:593-610` separate EXISTS in OR | ✅ Implemented |
| Cross-collection comparison | Both sides reference @collection | `filter_parser.py:376-388` inline EXISTS with two tables | ✅ Implemented |
| Collection aliases (`:alias` suffix) | Join same collection multiple times | Not implemented | ❌ Missing |

### 3.6 Relation Field Traversal

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| Single-level traversal (`relation.field`) | Access fields via relation | `filter_parser.py:256-272` + `_RelationCondition` class | ✅ Implemented |
| Multi-level traversal (`a.b.c`) | Deep relation chains | `filter_parser.py:263-267` raises "not yet supported" | ❌ Missing |
| Scalar vs array relation joins | `=` vs `ANY()` based on maxSelect | `filter_parser.py:519-523` checks max_select > 1 | ✅ Implemented |
| Relation AND grouping | Same relation grouped in single EXISTS | `filter_parser.py:634-645` groups by relation_key | ✅ Implemented |

### 3.7 API Rules Enforcement

| Feature | PocketBase | PPBase | Status |
|---------|-----------|--------|--------|
| 5 rules per collection (list, view, create, update, delete) | Standard rule set | All 5 defined in `collection.py:76-80` | ✅ Implemented |
| `null` = admin/superuser only (default) | Locked access | `rule_engine.py:32-35` checks `is_admin` | ✅ Implemented |
| `""` = public access | Open access | `rule_engine.py:38-39` returns True | ✅ Implemented |
| Expression = SQL WHERE filter | Dynamic access control | `rule_engine.py:44-46` returns expression string | ✅ Implemented |
| Admin/superuser bypasses all rules | Full admin access | `rule_engine.py:43-44` returns True for admins | ✅ Implemented |
| List: 200 empty on rule failure | Returns empty result set | `records.py:150-163` merges rule as filter | ✅ Implemented |
| View: 404 on rule failure | Record not found | `records.py:321-336` check_record_rule | ✅ Implemented |
| Create: 400 on rule failure | Bad request | `records.py:254-276` check_record_rule | ✅ Implemented |
| Update: 404 on rule failure | Record not found | `records.py:416-427` check_record_rule | ✅ Implemented |
| Delete: 404 on rule failure | Record not found | `records.py:485-496` check_record_rule | ✅ Implemented |
| 403 on null rule without admin | Forbidden | Implied by `check_rule` returning False | ✅ Implemented |
| `manageRule` for auth collections | Allows managing other users | `auth_service.py:79` defines default (null), but **not enforced** | ❌ Missing |
| Rule expression as additional WHERE clause | Rules act as data filters | `records.py:159-163` merges with user filter using `&&` | ✅ Implemented |
| `@request.auth.*` in rules context | Auth context for rule evaluation | `rule_engine.py:49-79` builds auth context | ✅ Implemented |

---

## Summary

### Auth Flows

| Category | Implemented | Partial | Missing | Total |
|----------|------------|---------|---------|-------|
| Password Auth | 9 | 1 | 0 | 10 |
| OAuth2 | 11 | 0 | 6 | 17 |
| OTP | 0 | 2 | 5 | 7 |
| MFA | 0 | 2 | 3 | 5 |
| Token Management | 5 | 0 | 0 | 5 |
| Email Verification | 6 | 0 | 0 | 6 |
| Password Reset | 5 | 0 | 0 | 5 |
| Impersonation | 0 | 0 | 4 | 4 |
| Email Change | 0 | 1 | 2 | 3 |
| **Total** | **36** | **6** | **20** | **62** |

### Realtime SSE

| Category | Implemented | Partial | Missing | Total |
|----------|------------|---------|---------|-------|
| Connection Protocol | 4 | 2 | 0 | 6 |
| Subscription Mgmt | 4 | 0 | 3 | 7 |
| Event Delivery | 7 | 0 | 0 | 7 |
| Authorization | 0 | 1 | 3 | 4 |
| **Total** | **15** | **3** | **6** | **24** |

### Filters and Rules

| Category | Implemented | Partial | Missing | Total |
|----------|------------|---------|---------|-------|
| Filter Syntax | 16 | 0 | 0 | 16 |
| Macros | 6 | 1 | 12 | 19 |
| Field Modifiers | 0 | 0 | 5 | 5 |
| Functions | 0 | 0 | 2 | 2 |
| @collection | 4 | 0 | 1 | 5 |
| Relation Traversal | 3 | 0 | 1 | 4 |
| Rules Enforcement | 12 | 0 | 1 | 13 |
| **Total** | **41** | **1** | **22** | **64** |

### Overall

| Area | ✅ Implemented | ⚠️ Partial | ❌ Missing | Coverage |
|------|---------------|-----------|-----------|----------|
| Auth Flows | 36 | 6 | 20 | 58% (63% incl. partial) |
| Realtime SSE | 15 | 3 | 6 | 63% (69% incl. partial) |
| Filters/Rules | 41 | 1 | 22 | 64% (66% incl. partial) |
| **Grand Total** | **92** | **10** | **48** | **61% (65% incl. partial)** |

---

## Critical Gaps (Priority Order)

1. **Realtime auth enforcement** -- Events are broadcast to all subscribers without checking ListRule/ViewRule. This is a security gap where unauthorized users can receive data they shouldn't see.

2. **OTP authentication** -- System table `_otps` exists but no API endpoints. Blocks MFA implementation since OTP is the most common second factor.

3. **MFA flow** -- System table `_mfas` exists but the 401-with-mfaId flow and second-factor verification are not implemented.

4. **Subscription replacement semantics** -- PocketBase replaces all subscriptions on each POST. PPBase adds incrementally, which breaks SDK behavior.

5. **manageRule enforcement** -- Config exists but the rule is never evaluated, meaning one user cannot be authorized to manage another user's data.

6. **Filter modifiers (`:isset`, `:changed`, `:length`, `:each`, `:lower`)** -- Used heavily in real-world PocketBase rules. Without these, many common access control patterns cannot be expressed.

7. **Datetime macros** -- Only `@now` works. The 12+ other datetime macros (`@yesterday`, `@todayStart`, etc.) are unimplemented, limiting date-based rules.

8. **`@request.context`** -- Cannot distinguish between different auth contexts (password, oauth2, otp, realtime) in rules.

9. **Multi-level relation traversal** -- Only single-hop (`author.name`) works. Deep chains (`post.author.team.name`) raise an error.

10. **User impersonation** -- No endpoint exists. Also blocks "API keys" pattern that PocketBase supports via superuser impersonation tokens.
