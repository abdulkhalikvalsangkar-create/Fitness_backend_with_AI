# Flutter API — current status

This is the source of truth for the Flutter app team: every `action`, every
field it accepts, what the backend fills in automatically vs. what the app
must send, and where the current implementation is incomplete. It was written
by reading the actual handler code (`apps/api/actions.py`,
`packages/storage/repositories/*.py`, `packages/orchestrator/*.py`), not by
copying `API.md` — `API.md` and `flutter_jwt.md` are older and are missing or
wrong in several places called out below. Where they agree, this file just
confirms it; where they disagree, **this file is correct**, because it was
checked against the code on 2026-09-21.

Status tags used throughout:

| tag | meaning |
|---|---|
| ✅ | Live, working, safe to build against |
| ⚠️ | Live but with a real gap — read the note |
| ⛔ | Stub / not implemented — do not build UI that assumes it works |
| 🔒 | Admin-only, not for the app |

---

## 0. What changed vs. the older docs

Read this section first — it's the reason this file exists.

1. **`sync.profile` accepts more than `API.md` shows.** `API.md`'s example
   only has `display_name`, `date_of_birth`, `sex`, `height_cm`, `weight_kg`,
   `pregnancy_status`. It never mentions `age_band`, `goals` (list of
   strings), or `preferences` (list of strings) — all of which the backend
   accepts and actually uses (goals/preferences are rendered straight into
   what the assistant reads about the user). See §4.4.
2. **`date_of_birth` is write-only.** You can send it and it is stored, but
   nothing in the backend ever reads it back or derives `age_band` from it.
   If you want the assistant to know the user's age, you must send
   `age_band` yourself — `date_of_birth` alone does nothing. See §4.4.
3. **Health metric names are an allowlist, silently.** `sync.metrics[].metric`
   accepts *any* string with no validation — but only twelve exact names are
   ever surfaced back to the assistant (`build_vitals` in
   `packages/storage/repositories/health.py` filters everything else out
   before the user context is built). Send anything outside that list and it
   sits in the database, fully invisible to chat. Full list and the
   consequences in §4.4.
4. **Blood pressure does not go through `metrics[]`.** Send it via
   `sync.medical.systolic` / `sync.medical.diastolic`, not as metric entries
   named `"systolic"`/`"diastolic"` — those names aren't on the allowlist in
   point 3, so a metric-shaped BP reading is silently dropped.
5. **`upload` now returns OCR text and a ready-to-show reply.** As of this
   session, `upload` runs OCR + an LLM cleanup pass on every image/PDF and
   returns `extracted_text` and `response` per attachment (plus a top-level
   `response` for the common one-attachment case). `API.md` still shows the
   old response shape without these fields. See §4.3.
6. **A document attached in `chat` is now read as context automatically.**
   If a message includes an `attachment_id` from an earlier `upload` call and
   that attachment has OCR text, the backend feeds that text to the model as
   context for the *current* `chat` action — it no longer requires the
   message to be phrased in a particular way, and it no longer routes the
   turn into the product-scan pipeline just because an attachment is present.
   See §4.1 and §4.3.
7. **Restaurant scanning (`scan_type: "restaurant"`) is a stub.** Place
   resolution only works if you pass a `place_id` — a restaurant name alone
   always fails to resolve, and even a resolved place currently returns zero
   findings from all four evidence stages (regulatory, recalls, news,
   complaints — none are wired to a real data source yet). Don't build a
   "trust this result" UI around it yet. See §4.2.
8. **Ingredient rows can be `"draft"` (unreviewed).** A newly discovered
   ingredient is written to the database as `draft` and, depending on an
   admin-only setting not exposed to the app, may stay that way indefinitely.
   `ingredient_table` rows now need to be read for `review_status` /
   `review_label`, not just `hazard_level`. See §5.

---

## 1. Transport

```
Base URL   https://fitness.moveneticsdigital.com/
Method     POST /     everything (auth + every action)
           GET  /     unauthenticated health check
```

One endpoint. The JSON (or multipart) body's `action` field selects the
operation; `action` defaults to `"chat"` if omitted from a JSON body. A
multipart body with file parts and no `action` defaults to `"scan"`.

### Response envelope

Every response, success or failure, has the same outer shape:

```jsonc
// 2xx
{ "success": true, "request_id": "8885a099c83f4a67", "action": "chat", /* action fields */ }

// 4xx / 5xx
{ "success": false, "error": "human-readable reason", "request_id": "..." }
```

`X-Request-Id` is also a response header. Log it — it's the only way to
correlate a bug report with a server-side trace.

| status | meaning |
|---|---|
| 400 | bad body, unknown action, too many attachments, base64 attachment |
| 401 | auth failed (missing/expired/malformed token — the reason is never disclosed) |
| 403 | admin action called by a non-admin principal |
| 404 | job/attachment/FAQ not found |
| 413 | body or file over the size limit |
| 422 | biological_age_calculator only — data loaded but couldn't produce a result |
| 429 | rate limited (`Retry-After` header present) |
| 503 | biological_age_calculator only — module/dependency unavailable on the server |
| 504 | `scan` only — worker didn't finish in time; retry `job_status` with the returned job ids |
| 500 | server error — `error` is always the literal string `"internal error"` (except the bio-age action, which returns a more specific message; see §4.5) |

### Health check — ✅

```http
GET /
```

No auth required.

```jsonc
{
  "success": true,
  "status": "ok",                  // or "degraded"
  "env": "production",
  "database": { "connected": true, "server": "10.11.16-MariaDB", "name": "..." },
  "versions": { "prompt": "v1", "kb": "v1" },
  "providers": { "openai": true, "deepseek": true, "huggingface": true },
  "actions": ["chat", "scan", "upload", "..."],
  "config_problems": 0
}
```

`status: "degraded"` means the DB is unreachable or the server config has
problems. Treat anything other than `"ok"` as not-ready for your status page.

---

## 2. Authentication — ✅

Full contract lives in **`flutter_jwt.md`** — read that file for the request
policy (refresh-on-401, rotation, storage). This is the summary:

```
Firebase Google login
  -> Firebase ID token
  -> POST { action: "auth.firebase_exchange", firebase_id_token, device_id?, device_name?, platform?, app_version? }
  -> access_token (15 min) + refresh_token (30 days)
```

| action | requires | notes |
|---|---|---|
| `auth.firebase_exchange` | Firebase ID token, no backend JWT | mints the backend session |
| `auth.refresh` | refresh token, no backend JWT | rotates both tokens — replace both on every call |
| `auth.logout` | backend JWT + refresh token | `{"all_devices": true}` to log out everywhere instead |
| `auth.logout_all` | backend JWT | no body needed |

All four auth request bodies are `extra="forbid"` Pydantic models
(`packages/domain/auth.py`) — sending an unknown field is a 400, not ignored.

Every other action needs:

```http
Authorization: Bearer <access_token>
```

`401` never says *why* the token was rejected (missing/expired/malformed all
look identical) — that's deliberate, don't try to branch client logic on the
error text.

### Security rules (non-negotiable)

- **Never sign a JWT in the app.** `JWT_SECRET` lives only on the backend; a
  client that can mint its own token can impersonate any user.
- Never send the Firebase Admin service-account JSON to the app — it doesn't
  need it and never will.
- Never put a `user_id` in a request body to select whose data to read —
  identity comes only from the bearer token.
- Store `access_token`/`refresh_token` in platform secure storage, never in
  plain prefs or logs.
- HTTPS only in production.
- Tokens are HS256, `aud=fitness-api`, `iss=movenetics-api` — these are
  backend config (`JWT_AUDIENCE`/`JWT_ISSUER`), not something the app sets.

### Rate limits — ✅

30 requests/min and 500/day, per user, plus a per-IP ceiling at 3× the
per-minute limit. The limiter **fails open**: if it can't reach the database
the request is allowed rather than blocked, so a limiter outage never takes
the app down. `Retry-After` is set on 429s.

---

## 3. Consent — ✅, call this before `sync`

```jsonc
{ "action": "consent", "op": "grant", "scopes": ["profile", "vitals"] }
// op: "list" (default) | "grant" | "revoke"
```

```jsonc
{ "action": "consent",
  "granted": ["profile", "vitals"],
  "available": ["vitals", "nutrition", "labs", "activity", "location", "profile"] }
```

An unknown scope string is a 400 — `scopes` must come from `available`.

| scope | unlocks | current status |
|---|---|---|
| `profile` | name, age, sex, height, weight, goals, preferences, **and allergies/conditions/medications** (safety facts are read live, not from a cached aggregate, so a condition mentioned in chat two minutes ago is already visible) | ✅ |
| `vitals` | weight, sleep, heart rate and other §4.4 metrics | ✅ |
| `nutrition` | food logs | ✅ |
| `activity` | workouts | ✅ |
| `labs` | lab *values* (BMI, blood pressure, HbA1c, lipids) in the medical section — separate from the safety facts above, which live under `profile` | ✅ |
| `location` | — | ⛔ accepted and stored, but nothing in the backend currently reads or gates on it. Granting/revoking it has no observable effect yet. |

Without a scope, that section is withheld from the assistant's context and it
says so rather than guessing — it does not silently fall back to a stale
value.

> Revoking a scope invalidates the user's cached answers, because what the
> assistant is allowed to see changed.

`consent` also creates the user's row if it doesn't exist yet, so it's safe
to call first, before any `sync`. Calling `sync` before `consent` stores data
the assistant isn't yet permitted to read (harmless, just pointless).

---

## 4. Actions

### 4.1 `chat` — ✅ the main conversational turn

```jsonc
// request
{
  "action": "chat",
  "message": "how much protein should I eat?",
  "session_id": "optional; omit to start a new one",
  "attachments": [{ "attachment_id": "5095aa07...", "mime_type": "image/jpeg" }],  // optional
  "locale": "en",
  "jurisdiction": "IN"
}
```

| field | required | notes |
|---|---|---|
| `message` | effectively yes (empty string is accepted but produces a smalltalk-ish answer) | max 8000 chars |
| `session_id` | no | **persist this.** Omitting it starts a fresh conversation with no memory of prior turns. |
| `attachments` | no | list of `{attachment_id, mime_type}` handles from a prior `upload`, **or** raw multipart file parts sent directly on this same `chat` call (multipart `chat` requests are accepted — see §4.3) |
| `locale` | no | default `"en"` |
| `jurisdiction` | no | default `"IN"` — selects which regulatory tables apply to product analysis |
| `scan_type` | no, and you shouldn't set it on `chat` yourself | internal — this is how `handle_scan` reuses `handle_chat`; see §4.2 |

**What happens with `attachments` on a plain `chat` call (no `scan_type`):**
for each attachment_id, the backend checks whether OCR text is already stored
for it (from a prior `upload`, or from this same request if you attached raw
files). If text is found, it's prepended to your message as context and that
attachment is dropped from the turn — the model answers using the document
text, and the turn is **not** routed into the product/scan pipeline. An
attachment with no OCR text yet (nothing readable, or OCR still pending) is
left as-is and currently has no effect on a plain chat turn — there is no
"attach a photo to chat and get it product-analysed" behavior any more (see
point 6 in §0). If you want product analysis, call `scan` explicitly.

```jsonc
// response
{
  "success": true,
  "action": "chat",
  "turn_id": "3ad48bb6...",
  "session_id": "10762b7f...",     // KEEP THIS and send it on the next turn
  "message": "plain text, all blocks joined — use as a fallback only",
  "payload": {
    "blocks": [ /* see §5 — render these */ ],
    "citations": [],
    "disclaimers": [],
    "confidence": 0.82,
    "confidence_reason": null,
    "data_gaps": []
  },
  "route": { "label": "FAQ", "confidence": 0.95, "stage": "S1_EXACT" },
  "source": "faq",
  "cache": "MISS",
  "pending_jobs": [],
  "latency_ms": 412
}
```

`source` identifies who produced the final answer: `faq` (stored template),
`llm` (model-generated), or `system` (deterministic — smalltalk, safety
notice, or a completed background job's structured result). `route.stage` is
one of `S0_RULES`, `S1_EXACT`, `S2_EMBEDDING`, `S3_SEMANTIC_CACHE`, `S4_LLM`,
`FALLBACK`. `cache` is `L1`/`L2`/`L3`/`MISS`.

**Render `payload.blocks`, not `message`.** `message` is a flattened fallback
for notifications/accessibility; it loses the verdict badge, the ingredient
table structure, and `personal_flags`.

---

### 4.2 `scan` — ✅ product; ⛔ restaurant — analyse images, product or place

**Multipart only.** Base64 is rejected everywhere in this API.

```js
const fd = new FormData();
fd.append("action", "scan");
fd.append("scan_type", "product");            // or "restaurant"
fd.append("message", "can I eat this?");       // required context for restaurant; optional for product
fd.append("file", photoBlob, "label.jpg");     // repeat "file" for multiple frames
await fetch(BASE, { method: "POST", headers: { Authorization: `Bearer ${jwt}` }, body: fd });
```

| field | required | notes |
|---|---|---|
| `scan_type` | recommended | `"product"` or `"restaurant"`. If omitted, legacy behaviour applies: any attachment selects product analysis; text alone with a restaurant/review-intent phrasing selects restaurant analysis. **Always send it explicitly** — see the routing change in §0 point 6. |
| `file` | product: yes; restaurant: no (optional) | repeatable form field; also accepted under `files`/`image`/`images`/`attachment`/`attachments`. Several angles of one pack improve barcode confidence through multi-frame voting. |
| `message` | product: no; restaurant: **yes, in practice** | for restaurant, this is the only query text the background investigation gets — put the restaurant name/place here |
| `place_id` | restaurant only, and currently required for any result | see status note below |

Max **8 MB per file**, **5 files**, accepted MIME types: `image/jpeg`,
`image/png`, `image/webp`, `image/heic`, `application/pdf`.

**Product (`scan_type: "product"`) — ✅ fully implemented.** Barcode decode →
OCR → ingredient resolution → deterministic hazard rules → optional LLM
explanation layer. The request blocks until the scan job (and any
ingredient-research child jobs) finish, or `SCAN_WAIT_SECONDS` (default 120s)
elapses, in which case you get a `504` with the pending job ids to poll via
`job_status`. **Scans take 20–30 seconds** — set a client timeout of at least
60s and show real progress.

**Restaurant (`scan_type: "restaurant"`) — ⛔ not functionally ready.**
Concretely, from `packages/restaurant/analyzer.py`:
- Place resolution (`_resolve_place`) only succeeds if you pass `place_id`
  directly. A restaurant name in `message` alone (no Places provider is wired
  up yet) always fails to resolve, and the app receives:
  *"I couldn't identify a specific branch for '&lt;query&gt;', so I can't
  report anything reliable about it."*
- Even with a resolved `place_id`, all four evidence stages — regulatory
  records, recall notices, news, public complaints — are stubs that always
  return "no data, stage unavailable." There is currently no real source
  wired for any of them.
- There is no equivalent of the product pipeline's draft/publish review gate
  for restaurant findings (a setting for this exists at the database level —
  `review_status_control_of_restaurant` — but nothing yet uses it to hold
  back a result; see §5's review-status note).

Don't build restaurant-scan UI that promises real findings yet; it will
currently always report "no adverse findings" or "couldn't identify a
branch," never an actual hazard.

**Product** response shape is the `chat` envelope with `payload.blocks`
replaced by the finished analysis (see §5), plus scan convenience fields
lifted to the top level:

```jsonc
{
  "action": "scan",
  /* ...all chat fields... */
  "verdict": "Use with caution",       // omitted/absent for restaurant or unidentified products
  "reason": "...",
  "flag_count": 2,
  "ingredient_count": 14,
  "unresolved_count": 1,
  "scan_job_results": [ /* raw completed job result payload(s) */ ]
}
```

**Restaurant is different, and this is important: `payload.blocks` and
`message` are never updated with the restaurant findings.**
`_complete_scan_jobs` in `apps/api/actions.py` only swaps the finished job
result into `payload.blocks` when that result has a top-level `"blocks"`
key — the product-scan job produces one, the restaurant-investigation job
does not (it returns `{query, place_resolved, summary, findings,
no_adverse_findings, stages_completed, stages_unavailable,
review_status_control}` instead). So for a restaurant scan, `payload.blocks`
stays whatever the initial "still working on it" placeholder was, and
`message`/`verdict`/`reason`/`flag_count` etc. are **not** populated at all.
**Read the real result from `scan_job_results[0]`** — `summary` is the
ready-to-show sentence, `findings[]` is the structured list (each with
`kind`, `scope`, `summary`, `source`, `url`, `date`, `verified`,
`counts_towards_assessment`), and `no_adverse_findings` /
`stages_completed` / `stages_unavailable` tell you how much of the
investigation actually ran. Given the stub status above, expect
`no_adverse_findings: true` and every stage in `stages_unavailable` for the
foreseeable future.

The cPanel worker cron must be running for `scan` to ever return before the
timeout:

```cron
* * * * * cd /home/USER/fitness-api && /home/USER/virtualenv/fitness-api/3.12/bin/python -m apps.worker.worker --once >> /home/USER/worker.log 2>&1
```

---

### 4.3 `upload` — ✅ store a file, get OCR text and a ready reply

Use this to upload once and reference the handle later (in `chat` or `scan`),
or to get an immediate OCR summary without starting a full conversational
turn.

```js
const fd = new FormData();
fd.append("action", "upload");
fd.append("file", blob, "report.jpg");
```

```jsonc
// response
{
  "action": "upload",
  "attachments": [{
    "attachment_id": "5095aa07...",
    "mime_type": "image/jpeg",
    "size_bytes": 137783,
    "sha256": "661b173e...",
    "deduplicated": false,
    "extracted_text": "Blood glucose (fasting): 94 mg/dL\nHbA1c: 5.4%\n...",
    "response": "I've had a look at what you uploaded — it looks like a fasting blood glucose and HbA1c panel. Both values are within typical ranges..."
  }],
  "errors": [],
  "response": "I've had a look at what you uploaded — ..."   // convenience: first non-empty per-attachment response
}
```

What happens server-side, automatically, per file: OCR runs (an external OCR
service, cached by content hash), then — if OCR produced text — an LLM pass
cleans the raw OCR text (fixes scan artefacts, never invents values) and
drafts `response`, a short reply describing what the document contains.
**Both `extracted_text` and `response` are `null`/absent if OCR found nothing
readable or the file isn't the kind of document with text on it** (e.g. a
photo of a barcode) — this is normal, not an error; don't treat it as a
failed upload.

The stored `extracted_text` is what a later `chat` call automatically
surfaces as context if you reference the same `attachment_id` (§4.1).

`deduplicated: true` means those exact bytes were already stored for this
user — a free re-upload, not an error. One bad file doesn't sink the others:
it appears in `errors` while the rest still store.

Attachments expire and are deleted after `ATTACHMENT_TTL_DAYS` (default
**30 days**) — don't treat an `attachment_id` as permanent storage.

Then reference the handle in a later turn:

```jsonc
{ "action": "chat", "message": "what does my report say about my sugar levels?",
  "attachments": [{ "attachment_id": "5095aa07...", "mime_type": "image/jpeg" }] }
```

---

### 4.4 `sync` — ✅ push health data (with the gaps from §0)

```jsonc
{
  "action": "sync",
  "profile": {
    "display_name": "...",
    "date_of_birth": "1994-03-02",   // stored, but NEVER read back — see below
    "age_band": "25-34",             // send this if you want the assistant to know age at all
    "sex": "female",
    "height_cm": 168,
    "weight_kg": 61.5,
    "pregnancy_status": "none",      // free text column; no server-side enum, but see note below
    "goals": ["lose fat", "run a 10k"],
    "preferences": ["vegetarian", "no early-morning sessions"]
  },
  "metrics": [{ "metric": "weight", "measured_on": "2026-08-01", "value": 61.5, "unit": "kg", "source": "manual" }],
  "nutrition": [{ "consumed_on": "2026-08-01", "calories": 2100, "protein_g": 130 }],
  "activities": [{ "started_at": "2026-08-01T06:30:00Z", "activity_type": "run", "duration_min": 42 }],
  "medical": { "report_date": "2026-07-20", "bmi": 22.1, "systolic": 118, "diastolic": 76,
               "hba1c": 5.4, "conditions": ["PCOS"], "allergies": ["peanuts"], "medications": ["metformin"] }
}
```

```jsonc
{ "action": "sync", "written": { "profile": 1, "metrics": 1 }, "aggregate_job_id": "..." }
```

All four top-level sections (`profile`, `metrics`, `nutrition`, `activities`,
`medical`) are independently optional — send only what changed.

#### `profile` fields — accepted vs. actually usable

| field | type | accepted | read back by the assistant |
|---|---|---|---|
| `display_name` | string | ✅ | ✅ |
| `date_of_birth` | ISO date | ✅ stored in `user_profile.date_of_birth` | ⛔ **never read.** No code anywhere computes `age_band` from it. If you only send this, the assistant knows nothing about the user's age. |
| `age_band` | free string | ✅ | ✅ — used directly, e.g. `"age 25-34"` in the context summary. No enum is enforced server-side; pick a consistent format across the app (`"25-34"` or an exact age as a string both work; just be consistent) |
| `sex` | free string | ✅ | ✅ |
| `height_cm` | number | ✅ | ✅ |
| `weight_kg` | number | ✅ | ✅ |
| `pregnancy_status` | free string, `VARCHAR(32)` | ✅ | ✅. No server-side enum on this endpoint, but the chat-side auto-capture that extracts this from conversation only ever writes one of `pregnant` / `breastfeeding` / `trying` / `none` — send the same four values from the app for consistency. |
| `goals` | list of strings | ✅ (was missing from `API.md`) | ✅ — appears verbatim as "goals: ..." in what the model reads |
| `preferences` | list of strings | ✅ (was missing from `API.md`) | ✅ — same, "preferences: ..." |

Any other key inside `profile` is silently ignored (not an error, not stored)
— `UserRepository.upsert_profile` only recognises the columns above.

#### `metrics[]` — the canonical metric-name allowlist

```jsonc
{ "metric": "weight", "measured_on": "2026-08-01", "value": 61.5, "unit": "kg", "source": "manual" }
```

`sync` writes *any* `metric` string you send to the database with no
validation. But only these **exact** twelve names are ever surfaced back into
what the assistant can see or reason about (`KNOWN_METRICS` in
`packages/storage/repositories/health.py`, used to filter both the "latest
metrics" snapshot and every rolling-average/trend lookup):

```
recovery, strain, sleep, sleep_hours, rhr, hrv,
weight, steps, spo2, respiratory_rate, body_fat, vo2max
```

Notably **not** on this list, so don't send them as metrics — they either go
elsewhere or aren't tracked at all:
- **`height`, `waist`** — not tracked as metrics anywhere; height lives on
  `profile.height_cm` and there is no waist field at all currently.
- **`systolic`, `diastolic`** — send these under `medical.systolic` /
  `medical.diastolic` instead (see below), not as metrics.
- **`resting_hr`** — the metric name is `rhr`, not `resting_hr`. (Note: the
  chat-side auto-capture from conversation actually uses `resting_hr`
  internally when a user *mentions* their resting heart rate in a message —
  that's a separate, pre-existing inconsistency in the backend, not
  something the app needs to work around. From the app, always use `rhr`.)

`measured_on` (ISO date), `unit`, and `source` are optional; `source` is part
of the row's uniqueness — a wearable-sourced and a manually-entered value for
the same day are both kept, not overwritten. Sync is idempotent on
`(user, metric, measured_on, source)` — safe to re-send.

#### `nutrition[]`

```jsonc
{ "consumed_on": "2026-08-01", "calories": 2100, "protein_g": 130, "carbs_g": 220,
  "fat_g": 70, "fiber_g": 28, "sugar_g": 45, "sodium_mg": 2100, "water_ml": 2000 }
```

One row per day (`consumed_on`), upserted — re-sending the same date
overwrites, it doesn't add a second entry.

#### `activities[]`

```jsonc
{ "started_at": "2026-08-01T06:30:00Z", "activity_type": "run", "duration_min": 42,
  "distance_m": 8000, "calories": 410, "load": 62 }
```

`activity_type` is a free string (no enum) — pick consistent values across
the app since `weekly_volume_by_type` groups by the exact string.

`started_at` is required (`activity_session.started_at` is `DATETIME(3) NOT
NULL`) and is now parsed with `datetime.fromisoformat` after normalising a
trailing `Z` to `+00:00` — both `"2026-08-01T06:30:00Z"` and
`"2026-08-01T06:30:00.551571Z"` (any 1–6 fractional digits) are accepted;
MySQL rounds anything past millisecond precision. **Before this fix,
`started_at` was passed straight through to the SQL layer unparsed** — MySQL
doesn't accept the ISO-8601 `T`/`Z` syntax for an implicit string→DATETIME
conversion, so every `sync` call carrying an activity failed with a bare
`500 "internal error"` no matter how the timestamp was formatted, and a
missing `started_at` hit the `NOT NULL` constraint the same way. Both now
fail cleanly with a `400` naming `activities[].started_at` instead. If you
were seeing 500s on activity sync, this was it — no client-side change is
required, but the app can now also rely on a `400` (not a 500) for a
malformed or missing timestamp.

#### `medical`

```jsonc
{ "report_date": "2026-07-20", "bmi": 22.1, "systolic": 118, "diastolic": 76, "hba1c": 5.4,
  "lipids": {"ldl": 110, "hdl": 55}, "labs": {}, "flags": [],
  "conditions": ["PCOS"], "allergies": ["peanuts"], "medications": ["metformin"] }
```

Every `sync.medical` call inserts a brand-new `medical_report` row (it's not
an upsert), and the assistant only ever reads the single most recent row —
so whatever `allergies`/`conditions`/`medications` you send here becomes the
**whole** list the assistant sees, not a merge with what was sent before.
Send the complete current list every time, not just what changed. This is
different from the chat-side auto-capture, which only ever *adds* to the
existing list and never removes — so a manual edit through `sync` (which can
drop an item) should be a deliberate, explicit user action in the UI, not
something triggered from an inferred suggestion.

---

### 4.5 `biological_age_calculator` — ✅ biological age from ENMO CSV

**Multipart recommended**, one CSV file with an ENMO wearable timeseries.
JSON with a pre-stored `attachment_id` also works.

```js
const fd = new FormData();
fd.append("action", "biological_age_calculator");
fd.append("chronological_age", "45");
fd.append("gender", "male");
fd.append("return_features", "false");
fd.append("file", csvBlob, "enmo_sample.csv");
```

| field | required | value |
|---|---|---|
| `chronological_age` | ✅ | number, 0–120 |
| `gender` | ✅ | `male`/`M`/`female`/`F` |
| `return_features` | no | boolean, default `false` — set `true` to also get raw cosinor/nonparam/PA/sleep features |
| `file` (or a stored `attachment_id`) | ✅ | one CSV, timestamp + ENMO (mg) columns, common column names auto-detected |

Max **8 MB**, **1 file** (extras ignored, first used).

```jsonc
{
  "action": "biological_age_calculator",
  "predicted_biological_age": 47.83,
  "chronological_age": 45.0,
  "gender": "male",
  "biological_age_advance": 2.83,
  "cosinor_features": { "mesor": 35.6214, "amplitude": 28.4571, "acrophase": 3.1416 },
  "data_summary": { "days_covered": 7.25 }
  // + "features": {...} only when return_features=true
}
```

| status | meaning |
|---|---|
| 400 | input validation (age out of range, bad gender, missing/oversized/unreadable file) |
| 422 | CSV loaded but CosinorAge couldn't produce a prediction — insufficient coverage; retry with more/better data |
| 503 | biological-age module unavailable on this deploy (missing `cosinorage` dependency) |
| 500 | internal processing error, with a specific message and stage in `error` — not the generic `"internal error"` other actions return |

---

### 4.6 `context` — ✅ what the assistant currently knows

```jsonc
{ "action": "context" }
```

```jsonc
{
  "action": "context",
  "profile": { "display_name": "...", "age_band": "25-34", "goals": [...], "..." : "..." },
  "profile_version": 4,
  "consent": ["profile", "vitals"],
  "aggregate_versions": { "vitals": "12", "nutrition": "8" },
  "latest_metrics": [{ "metric": "weight", "value": 61.5, "unit": "kg", "measured_on": "2026-08-01" }]
}
```

Useful for a "your data" debug screen. Note `latest_metrics` here is
**unfiltered** — it shows every metric name you've ever synced, including
ones outside the `KNOWN_METRICS` allowlist from §4.4. That's the one place in
the API where an off-list metric name is visible; it still won't be used by
chat.

---

### 4.7 `history` — ✅ recent turns in a session

```jsonc
{ "action": "history", "session_id": "...", "limit": 20 }   // session_id REQUIRED, or 400
```

```jsonc
{ "action": "history",
  "turns": [{ "role": "user", "content": "...", "created_at": "2026-08-05 12:01:33" }] }
```

---

### 4.8 `memory` — ✅ durable facts, user-visible and user-deletable

```jsonc
{ "action": "memory", "op": "list" }                                 // or omit op
{ "action": "memory", "op": "list", "kind": "goal" }                 // filter
{ "action": "memory", "op": "remember", "kind": "goal", "value": "run a 10k" }
{ "action": "memory", "op": "forget", "memory_id": 42 }
```

`op` is `list` | `remember` | `forget` — anything else is a 400.

```jsonc
{ "action": "memory", "memories": [{"id": 42, "kind": "goal", "value": "run a 10k", "confidence": 1.0, "updated_at": "..."}] }
{ "action": "memory", "stored": true }
{ "action": "memory", "forgotten": 1 }
```

**`kind` is a free string on this endpoint — the backend does not validate
it against a fixed list when called from the app.** For UI consistency
(grouping, icons, filters) use the same kinds the automatic chat-extraction
uses: `goal`, `dietary_restriction`, `disliked_exercise`, `preferred_exercise`,
`constraint`, `injury`, `schedule`, `equipment`, `motivation`. The assistant
captures facts from conversation automatically into these same kinds —
**build a screen for this.** Users need to see and delete what the assistant
has inferred about them; that's both a trust and a privacy requirement, not
an optional nicety.

---

### 4.9 `job_status` / `job_enqueue` — ✅ (client use is mostly `job_status`)

```jsonc
{ "action": "job_status", "job_id": "abc..." }     // one job
{ "action": "job_status" }                          // this user's recent jobs, limit default 20
```

```jsonc
{ "action": "job_status",
  "job": { "job_id": "...", "job_type": "chemical_research", "status": "succeeded", "attempts": 1, "result": {...} } }
```

`status`: `queued` | `running` | `succeeded` | `failed` | `cancelled`.

`job_enqueue` lets a caller start a job directly (`job_type` + `payload`).
**The app normally never needs this** — `chat`/`scan`/`sync` enqueue
background jobs for you automatically (ingredient research, context
aggregation, restaurant investigation, etc.), and `job_type: "etl_chemical_kb"`
is admin-only (403 otherwise). Only reach for `job_enqueue` if a specific
feature explicitly calls for starting one of these job types yourself:
`chemical_research`, `product_scan`, `deep_research`, `restaurant_investigation`,
`memory_summarise`, `profile_capture`, `context_aggregate`,
`embedding_backfill`, `ocr`.

---

### 4.10 `admin.*` — 🔒 not for the app

`admin.cache`, `admin.faq`, `admin.jobs`. All require `principal.is_admin`
(403 otherwise). Not used by the mobile client; listed here only so you don't
accidentally wire them up. Two settings relevant to what the app *displays*
(but not callable by the app) are covered in §5.

---

## 5. Rendering `payload.blocks`

Each block is `{ block_id, type, text, data }`. Render by `type`; fall back
to `text` for any type you don't handle yet, so a new block type never
breaks the UI. `block_id` is unique within one response, not globally — the
same id (e.g. `personal_1`) means something different depending on which
route produced it.

| type | contains | render as |
|---|---|---|
| `text` | `text` | paragraph |
| `faq_answer` | `text`, `data.faq_id`, `data.version` | paragraph |
| `hazard_badge` | `data.verdict` | **coloured badge — the headline** |
| `ingredient_table` | `data.rows[]` | table (see below) |
| `metric_card` | `data.metrics[]`, `data.trends` | cards / chart |
| `evidence_list` | `data.sources[]` | citation list |
| `product_unidentified` | `data.reason`, `data.action` | retry prompt |
| `job_pending` | `data.job_ids`, `data.poll_action` | "working on it" note |
| `safety_notice` | `text`, `data.kind` | prominent notice — see below |
| `action_prompt` | `data.flags[]` | call-to-action / highlighted list |

### Verdicts — exactly four values, a fixed enum from the rules engine

| `data.verdict` | suggested colour |
|---|---|
| `"Generally suitable"` | green |
| `"Use with caution"` | amber |
| `"Not recommended for you"` | red |
| `"Insufficient data"` | grey |

These come from the deterministic rules engine, never the model — safe to
`switch` on. `"Insufficient data"` means too little of the panel could be
assessed, not "safe" — render it grey/neutral, never green.

### `ingredient_table` rows

```jsonc
{
  "position": 10,
  "name": "hydrolysed groundnut protein",
  "raw": "hydrolysed groundnut protein",
  "recognised": false,
  "resolution": "unresolved",
  "confidence": 0.0,
  "review_status": "draft",
  "review_label": "unverified / pending review",
  "hazard_level": "unknown",          // none | low | moderate | high | unknown
  "iarc_group": null,
  "endocrine": false,
  "allergen": false,
  "banned_in": [],
  "restricted_in": [],
  "caveat": null,
  "rules_fired": [],
  "personal_flags": ["contains groundnut, which is the same allergen as your declared peanuts"]
}
```

**`personal_flags` is the most important field on the screen** — user-specific
and safety-critical. Sort flagged rows to the top and make them unmissable.

**`review_status`/`review_label`** (added since `API.md` was written): a
newly-discovered ingredient starts as `"draft"` and shows
`review_label: "unverified / pending review"` until it's published — either
by an admin, or automatically, depending on a database-only admin setting
(`review_status_control_of_product`, not reachable or settable from the app).
Show `review_label` when present — a "not yet reviewed" chip next to the row
— rather than treating a draft ingredient's `hazard_level: "unknown"` as
identical to a fully-assessed, genuinely-hazard-free one.

Distinguish `hazard_level: "unknown"` (nobody has assessed it, or it's still
in draft) from `"none"` (assessed, nothing found). Never render `"unknown"`
as safe.

### `safety_notice` blocks

Fixed, reviewed copy — never model-generated. `data.kind` is one of
`self_harm`, `emergency_symptom`, `disordered_eating` (the other
`SafetyFlagKind` values can block a turn without necessarily producing this
specific block). Render prominently; these already contain crisis-line
numbers for India plus a general international pointer — don't append your
own boilerplate on top.

---

## 6. Two flows to implement

### Onboarding

```
1. Firebase login -> auth.firebase_exchange -> store access/refresh tokens
2. POST { action: "consent", op: "grant", scopes: [...] }   <- before any data
3. POST { action: "sync", profile: {...}, medical: {...} }  <- include age_band, not just date_of_birth
4. POST { action: "chat", message: "..." }                  <- keep session_id
```

### Scan

```
1. capture 1-3 frames of the pack (barcode side + ingredients panel)
2. multipart POST: action=scan, scan_type=product, file (repeated)
3. render hazard_badge -> personal_flags -> ingredient_table (watch review_label)
4. if pending_jobs, show the job_pending note; poll job_status, don't hammer it
```

### Document upload + follow-up question (new)

```
1. multipart POST: action=upload, file=<photo of a report>
2. show response.response immediately (or attachments[0].response)
3. later, in the same session:
   POST { action: "chat", message: "...", attachments: [{attachment_id, mime_type}] }
   -> the document's OCR text is used as context automatically
```

---

## 7. Things that will bite you

| | |
|---|---|
| **Don't send base64.** | Rejected with a 400. Use multipart — repeat the `file` field for multiple images. |
| **Don't discard `session_id`.** | Every turn without it is a cold start with no memory. |
| **Don't render `message` instead of `payload.blocks`.** | You lose the verdict badge, the ingredient table, `personal_flags`, and `review_label`. |
| **Don't treat `"Insufficient data"` or `hazard_level: "unknown"` as safe.** | Both mean unassessed, not clean. |
| **Don't send `date_of_birth` and expect age personalisation.** | Send `age_band` too — nothing derives it for you. |
| **Don't invent metric names.** | Only the 12 names in §4.4 are ever read back by the assistant; anything else is stored but invisible. |
| **Don't send blood pressure as metrics.** | Use `sync.medical.systolic`/`diastolic`. |
| **Don't build a "trusted restaurant safety report" UI yet.** | `scan_type: "restaurant"` has no real data sources wired up (§4.2). |
| **Don't attach a product photo to `chat` expecting a scan.** | That routing was removed — call `scan` explicitly. |
| **Don't poll `job_status` in a tight loop.** | Rate limit is 30/min; chemical research takes 10–60s in the background. |
| **Don't sign JWTs in the app, or send a `user_id` field to select whose data to read.** | Identity comes only from the bearer token. |
| **Set a 60s+ client timeout for `scan`.** | 20–30s is normal for product scans. |
| **Log `request_id`.** | It's how anything gets diagnosed server-side. |

---

## 8. Quick reference

```
POST /   Authorization: Bearer <jwt>   (except auth.firebase_exchange / auth.refresh)

action                      purpose                              content-type   status
---------------------------  -----------------------------------  -------------  ------
auth.firebase_exchange      mint backend session from Firebase    json           ✅
auth.refresh                 rotate tokens                        json           ✅
auth.logout / logout_all     revoke session(s)                    json           ✅
chat                        conversational turn                  json           ✅
scan (scan_type=product)     analyse product images                multipart      ✅
scan (scan_type=restaurant)  analyse a restaurant/place             multipart      ⛔ stub
upload                      store a file; get OCR text + reply    multipart      ✅
sync                        push health data                      json           ✅ (see §4.4 gaps)
biological_age_calculator    biological age from ENMO CSV          multipart/json ✅
consent                     grant/revoke/list scopes               json           ✅
context                     what the assistant currently knows     json           ✅
history                     recent turns in a session              json           ✅
memory                      list/remember/forget durable facts     json           ✅
job_status / job_enqueue     background job state                  json           ✅
admin.cache / admin.faq / admin.jobs   admin-only                  json           🔒
```

Limits: 30 req/min, 500/day per user; 8 MB/file, 5 files/request, 2 MB JSON
body; attachments expire after 30 days.
