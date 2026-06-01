# Family Office Agent API

FastAPI service backing the **Mizan** (Portfolio Scorer & Analyst) and **Horizon** (Forward-Looking Signals & Event Impact) agents for the Nebelus family office demo.

- **Repo:** https://github.com/haydar-alsaad/family-office-api
- **Railway URL:** https://family-office-agent-api.up.railway.app
- **Health check:** https://family-office-agent-api.up.railway.app/health

---

## What this service does

- Loads JSON files from `data/` and seeds them into Supabase Postgres on cold boot (when `SEED_ON_BOOT=true` and the relevant table is empty).
- Exposes 8 read endpoints and 4 write endpoints for the Mizan and Horizon agents in Nebelus.
- Every agent write produces TWO inserts: the business write (e.g. `insights`) plus an entry in `agent_actions` with curated description text for the portal's Live Agent Activity drawer.

## What this service does NOT do

- It does not host the portal UI — that's the Lovable Cloud project.
- It does not handle chat message persistence — the Nebelus chat widget owns that.
- It does not authenticate (demo posture, no auth between Nebelus and Railway).

---

## Architecture

```
Lovable Portal (Lovable Cloud, React)
    ↕ Supabase JS client + Realtime
    │   reads: holdings, KPIs, insights, IC items, notes, watchlist
    │   listens: realtime on agent_actions (Live Activity drawer)
    │            realtime on insights (dashboard pin renders)
    │            realtime on ic_items, notes, watchlist_items (panels)
    │   writes (user-authored only): notes, watchlist_items
    │
Nebelus Chat Widget (embedded in portal)
    ↓ user message (e.g. "@horizon what's going on with oil?")
Nebelus Router Agent (workflow)
    ↓ routes by @mention or intent
Mizan  OR  Horizon
    ↓
Railway FastAPI ("main.py") ── https://family-office-agent-api.up.railway.app
    │   GET endpoints: reads from Supabase, enriches, returns to agent
    │   POST endpoints: writes to Supabase + logs agent_actions
    ↑
Supabase Postgres (single source of truth)
```

---

## Repo structure

```
family-office-api/
├── README.md
├── requirements.txt
├── Procfile
├── .env.example
├── main.py                        # FastAPI app: seed + GET + POST + agent_actions logging
├── docs/
│   └── 02_data_dictionary.md      # What each JSON file is and its Supabase mapping
├── build_data.py                  # Regenerator for /data (deterministic; seed=42)
└── data/                          # Version-controlled JSON baseline (read-side seed)
    ├── family_office.json
    ├── people.json
    ├── entities.json
    ├── portfolio_companies.json
    ├── portfolio_company_kpis.json
    ├── holdings.json
    ├── transactions.json
    ├── benchmarks.json
    └── ips_policies.json
```

---

## Endpoints

### GET (read-side, 8 endpoints)

| Path | Purpose |
|---|---|
| `GET /health` | Health check + table row counts |
| `GET /family-office` | The family office record |
| `GET /people` | All people, filterable by role / type |
| `GET /entities` | Legal entity structure |
| `GET /portfolio` | **Workhorse.** Family office + holdings + opcos + IPS + benchmarks + top-5 concentrations in one call |
| `GET /portfolio-company/{id}` | Single opco + 24-month KPI history + CEO + entity |
| `GET /holdings` | Filterable holdings list (asset class, sub class, status, geography) |
| `GET /transactions` | Recent transactions (last 90 days), filterable |

### POST (agent-only, 4 endpoints)

| Path | Purpose |
|---|---|
| `POST /insight` | Agent creates analysis output. Renders in dashboard if `pinned_to_dashboard=true` |
| `POST /ic-item` | Agent flags something for the IC queue |
| `POST /note` | Agent adds a note attached to a holding / company / entity / general |
| `POST /watchlist-item` | Agent adds something to the watchlist |

**Every POST does two inserts**: the business write, plus an `agent_actions` entry with a curated `description` string that the Live Agent Activity drawer renders directly.

All POSTs require a `source_agent` field set to `"mizan"` or `"horizon"`. The Nebelus router supplies this; the agent never sets it inconsistently.

---

## Supabase schema needed

The schema doc is the next deliverable (`docs/03_supabase_schema.md`). Brief summary of tables:

**Seeded from `/data` (read-side):**
- `family_office` (1 row)
- `people` (12 rows)
- `entities` (10 rows)
- `portfolio_companies` (3 rows)
- `portfolio_company_kpis` (72 rows: 3 × 24 months)
- `holdings` (39 rows)
- `transactions` (25 rows)
- `benchmarks` (8 rows)
- `ips_policies` (18 rows)

**Created empty, written by agents and user (write-side):**
- `insights`
- `ic_items`
- `notes`
- `watchlist_items`
- `agent_actions` (activity log; agent writes only)

---

## Deployment

### 1. Create the Supabase project
Either via Lovable Cloud (recommended — Lovable scaffolds the schema and the portal together) or directly in Supabase. Capture the project URL and the service-role key.

### 2. Configure Railway

```bash
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SEED_ON_BOOT=true
```

Railway auto-detects FastAPI from `Procfile` and `requirements.txt`. The first deploy:
1. Boots the service
2. Seeds JSON files into Supabase (since tables are empty)
3. Reports `"seed_on_boot": true` and table counts on `/health`

After the first successful seed, set `SEED_ON_BOOT=false` so subsequent deploys don't re-seed (the seeder skips populated tables anyway, but flipping the flag avoids the boot-time check).

### 3. Verify

```bash
curl https://family-office-agent-api.up.railway.app/health
```

Expected: `{ "ok": true, "supabase_connected": true, "table_counts": { ... } }`

### 4. Local development

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
uvicorn main:app --reload
```

Visit http://localhost:8000/health to verify.

### 5. Cold-start mitigation

Railway free tier sleeps after ~10 minutes idle. First request after sleep takes 5-10 seconds. Set up a cron-job.org ping to `/health` every 5 minutes during demo windows. (Same pattern as the Al-Noor service.)

---

## Resetting demo data

The portal's "Reset Demo" button runs SQL directly in Supabase (via Lovable Cloud, not through Railway) to:
1. Delete all rows from `insights`, `ic_items`, `notes`, `watchlist_items`, `agent_actions`
2. (Optional) Re-seed the read-side tables from JSON by setting `SEED_ON_BOOT=true` and redeploying — but normally the read-side stays untouched and only the agent/user-written tables get cleared.

---

## Naming conventions

- JSON files use human-readable keys: `"Holding ID"`, `"Name (EN)"`, `"NAV (USD m)"`
- `json_to_db_row()` in `main.py` translates these to snake_case columns: `holding_id`, `name_en`, `nav_usd_m`
- Nested objects (e.g. `Operating KPIs`) stored as JSONB in Postgres
- IDs are human-readable: `HLD-001`, `OPCO-001`, `ENT-001`
- Dates ISO `YYYY-MM-DD`
- Percentages stored as decimals (`0.082`, not `8.2`)
- English-only for v1 (Arabic adds in v2 with `_ar` suffix)

---

## Security posture

Demo only. No auth on the FastAPI endpoints. Synthetic data. Acceptable for the early-stage family office conversations; we harden before any real customer data lands. Production upgrade path documented separately when needed.
