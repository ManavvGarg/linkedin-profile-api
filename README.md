# LinkedIn Profile API

An HTTP API that takes a LinkedIn profile URL and returns the profile as structured JSON.

```bash
curl "https://<your-deployment>/v1/profile?url=https://www.linkedin.com/in/williamhgates"
```

```jsonc
{
  "schema_version": "1.0",
  "source": "voyager",
  "request": { "resolved_public_id": "williamhgates", "cached": false, "duration_ms": 1840 },
  "provenance": { "experience": { "source": "voyager", "complete": true, "redacted": false } },
  "warnings": [],
  "profile": {
    "full_name": "Ada Lovelace",
    "headline": "Principal Engineer at Analytical Systems",
    "location": { "text": "London, England, United Kingdom", "country_code": "gb" },
    "summary": "Engineer working on distributed data systems…",
    "experience": [
      {
        "title": "Principal Engineer",
        "company": { "name": "Analytical Systems", "staff_count": 340 },
        "date_range": { "start": { "year": 2021, "month": 3 }, "end": null, "is_current": true }
      }
    ],
    "education": [ … ], "skills": [ … ], "certifications": [ … ], "languages": [ … ]
  }
}
```

---

## Table of contents

- [Quick start](#quick-start) · [Configuration](#configuration) · [API reference](#api-reference)
- [Approach](#approach-how-this-actually-works) — *the interesting part*
- [Schema design](#schema-design) · [Handling schema drift](#handling-schema-drift)
- [Deployment](#deployment) · [Known limitations](#known-limitations)
- [Legal and ethical considerations](#legal-and-ethical-considerations)

---

## Quick start

### Run without credentials

The API ships with a bundled fixture, so it starts and serves a complete response with no
LinkedIn account involved. Useful for reviewing the schema before deciding to point a real
session at it.

```bash
git clone https://github.com/<you>/linkedin-profile-api.git
cd linkedin-profile-api
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

FETCH_MODE=fixture uvicorn app.main:app --reload
```

```bash
curl "http://localhost:8000/v1/profile?url=ada-lovelace-demo" | jq
```

Interactive docs at <http://localhost:8000/>.

### Run against LinkedIn

```bash
cp .env.example .env
# Set LINKEDIN_LI_AT — see below.
uvicorn app.main:app
```

**Getting `li_at`:** log in to LinkedIn → DevTools → Application → Cookies →
`https://www.linkedin.com` → copy the `li_at` value.

> ⚠️ **`li_at` is a login, not an API key.** Anyone holding it *is* that account —
> able to read the inbox, send messages, and change the profile. It cannot be scoped
> down. Keep it in a secrets manager, never in the repository, and revoke it from
> **Settings → Sign in & security → Where you're signed in** when finished.
>
> Use a throwaway or secondary LinkedIn account. Automated traffic can get an account
> restricted, and that risk should not land on your primary professional identity.

Also set `LINKEDIN_USER_AGENT` to match the browser you copied the cookie from —
LinkedIn ties a session to the fingerprint it was created in, and a mismatch shortens
its life.

### Docker

```bash
docker compose up --build        # reads .env
```

### Tests

```bash
pytest              # 64 tests, no network access required
```

---

## Configuration

Every setting is an environment variable; `.env.example` documents them all.

| Variable | Default | Notes |
|---|---|---|
| `LINKEDIN_LI_AT` | — | Session cookie. The only real secret. |
| `LINKEDIN_JSESSIONID` | auto | Optional — see [CSRF](#the-csrf-token-is-not-a-security-control). |
| `LINKEDIN_USER_AGENT` | Chrome 131 | Must match the cookie's browser. |
| `FETCH_MODE` | `auto` | `live` · `fixture` · `auto` (live if a cookie is set). |
| `IMPERSONATE` | `chrome131` | TLS fingerprint. [Why this matters](#tls-fingerprinting). |
| `PROXY_URL` | — | Use `socks5h://`, never `socks5://`. |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | 2 / 5 | Jittered spacing between upstream calls. |
| `CACHE_TTL_SECONDS` | 86400 | [Load-bearing, not an optimisation](#caching-is-a-correctness-feature). |
| `API_KEYS` | — | Comma-separated. **Blank means the API is open.** |
| `ENABLE_GUEST_FALLBACK` | `false` | [Off for good reason](#the-logged-out-fallback). |

---

## API reference

### `GET /v1/profile`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | *required* | Profile URL or bare public id. |
| `refresh` | bool | `false` | Bypass the cache. |
| `allow_guest_fallback` | bool | env | Fall back to the logged-out page on failure. |

`POST /v1/profile` takes the same fields as a JSON body.

**Accepted URL forms** — all resolve to the same profile:

```
https://www.linkedin.com/in/williamhgates          https://in.linkedin.com/in/williamhgates
https://www.linkedin.com/in/williamhgates?trk=…    linkedin.com/in/williamhgates
https://www.linkedin.com/comm/in/williamhgates     williamhgates
https://www.linkedin.com/sales/people/ACwAAA…,NAME_SEARCH,abcd
```

Search URLs and company pages are rejected with a specific message rather than a
generic 404, because they are the two most common paste mistakes.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | Liveness, mode, session state, cache size. **Never calls LinkedIn.** |
| `POST /v1/session/check` | Deliberately probe the session. Costs one upstream request. |
| `GET /v1/fixtures` | List bundled sample profiles. |
| `GET /` | Swagger UI. `GET /openapi.json` for the spec. |

`/v1/health` does not touch LinkedIn on purpose: a health check that spends rate budget
eventually causes the outage it exists to detect.

### Errors

```json
{ "error": { "code": "session_challenged",
             "message": "LinkedIn redirected to a verification challenge.",
             "remediation": "The cookie is fine — open LinkedIn, complete the check…" } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `invalid_profile_url` | 400 | Not a member profile URL. |
| `profile_not_found` | 404 | No such profile. |
| `fixture_not_found` | 404 | Fixture mode, unknown sample. |
| `rate_limited` | 429 | LinkedIn is refusing this IP or session. |
| `session_missing` / `session_malformed` | 503 | Not configured, or a bad paste. |
| `session_expired` | 503 | Cookie was valid, no longer is. |
| `session_challenged` | 503 | **Cookie is fine** — LinkedIn wants verification. |
| `session_invalid` | 503 | Rejected outright, or a CSRF mismatch. |
| `upstream_blocked` | 502 | LinkedIn's `999` block. |
| `upstream_schema_drift` | 502 | LinkedIn changed shape. |

The five-way session taxonomy is deliberate. `expired` and `challenged` look identical
in a naive client but need opposite responses: one means re-authenticate, the other means
the credential is healthy and a human has to clear a checkpoint. Collapsing them is the
most common integration bug in this space.

---

## Approach: how this actually works

### The decision that shapes everything

There are two ways to get a LinkedIn profile, and the choice is not a matter of taste.

I started by measuring what a logged-out client can actually see. The answer is: **not
enough, and not by accident.** LinkedIn redacts profile data *server-side* for anonymous
clients. The values are not hidden with CSS — they are never sent. Here is the raw HTML
from a live profile:

```html
<div class="blurred-overlay experience-education__list">
  <h3>Gates Foundation</h3>
  <h4><p class="blur" aria-hidden="true">********</p></h4>   <!-- the job title -->
```

The same redaction appears in the page's JSON-LD:

```json
"jobTitle": ["******** *** ***", "****** ***** ** ********"],
"worksFor": [ {"name": "Microsoft"}, {"name": "********** ** *******"} ]
```

The masking preserves character counts and word boundaries exactly — `"Chairman and CEO"`
becomes `8/3/3` asterisks — so you can confirm the structure is intact while the content
is gone. I verified this on five profiles; every one returned exactly **12
`blurred-overlay` containers**. It is systematic, not incidental.

**So: about, job titles, position dates, skills, certifications and languages — most of
what this challenge asks for — cannot be obtained anonymously at any level of parsing
effort.** That settles the architecture. The API authenticates.

### It calls the internal API, not the page

Authenticated, the right move is LinkedIn's own **Voyager** API (`/voyager/api/…`) — the
private JSON API linkedin.com's frontend calls. This is also what the established
commercial tools do. PhantomBuster's documentation says so outright:

> "This Phantom does not 'visit' the profile, it extracts data using API calls. That
> means profiles will not show as 'viewed' inside LinkedIn."

Their own published throughput confirms it — their API-based scraper runs at **~1.8
seconds per profile** while their browser-based one takes **~30 seconds**. No headless
Chrome renders a LinkedIn profile in 1.8 s; that is an HTTP round trip. The risk budgets
differ just as sharply: they rate the API path at **1,500 profiles/day** against **80/day**
for the rendering path.

Calling the JSON API rather than parsing HTML means:

- **Structured data at the source.** Dates arrive as `{"year": 2021, "month": 3}`, not
  `"Jan 2021 - Present"`. Nothing to parse, nothing to get wrong.
- **No CSS selector churn.** HTML scrapers break on every redesign. The open-source
  precedent here is stark — one widely-used scraper's source is littered with comments
  like `// Issue #52: new UI`, `// Issue #128: new UI`.
- **Complete sections.** The rendered page lazy-loads; the API returns everything at once.
- **No profile view.** Voyager reads do not register as "viewed your profile", which is
  both less intrusive and less detectable.

### Things that are true but counter-intuitive

These cost real debugging time, so they are documented in code as well as here.

#### The blocker is header completeness, not IP reputation

The widely-repeated claim is that LinkedIn blocks datacenter IPs. That is true but
secondary — the *first* wall is much simpler. Measured from one residential IP, seconds
apart:

| Request | Result |
|---|---|
| Plausible `User-Agent` only | **999** (blocked) |
| Full Chrome header set (`sec-fetch-*`, `sec-ch-ua`, `upgrade-insecure-requests`) | **200** |
| Googlebot / Bingbot UA | **999** |

A believable UA alone is not enough; the `sec-fetch-*` and `sec-ch-ua` families are what
LinkedIn checks. Crawler UAs are explicitly refused, which kills the "pretend to be
Googlebot" trick. `app/session.py::browser_headers` sends the full set.

I also tested the popular advice that a Chrome **TLS fingerprint** is what gets you in.
It is not, on this path: `curl_cffi` with `impersonate="chrome131"` but sparse headers
returned **999**, while plain `curl` with complete headers returned **200**.

#### TLS fingerprinting

That said, this client *does* impersonate Chrome's TLS fingerprint — for a different
reason. On **authenticated** Voyager calls, a non-browser TLS fingerprint is reported to
invalidate `li_at` outright, on the first request. That failure is expensive and its
symptom (suddenly logged out) looks nothing like its cause. Impersonation here is a
safety control, not a way past a block.

#### 403 does not mean the session died

Voyager's status codes do not mean what they look like:

| Signal | What it actually means |
|---|---|
| `403` | The `csrf-token` header does not match the `JSESSIONID` cookie. **Not** a dead session. |
| `302 → /uas/login` | The only reliable signal that the session is dead. |
| `999` | Edge block. Only ever seen unauthenticated. |
| `410` | The endpoint was retired. |

Because session death is a *redirect*, the client sets `allow_redirects=False`. Follow the
redirect and you get a `200` containing a login page, so an auth failure disguises itself
as a parsing bug.

#### The CSRF token is not a security control

LinkedIn only checks that the `csrf-token` header *equals* the `JSESSIONID` cookie. It
never validates the value. A client can mint its own — which is why `LINKEDIN_JSESSIONID`
is optional and the app generates one.

#### The endpoint everyone documents is dead

Nearly every tutorial and library uses:

```
GET /voyager/api/identity/profiles/{public_id}/profileView
```

It was **retired around September–October 2025** and now returns `{"status": 410}` — with
no `message` key, which is why libraries in that lineage crash with `KeyError: 'message'`
rather than failing cleanly. The most-starred Python implementation went private in March
2025 and is unmaintained.

This service uses the live replacement:

```
GET /voyager/api/identity/dash/profiles
    ?q=memberIdentity&memberIdentity={public_id}&decorationId=…FullProfileWithEntities-93
```

#### `included[]` is not in display order

Voyager answers in Rest.li's normalized form: `data` holds URN *references* and
`included` is a flat bag of every entity involved. The trap is that `included` is **not**
in display order. Filter it by `$type` and you get the right entities in the wrong
sequence — jobs attached to the wrong employers — **with no error raised**. The output
looks entirely plausible and is wrong.

Order has to come from the URN sequence in the referring field. `app/normalize.py`
resolves `*profilePositions` first and only falls back to `included` order.
`tests/test_normalize.py` pins this with a fixture whose `included` array is deliberately
shuffled.

A related trap: not every entity is keyed by `entityUrn` — paging metadata uses `$id`, so
an index built on `entityUrn` alone silently drops entries.

#### Profile images expire

A LinkedIn image is a `rootUrl` plus artifacts contributing one path segment per size;
neither half is usable alone. The reconstructed URLs are **signed and short-lived** —
each artifact carries an `expiresAt`, typically minutes away. The API returns every
variant with its expiry. **Mirror the bytes if you need them to persist; never store the
URL as if it were stable.**

#### Caching is a correctness feature

The cache is not there for speed. LinkedIn's rate budget is the scarcest resource in the
system, and serving repeat lookups from memory is what keeps the service alive under real
traffic. See the measured numbers in [Known limitations](#known-limitations).

#### Cookie rotation

LinkedIn rotates `li_at` server-side. A deployment that keeps replaying the originally
pasted value drifts out of date. The client reads the rotated cookie off each response and
prefers it, which lets a session outlive the cookie you actually pasted.

---

## Schema design

Four decisions, each a reaction to how the incumbent scrapers get this wrong.

**1 · Nested, not flattened.** PhantomBuster's CSV keeps the current and one previous
role (`linkedinJobTitle` / `linkedinPreviousJobTitle`), so a ten-job career silently
becomes two. Their own docs concede full history is JSON-only and suggest users
*"paste the JSON into a free online JSON to CSV converter."* Flattening is an export
concern and does not belong in the API contract.

**2 · Structured dates.** `{"year": 2021, "month": 3}`, never `"Jan 2021 - Present"`.
LinkedIn hands us structured dates; stringifying them destroys information for free and
forces every consumer to write the same brittle parser. Partial dates stay partial — a
year with no month is common on education, and inventing January would be a lie.

**3 · Explicit nulls.** PhantomBuster deletes falsy values from its output, which makes
column sets vary row to row and drops legitimate zeroes. Here, absent means *"LinkedIn had
no value"*, and a skill with **0 endorsements** keeps its `0`. There is a test for exactly
this.

**4 · Per-section provenance.** Every response carries a `provenance` map:

```json
"provenance": {
  "skills": { "source": "guest_page", "complete": false, "redacted": true,
              "note": "LinkedIn withholds this from logged-out clients." }
}
```

Without it, a caller cannot distinguish a genuinely empty section from one that was gated,
truncated, or failed to parse — and those need completely different handling. An empty
section from an authenticated call is also flagged `complete: false`, because that is how
upstream drift first shows up.

---

## Handling schema drift

Voyager is undocumented, unversioned, and changes without notice. This service is built to
degrade honestly rather than to pretend otherwise.

**Endpoint versions are discovered, not hardcoded.** Voyager's `decorationId` carries a
version suffix that LinkedIn rotates — `FullProfileWithEntities-91` and `-93` were both
live in different codebases simultaneously. Hardcoding one is why scrapers break on a
Tuesday. `VoyagerClient.discover_versions` reads the current value out of an authenticated
page's own JavaScript, caches it, and falls back to a known-good list only if discovery
fails. LinkedIn's own frontend necessarily contains the version it is calling, which makes
the page the most reliable source available.

**Drift is loud.** A payload we do not recognise raises `upstream_schema_drift` rather
than returning a half-empty profile. A silently-empty `experience` array is far more
damaging than an error, because it looks like a person with no jobs.

**Section parsing is isolated.** One malformed section yields a warning and an empty list,
never a 500. Nine good sections plus a warning beats an exception.

---

## Deployment

Any container host works. Configs for two are included.

**Render** — push the repo, then New → Blueprint. `render.yaml` declares the service and
marks every secret `sync: false` so it is set in the dashboard, never committed.

**Fly.io**

```bash
fly launch --no-deploy
fly secrets set LINKEDIN_LI_AT="…" API_KEYS="…"
fly deploy
```

`min_machines_running = 1` is deliberate — the cache lives in process, and a cold start
throws away the thing protecting your rate budget.

**Before going public**, set `API_KEYS`. Blank means open, which is fine locally and
careless on the internet: an unauthenticated deployment lets anyone spend your LinkedIn
session's rate budget and get the underlying account restricted.

### The datacenter egress caveat

LinkedIn's `www` host refuses **logged-out** requests from cloud IP ranges outright,
regardless of headers. Authenticated Voyager calls are a different path and generally work
from a datacenter, but if a deployment sees `upstream_blocked`, route it through a
residential proxy via `PROXY_URL`.

Use `socks5h://`, **not** `socks5://`. The `h` resolves DNS at the proxy; without it DNS
resolves locally and can return an address the proxy cannot route — and that failure looks
exactly like being blocked, which is an unpleasant thing to debug.

---

## Known limitations

Stated plainly, because most of these are properties of LinkedIn rather than bugs to fix.

### Rate limits are the binding constraint

Measured directly during development, from a clean residential IP, no proxy:

> **Roughly 5–10 logged-out profile fetches, then HTTP 999 for over two hours.**
> The block is **IP-scoped** — every country subdomain refused simultaneously — and
> **path-scoped**: `/robots.txt` and `/jobs-guest/` kept returning 200 throughout.
> **Backing off does not clear it.** A byte-identical request that succeeded twenty
> minutes earlier failed after the block landed.

Authenticated limits are far more generous but still real. PhantomBuster rates the API
path at **1,500 profiles/day**; another vendor advises **500/day per account**. Nobody in
the public record has a dated capture of a `429`, so treat every published figure as an
estimate. One concrete data point: ~1,200 records in a few hours got a *fresh* account
banned. New accounts are watched more closely than aged ones.

**Practical implications:** the cache is not optional; sustained volume needs multiple
sessions and rotating egress IPs; and retry-with-backoff is the wrong instinct here —
once blocked, waiting does not help.

### What the API cannot return

- **Contact details** — email, phone, and personal websites are visible only for 1st-degree
  connections, and this service does not request them. Commercial tools that "find" emails
  are inferring them from name and company domain, not extracting them.
- **Out-of-network profiles** may come back as LinkedIn's anonymised placeholder rather
  than a real member. The API flags this in `warnings` rather than returning a phantom
  profile.
- **Recommendations, endorsering members, and activity/posts** are not implemented. They
  need extra round trips against the same rate budget; the challenge did not ask for them.
- **Sales Navigator and Recruiter fields** need an entitled account.

### Structural

- **The cache is in-process**, so replicas do not share it and each spends its own budget.
  Fine for a single instance; scaling out means Redis. `app/cache.py` is deliberately
  narrow enough to make that a drop-in change.
- **One session per deployment.** A production system would pool sessions and rotate them.
  The `LinkedInSession` type is built for that; the pool is not implemented.
- **Voyager can change tomorrow.** This is a private API with no compatibility promise.
  Version discovery and loud drift errors reduce the blast radius; they do not eliminate it.
- **The bundled fixture is synthetic.** Real profile data would mean shipping a real
  person's personal information in a public repository. The *shape* is faithful to a live
  Voyager response, including the deliberately shuffled `included` array.

### The logged-out fallback

`ENABLE_GUEST_FALLBACK` exists and is **off by default**. It returns name, headline,
location, photo, follower count, first employer and first school — with titles, dates,
about, skills and certifications absent, and `redacted: true` in the provenance. It also
burns the IP budget above. Enable it only where partial data genuinely beats an error.

---

## Legal and ethical considerations

Not legal advice. This was built for a hiring exercise; anyone running it in production
should take their own advice.

**Scraping public pages is not a computer-crime problem.** In *hiQ Labs v. LinkedIn*, the
Ninth Circuit held that scraping publicly available data does not violate the Computer
Fraud and Abuse Act, since public pages involve no "authorization" to circumvent.

**But hiQ still lost.** The CFAA holding and the case outcome are different things, and
they are widely conflated. hiQ was bound by LinkedIn's User Agreement — it had registered
accounts and bought subscriptions — and it used **fake accounts to reach
password-protected pages**. It settled in 2022 for **$500,000** plus an injunction
requiring it to delete the collected data and the code. The exposure was **breach of
contract**, not hacking.

**LinkedIn's User Agreement is unusually broad**, and the distinction matters. When Meta
sued Bright Data over logged-out scraping, Bright Data won because Meta's terms governed
*"your use"* and Meta had **deleted** the clause binding non-registrants. LinkedIn never
deleted its equivalent: § 1.1 binds anyone *"accessing or using our Services"*, registered
or not. § 8.2 separately prohibits automated access, and its current wording reaches
parties who *"develop, support"* such tools, not just those who run them.

**This project's posture.** It uses a real account's own credential rather than a pool of
fake accounts, which is the fact that distinguished the enforcement actions above — the
Proxycurl shutdown (permanent injunction, July 2025) and the ProAPIs suit both turned on
industrial-scale fake-account operations. That is a materially better position than those
defendants held. It is **not** a defence against breach of contract, because using
`li_at` means you accepted the User Agreement.

**Privacy law applies regardless.** Profile data is personal data under GDPR and CCPA, and
there is no "publicly available" exemption under GDPR. The Dutch DPA's position is that
commercial scraping cannot rest on legitimate interest at all. If you process EU residents'
data you need a lawful basis, and scraped data does not come with one.

**Practical guidance.** Use a secondary account. Keep volume low. Cache aggressively —
the cheapest request is the one you do not make. Do not collect contact details. Honour
deletion requests. If you need this at commercial scale, licensed providers exist, and the
reason they cost money is that they carry this risk for you.

---

## Project layout

```
app/
  main.py        FastAPI routes, error handling, API-key auth
  service.py     Orchestration: cache -> Voyager -> optional guest fallback
  voyager.py     Voyager client, version discovery, status-code semantics
  normalize.py   Rest.li included[] graph -> the public schema
  guest.py       Logged-out fallback, with redaction detection
  session.py     Cookie/header construction, rotation, throttling
  models.py      The response schema
  resolve.py     URL -> public id
  cache.py       TTL cache
  errors.py      Typed errors with remediation text
  fixtures/      Synthetic Voyager payload for offline demos
tests/           64 tests, no network required
```

## License

MIT. See [LICENSE](LICENSE).
