"""
Family Office Agent API
=======================
FastAPI service backing the Mizan and Horizon agents.

Architecture
------------
- JSON files in /data are the version-controlled baseline.
- On cold boot (if SEED_ON_BOOT=true and table is empty), JSON is seeded into Supabase Postgres.
- All agent reads go through this API's opinionated, enrichment-aware GET endpoints.
- All agent writes go through this API. Every POST does TWO inserts:
    1. The business write (insights / ic_items / notes / watchlist_items)
    2. An entry in `agent_actions` with curated description text
- The Lovable portal subscribes to Supabase Realtime on the business tables (for UI rendering)
  AND on `agent_actions` (for the Live Agent Activity drawer).
- Why agent_actions separately: curated descriptions, agent-vs-user distinction, and
  uniform capture of inserts/updates/deletes.
- Chat message persistence is handled by the Nebelus widget, not this service.

Endpoint counts
---------------
GET (8):
  /health
  /family-office
  /people
  /entities
  /portfolio                   -- workhorse: family office + holdings + opcos + IPS in one call
  /portfolio-company/{id}      -- one opco with KPI history
  /holdings                    -- filterable holdings
  /transactions                -- recent transactions

POST (4, agent-only):
  /insight
  /ic-item
  /note
  /watchlist-item

Auth
----
None (demo posture). Railway URL is HTTPS but otherwise open. Supabase service-role key
held only by this service, never exposed to the agent or the portal.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client

load_dotenv()

# ----------------------------------------------------------------------
# Supabase client (server-side, service-role key)
# ----------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SEED_ON_BOOT = os.environ.get("SEED_ON_BOOT", "false").lower() == "true"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("WARNING: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set. API will fail on data calls.")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------
app = FastAPI(
    title="Family Office Agent API",
    description="Read + write surface for Mizan and Horizon agents.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# JSON -> DB column name translation
# JSON files use human-readable keys; DB tables use snake_case column names.
# ----------------------------------------------------------------------
def json_to_db_row(row: dict) -> dict:
    """
    Translates JSON keys like "Holding ID" / "Name (EN)" / "NAV (USD m)" to
    snake_case DB column names: holding_id / name_en / nav_usd_m.
    """
    out = {}
    for k, v in row.items():
        col = k.lower()
        # Normalize parenthetical suffixes
        col = col.replace(" (en)", "_en").replace(" (ar)", "_ar")
        col = col.replace(" (usd m)", "_usd_m").replace(" (usd)", "_usd")
        col = col.replace(" (sar)", "_sar").replace(" (%)", "_pct")
        col = col.replace("%", "_pct")
        # General clean-up
        col = col.replace(" / ", "_").replace("/", "_")
        col = col.replace(" — ", "_").replace("—", "_")
        col = col.replace(" - ", "_").replace("-", "_")
        col = col.replace(" ", "_")
        # Collapse repeats
        while "__" in col:
            col = col.replace("__", "_")
        col = col.strip("_")
        out[col] = v
    return out


# ----------------------------------------------------------------------
# Seed-on-boot
# ----------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"

# Mapping: JSON filename (without .json) -> Supabase table + record kind
SEED_MAP = [
    ("family_office", "family_office", "single"),
    ("people", "people", "list"),
    ("entities", "entities", "list"),
    ("portfolio_companies", "portfolio_companies", "list"),
    ("portfolio_company_kpis", "portfolio_company_kpis", "list"),
    ("holdings", "holdings", "list"),
    ("transactions", "transactions", "list"),
    ("benchmarks", "benchmarks", "list"),
    ("ips_policies", "ips_policies", "list"),
]


def seed_if_empty():
    if not supabase or not SEED_ON_BOOT:
        return

    print("[SEED] Seeding pass starting...")
    for filename, table, kind in SEED_MAP:
        path = DATA_DIR / f"{filename}.json"
        if not path.exists():
            print(f"[SEED] skipping {table}: {path} not found")
            continue

        try:
            existing = supabase.table(table).select("*", count="exact").limit(1).execute()
            if existing.count and existing.count > 0:
                print(f"[SEED] skipping {table}: already has {existing.count} row(s)")
                continue
        except Exception as e:
            print(f"[SEED] error checking {table}: {e}")
            continue

        with open(path) as f:
            data = json.load(f)

        if kind == "single":
            rows = [json_to_db_row(data)]
        else:
            rows = [json_to_db_row(r) for r in data]

        try:
            for i in range(0, len(rows), 100):
                batch = rows[i:i + 100]
                supabase.table(table).insert(batch).execute()
            print(f"[SEED] inserted {len(rows)} row(s) into {table}")
        except Exception as e:
            print(f"[SEED] error inserting into {table}: {e}")

    print("[SEED] Seeding pass complete.")


@app.on_event("startup")
def on_startup():
    seed_if_empty()


# ======================================================================
# agent_actions logging
# Every agent write logs an entry here. Lovable's Live Agent Activity drawer
# subscribes to this table via Supabase Realtime and renders the `description`.
# ======================================================================

def log_action(
    actor: str,
    action_type: str,
    description: str,
    target_table: str,
    target_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    """
    Insert an entry into agent_actions. Lovable's activity drawer subscribes
    to this table and renders the `description` field as the user-facing log line.

    actor:         'mizan' | 'horizon'
    action_type:   dotted notation, e.g. 'insight.created', 'ic_item.created'
    description:   curated, human-readable string for the drawer
    target_table:  the business table this action affected
    target_id:     the row ID created/affected
    payload:       optional JSON snapshot of the action context
    """
    if not supabase:
        return
    try:
        supabase.table("agent_actions").insert({
            "actor": actor,
            "action_type": action_type,
            "description": description,
            "target_table": target_table,
            "target_id": target_id,
            "payload": payload or {},
            "created_at": datetime.utcnow().isoformat() + "Z",
        }).execute()
    except Exception as e:
        # Activity log failure should not break the agent's action; log and move on.
        print(f"[agent_actions] log_action failed: {e}")


def _agent_display_name(actor: str) -> str:
    return {"mizan": "Mizan", "horizon": "Horizon"}.get(actor, actor.title())


# ======================================================================
# HEALTH
# ======================================================================
@app.get("/health")
def health():
    counts = {}
    if supabase:
        for _, table, _ in SEED_MAP:
            try:
                r = supabase.table(table).select("*", count="exact").limit(1).execute()
                counts[table] = r.count
            except Exception as e:
                counts[table] = f"error: {e}"
    return {
        "ok": True,
        "service": "family-office-agent-api",
        "version": "1.0.0",
        "supabase_connected": supabase is not None,
        "seed_on_boot": SEED_ON_BOOT,
        "table_counts": counts,
    }


# ======================================================================
# GET endpoints (read-side)
# Functional placeholders; behavioral descriptions to be tightened in Step 3
# (agent_endpoint_spec.md), which is what gets pasted into Nebelus AI Studio.
# ======================================================================

@app.get("/family-office")
def get_family_office():
    """Return the single family office record + summary totals."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    r = supabase.table("family_office").select("*").limit(1).execute()
    if not r.data:
        raise HTTPException(404, "Family office record not found")
    return r.data[0]


@app.get("/people")
def get_people(role: Optional[str] = None, type_: Optional[str] = Query(None, alias="type")):
    """Return all people, optionally filtered by role substring or type."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    q = supabase.table("people").select("*")
    if type_:
        q = q.eq("type", type_)
    r = q.execute()
    data = r.data or []
    if role:
        data = [p for p in data if role.lower() in (p.get("role_en") or "").lower()]
    return {"count": len(data), "people": data}


@app.get("/entities")
def get_entities():
    """Return the legal entity structure as a flat list."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    r = supabase.table("entities").select("*").execute()
    return {"count": len(r.data or []), "entities": r.data or []}


@app.get("/portfolio")
def get_portfolio():
    """
    Workhorse endpoint. Returns family_office + all holdings + operating companies
    + IPS policies + benchmarks + top-5 concentrations in one call.
    """
    if not supabase:
        raise HTTPException(500, "Supabase not configured")

    fo = supabase.table("family_office").select("*").limit(1).execute().data
    holdings = supabase.table("holdings").select("*").execute().data or []
    opcos = supabase.table("portfolio_companies").select("*").execute().data or []
    ips = supabase.table("ips_policies").select("*").execute().data or []
    benchmarks = supabase.table("benchmarks").select("*").execute().data or []

    if not fo:
        raise HTTPException(404, "Family office record not found")
    fo = fo[0]

    total_holdings_nav = sum(float(h.get("current_nav_usd_m") or 0) for h in holdings)
    total_opco_carrying = sum(float(c.get("carrying_value_usd_m") or 0) for c in opcos)
    total_aum = total_holdings_nav + total_opco_carrying

    by_class = {}
    for h in holdings:
        cls = h.get("asset_class") or "Unknown"
        by_class[cls] = by_class.get(cls, 0) + float(h.get("current_nav_usd_m") or 0)

    sorted_h = sorted(holdings, key=lambda x: -float(x.get("current_nav_usd_m") or 0))
    top_concentrations = [
        {
            "holding_id": h["holding_id"],
            "name_en": h["name_en"],
            "nav_usd_m": float(h.get("current_nav_usd_m") or 0),
            "pct_of_liquid_aum": round(float(h.get("current_nav_usd_m") or 0) / total_holdings_nav, 4) if total_holdings_nav else 0,
        }
        for h in sorted_h[:5]
    ]

    return {
        "family_office": fo,
        "summary": {
            "total_aum_usd_m": round(total_aum, 1),
            "liquid_aum_usd_m": round(total_holdings_nav, 1),
            "operating_co_aum_usd_m": round(total_opco_carrying, 1),
            "holdings_count": len(holdings),
            "operating_companies_count": len(opcos),
            "asset_class_breakdown_usd_m": {k: round(v, 1) for k, v in by_class.items()},
        },
        "holdings": holdings,
        "operating_companies": opcos,
        "ips_policies": ips,
        "benchmarks": benchmarks,
        "top_concentrations": top_concentrations,
    }


@app.get("/portfolio-company/{company_id}")
def get_portfolio_company(company_id: str, months: int = 24):
    """Return a single operating company + its monthly KPI history (default 24 months) + CEO + entity."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")

    co = supabase.table("portfolio_companies").select("*").eq("company_id", company_id).limit(1).execute()
    if not co.data:
        raise HTTPException(404, f"Operating company {company_id} not found")
    co = co.data[0]

    kpis = supabase.table("portfolio_company_kpis").select("*").eq("company_id", company_id).order("month").execute()
    kpi_rows = kpis.data or []
    if months and len(kpi_rows) > months:
        kpi_rows = kpi_rows[-months:]

    ceo = None
    if co.get("ceo_person_id"):
        c = supabase.table("people").select("*").eq("person_id", co["ceo_person_id"]).limit(1).execute()
        ceo = c.data[0] if c.data else None

    entity = None
    if co.get("entity_id"):
        e = supabase.table("entities").select("*").eq("entity_id", co["entity_id"]).limit(1).execute()
        entity = e.data[0] if e.data else None

    return {
        "company": co,
        "ceo": ceo,
        "entity": entity,
        "kpi_history": kpi_rows,
        "kpi_months_returned": len(kpi_rows),
    }


@app.get("/holdings")
def get_holdings(
    asset_class: Optional[str] = None,
    sub_class: Optional[str] = None,
    status: Optional[str] = None,
    geography: Optional[str] = None,
):
    """Filterable holdings list. Use for queries narrower than /portfolio."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    q = supabase.table("holdings").select("*")
    if asset_class:
        q = q.eq("asset_class", asset_class)
    if sub_class:
        q = q.eq("sub_class", sub_class)
    if status:
        q = q.eq("status", status)
    if geography:
        q = q.ilike("geography_en", f"%{geography}%")
    r = q.execute()
    return {"count": len(r.data or []), "holdings": r.data or []}


@app.get("/transactions")
def get_transactions(
    days: int = 90,
    type_: Optional[str] = Query(None, alias="type"),
    status: Optional[str] = None,
    holding_id: Optional[str] = None,
):
    """Recent transactions, filterable by type, status, or holding."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    q = supabase.table("transactions").select("*").order("date", desc=True)
    if type_:
        q = q.eq("type", type_)
    if status:
        q = q.eq("status", status)
    if holding_id:
        q = q.eq("holding_id", holding_id)
    r = q.execute()
    return {"count": len(r.data or []), "transactions": r.data or []}


# ======================================================================
# POST endpoints (agent-only)
# Each POST does TWO inserts: business table + agent_actions.
# ======================================================================

# ---- Request models ----

class InsightCreate(BaseModel):
    title: str = Field(..., max_length=200, description="Short, scannable title for the insight.")
    body_md: str = Field(..., description="Markdown body. The actual analysis.")
    source_agent: str = Field(..., description="'mizan' or 'horizon'")
    insight_type: str = Field(..., description="'analysis' | 'diagnosis' | 'comparison' | 'signal' | 'event_impact' | 'compliance_check' | 'recommendation'")
    confidence: Optional[str] = Field(None, description="'low' | 'medium' | 'high'. Required for forward-looking insights.")
    pinned_to_dashboard: bool = Field(False, description="If true, the portal renders this on the main dashboard.")
    linked_holding_ids: Optional[List[str]] = Field(default_factory=list)
    linked_company_ids: Optional[List[str]] = Field(default_factory=list)
    chart_spec: Optional[dict] = Field(None, description="Optional chart spec (Mermaid, Vega-Lite, or simple {type, series}).")
    tags: Optional[List[str]] = Field(default_factory=list)


class ICItemCreate(BaseModel):
    title: str = Field(..., max_length=200)
    rationale_md: str = Field(..., description="Why this item is being added to the IC queue.")
    source_agent: str = Field(..., description="'mizan' or 'horizon'")
    urgency: str = Field(..., description="'immediate' | 'next_ic' | 'monitor'")
    proposed_action: Optional[str] = None
    linked_holding_ids: Optional[List[str]] = Field(default_factory=list)
    linked_company_ids: Optional[List[str]] = Field(default_factory=list)
    linked_insight_id: Optional[str] = None


class NoteCreate(BaseModel):
    body_md: str = Field(..., description="Note text (markdown).")
    source_agent: str = Field(..., description="'mizan' or 'horizon'")
    attached_to_type: Optional[str] = Field(None, description="'holding' | 'company' | 'entity' | 'general'")
    attached_to_id: Optional[str] = None
    tags: Optional[List[str]] = Field(default_factory=list)


class WatchlistItemCreate(BaseModel):
    title: str = Field(..., max_length=200)
    reason_md: str = Field(..., description="Why this is being watched.")
    source_agent: str = Field(..., description="'mizan' or 'horizon'")
    holding_id: Optional[str] = None
    company_id: Optional[str] = None
    review_by: Optional[str] = Field(None, description="ISO date — when this should be re-reviewed.")
    severity: str = Field("monitor", description="'monitor' | 'elevated' | 'urgent'")


def _validate_source_agent(value: str):
    if value not in ("mizan", "horizon"):
        raise HTTPException(400, f"source_agent must be 'mizan' or 'horizon', got '{value}'")


# ---- POST /insight ----

@app.post("/insight")
def create_insight(payload: InsightCreate):
    """
    Create an insight. Pinned insights render on the portal dashboard; all insights
    appear in the activity drawer (via the agent_actions log entry).
    """
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    _validate_source_agent(payload.source_agent)

    row = {
        "title": payload.title,
        "body_md": payload.body_md,
        "source_agent": payload.source_agent,
        "insight_type": payload.insight_type,
        "confidence": payload.confidence,
        "pinned_to_dashboard": payload.pinned_to_dashboard,
        "linked_holding_ids": payload.linked_holding_ids or [],
        "linked_company_ids": payload.linked_company_ids or [],
        "chart_spec": payload.chart_spec,
        "tags": payload.tags or [],
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        r = supabase.table("insights").insert(row).execute()
        created = r.data[0] if r.data else row
    except Exception as e:
        raise HTTPException(500, f"Insert failed: {e}")

    # Log to agent_actions for the activity drawer
    agent_display = _agent_display_name(payload.source_agent)
    pinned_suffix = " and pinned it to the dashboard" if payload.pinned_to_dashboard else ""
    description = f"{agent_display} created insight: {payload.title}{pinned_suffix}"

    log_action(
        actor=payload.source_agent,
        action_type="insight.pinned" if payload.pinned_to_dashboard else "insight.created",
        description=description,
        target_table="insights",
        target_id=str(created.get("id")) if isinstance(created, dict) and created.get("id") is not None else None,
        payload={
            "insight_type": payload.insight_type,
            "confidence": payload.confidence,
            "linked_holding_ids": payload.linked_holding_ids or [],
            "linked_company_ids": payload.linked_company_ids or [],
        },
    )

    return {"ok": True, "insight": created}


# ---- POST /ic-item ----

@app.post("/ic-item")
def create_ic_item(payload: ICItemCreate):
    """Add an item to the Investment Committee queue."""
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    _validate_source_agent(payload.source_agent)

    if payload.urgency not in ("immediate", "next_ic", "monitor"):
        raise HTTPException(400, "urgency must be one of: immediate, next_ic, monitor")

    row = {
        "title": payload.title,
        "rationale_md": payload.rationale_md,
        "source_agent": payload.source_agent,
        "urgency": payload.urgency,
        "proposed_action": payload.proposed_action,
        "linked_holding_ids": payload.linked_holding_ids or [],
        "linked_company_ids": payload.linked_company_ids or [],
        "linked_insight_id": payload.linked_insight_id,
        "status": "open",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        r = supabase.table("ic_items").insert(row).execute()
        created = r.data[0] if r.data else row
    except Exception as e:
        raise HTTPException(500, f"Insert failed: {e}")

    agent_display = _agent_display_name(payload.source_agent)
    urgency_label = {"immediate": "immediate", "next_ic": "next IC", "monitor": "monitor"}.get(payload.urgency, payload.urgency)
    description = f"{agent_display} flagged for IC ({urgency_label}): {payload.title}"

    log_action(
        actor=payload.source_agent,
        action_type="ic_item.created",
        description=description,
        target_table="ic_items",
        target_id=str(created.get("id")) if isinstance(created, dict) and created.get("id") is not None else None,
        payload={
            "urgency": payload.urgency,
            "linked_holding_ids": payload.linked_holding_ids or [],
            "linked_company_ids": payload.linked_company_ids or [],
            "linked_insight_id": payload.linked_insight_id,
        },
    )

    return {"ok": True, "ic_item": created}


# ---- POST /note ----

@app.post("/note")
def create_note(payload: NoteCreate):
    """
    Add a note (agent-authored). Notes can attach to a holding, operating company,
    entity, or be general. Surfaces in the portal's Notes panel and on detail pages.
    Note: user-authored notes from the Lovable portal are written directly to Supabase
    by Lovable, not through this endpoint, and do NOT appear in the agent activity drawer.
    """
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    _validate_source_agent(payload.source_agent)

    if payload.attached_to_type and payload.attached_to_type not in ("holding", "company", "entity", "general"):
        raise HTTPException(400, "attached_to_type must be one of: holding, company, entity, general")

    row = {
        "body_md": payload.body_md,
        "source_agent": payload.source_agent,
        "source": payload.source_agent,  # mirror for query convenience; user-authored notes set source='user'
        "attached_to_type": payload.attached_to_type,
        "attached_to_id": payload.attached_to_id,
        "tags": payload.tags or [],
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        r = supabase.table("notes").insert(row).execute()
        created = r.data[0] if r.data else row
    except Exception as e:
        raise HTTPException(500, f"Insert failed: {e}")

    agent_display = _agent_display_name(payload.source_agent)
    snippet = (payload.body_md or "").replace("\n", " ").strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    if payload.attached_to_type and payload.attached_to_id:
        attach_str = f" on {payload.attached_to_type} {payload.attached_to_id}"
    else:
        attach_str = ""
    description = f"{agent_display} added note{attach_str}: {snippet}"

    log_action(
        actor=payload.source_agent,
        action_type="note.created",
        description=description,
        target_table="notes",
        target_id=str(created.get("id")) if isinstance(created, dict) and created.get("id") is not None else None,
        payload={
            "attached_to_type": payload.attached_to_type,
            "attached_to_id": payload.attached_to_id,
            "tags": payload.tags or [],
        },
    )

    return {"ok": True, "note": created}


# ---- POST /watchlist-item ----

@app.post("/watchlist-item")
def create_watchlist_item(payload: WatchlistItemCreate):
    """
    Add a watchlist item. Surfaces in the portal's Watchlist panel. Either
    holding_id or company_id may be set, or neither for thematic watches.
    """
    if not supabase:
        raise HTTPException(500, "Supabase not configured")
    _validate_source_agent(payload.source_agent)

    if payload.severity not in ("monitor", "elevated", "urgent"):
        raise HTTPException(400, "severity must be one of: monitor, elevated, urgent")

    row = {
        "title": payload.title,
        "reason_md": payload.reason_md,
        "source_agent": payload.source_agent,
        "holding_id": payload.holding_id,
        "company_id": payload.company_id,
        "review_by": payload.review_by,
        "severity": payload.severity,
        "status": "active",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        r = supabase.table("watchlist_items").insert(row).execute()
        created = r.data[0] if r.data else row
    except Exception as e:
        raise HTTPException(500, f"Insert failed: {e}")

    agent_display = _agent_display_name(payload.source_agent)
    description = f"{agent_display} added to watchlist ({payload.severity}): {payload.title}"

    log_action(
        actor=payload.source_agent,
        action_type="watchlist.added",
        description=description,
        target_table="watchlist_items",
        target_id=str(created.get("id")) if isinstance(created, dict) and created.get("id") is not None else None,
        payload={
            "severity": payload.severity,
            "holding_id": payload.holding_id,
            "company_id": payload.company_id,
            "review_by": payload.review_by,
        },
    )

    return {"ok": True, "watchlist_item": created}
