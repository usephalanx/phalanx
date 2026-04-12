"""
Traces API — read-only view into agent reasoning traces.

Routes:
  GET /runs/{run_id}/trace          — list all traces for a run
  GET /runs/{run_id}/trace/{id}     — get a single trace
  GET /traces                       — HTML portal (timeline view)

Query params:
  ?type=reflection|decision|uncertainty|disagreement|self_check|handoff
  ?task_id=<uuid>
  ?agent_role=builder|reviewer|...
  ?limit=50
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from phalanx.db.models import AgentTrace, Run
from phalanx.db.session import get_db

router = APIRouter(prefix="/runs", tags=["traces"])
portal_router = APIRouter(tags=["traces"])


# ── Schema ────────────────────────────────────────────────────────────────────


class TraceOut(BaseModel):
    id: str
    run_id: str
    task_id: str | None
    agent_role: str
    agent_id: str
    trace_type: str
    content: str
    context: dict
    tokens_used: int | None
    created_at: str

    @classmethod
    def from_orm(cls, t: AgentTrace) -> TraceOut:
        return cls(
            id=t.id,
            run_id=t.run_id,
            task_id=t.task_id,
            agent_role=t.agent_role,
            agent_id=t.agent_id,
            trace_type=t.trace_type,
            content=t.content,
            context=t.context or {},
            tokens_used=t.tokens_used,
            created_at=t.created_at.isoformat(),
        )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/{run_id}/trace", response_model=list[TraceOut])
async def list_run_traces(
    run_id: str,
    type: str | None = Query(default=None, description="Filter by trace_type"),
    task_id: str | None = Query(default=None, description="Filter by task_id"),
    agent_role: str | None = Query(default=None, description="Filter by agent_role"),
    limit: int = Query(default=100, le=500),
):
    """List agent reasoning traces for a run, oldest first."""
    async with get_db() as session:
        run_check = await session.execute(select(Run.id).where(Run.id == run_id))
        if run_check.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found",
            )

        stmt = (
            select(AgentTrace)
            .where(AgentTrace.run_id == run_id)
            .order_by(AgentTrace.created_at)
            .limit(limit)
        )
        if type is not None:
            stmt = stmt.where(AgentTrace.trace_type == type)
        if task_id is not None:
            stmt = stmt.where(AgentTrace.task_id == task_id)
        if agent_role is not None:
            stmt = stmt.where(AgentTrace.agent_role == agent_role)

        result = await session.execute(stmt)
        traces = list(result.scalars())
        return [TraceOut.from_orm(t) for t in traces]


@router.get("/{run_id}/trace/{trace_id}", response_model=TraceOut)
async def get_trace(run_id: str, trace_id: str):
    """Get a single agent trace by ID."""
    async with get_db() as session:
        result = await session.execute(
            select(AgentTrace).where(
                AgentTrace.id == trace_id,
                AgentTrace.run_id == run_id,
            )
        )
        trace = result.scalar_one_or_none()
        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trace {trace_id!r} not found for run {run_id!r}",
            )
        return TraceOut.from_orm(trace)


# ── HTML portal ───────────────────────────────────────────────────────────────

_TRACES_PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phalanx — Agent Traces</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
         background: #09090b; color: #e4e4e7; min-height: 100vh; }
  .header { background: #111113; border-bottom: 1px solid #1f1f23;
            padding: 0 32px; height: 56px; display: flex; align-items: center; gap: 12px;
            position: sticky; top: 0; z-index: 10; }
  .header-logo { font-size: 15px; font-weight: 700; color: #f4f4f5;
                 letter-spacing: -.02em; display: flex; align-items: center; gap: 8px; }
  .header-logo span { color: #818cf8; }
  .container { max-width: 1100px; margin: 0 auto; padding: 32px 24px; }
  .search-bar { display: flex; gap: 10px; margin-bottom: 28px; }
  .search-bar input { flex: 1; background: #111113; border: 1px solid #27272a;
                      color: #e4e4e7; padding: 10px 14px; border-radius: 8px;
                      font-size: 14px; outline: none; font-family: monospace; }
  .search-bar input:focus { border-color: #818cf8; }
  .search-bar button { background: #818cf8; color: #fff; border: none;
                       padding: 10px 20px; border-radius: 8px; cursor: pointer;
                       font-size: 14px; font-weight: 600; }
  .search-bar button:hover { background: #6366f1; }
  .filters { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .filter-btn { background: #18181b; border: 1px solid #27272a; color: #a1a1aa;
                padding: 5px 12px; border-radius: 20px; cursor: pointer;
                font-size: 12px; font-weight: 500; transition: all .15s; }
  .filter-btn.active, .filter-btn:hover { border-color: #818cf8; color: #818cf8; }
  .count { font-size: 12px; color: #52525b; margin-bottom: 16px; }
  .trace-list { display: flex; flex-direction: column; gap: 10px; }
  .trace-card { background: #111113; border: 1px solid #1f1f23; border-radius: 12px;
                overflow: hidden; }
  .trace-header { display: flex; align-items: center; gap: 10px; padding: 12px 16px;
                  cursor: pointer; user-select: none; }
  .trace-header:hover { background: #18181b; }
  .badge { font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 4px;
           text-transform: uppercase; letter-spacing: .05em; }
  .badge-reflection   { background: #1e3a5f; color: #60a5fa; }
  .badge-decision     { background: #1f2d1f; color: #4ade80; }
  .badge-uncertainty  { background: #3b2100; color: #fb923c; }
  .badge-disagreement { background: #2d1515; color: #f87171; }
  .badge-self_check   { background: #1c1f2e; color: #a78bfa; }
  .badge-handoff      { background: #1e2f2f; color: #34d399; }
  .badge-default      { background: #27272a; color: #a1a1aa; }
  .agent-pill { font-size: 11px; color: #71717a; background: #18181b;
                border: 1px solid #27272a; padding: 2px 8px; border-radius: 10px; }
  .trace-time { font-size: 11px; color: #52525b; margin-left: auto; font-family: monospace; }
  .trace-content { padding: 0 16px 14px; }
  .trace-body { font-size: 13px; color: #a1a1aa; white-space: pre-wrap;
                line-height: 1.6; font-family: monospace;
                border-left: 2px solid #27272a; padding-left: 12px;
                max-height: 300px; overflow-y: auto; }
  .trace-ctx { margin-top: 8px; font-size: 11px; color: #52525b;
               font-family: monospace; }
  .empty { text-align: center; color: #52525b; padding: 60px 0; font-size: 14px; }
  .error { color: #f87171; font-size: 13px; padding: 16px; background: #1c0a0a;
           border-radius: 8px; margin-bottom: 16px; }
  .chevron { margin-left: auto; color: #52525b; transition: transform .2s; font-size: 12px; }
  .chevron.open { transform: rotate(90deg); }
</style>
</head>
<body>
<div class="header">
  <div class="header-logo">⚡ <span>Phalanx</span> · Agent Traces</div>
</div>
<div class="container">
  <div class="search-bar">
    <input id="run-input" type="text" placeholder="Enter run ID…" autocomplete="off" />
    <button onclick="loadTraces()">Load</button>
  </div>
  <div id="filters" class="filters" style="display:none"></div>
  <div id="count" class="count"></div>
  <div id="error" class="error" style="display:none"></div>
  <div id="trace-list" class="trace-list"></div>
</div>
<script>
const BADGE = {
  reflection:'badge-reflection', decision:'badge-decision',
  uncertainty:'badge-uncertainty', disagreement:'badge-disagreement',
  self_check:'badge-self_check', handoff:'badge-handoff'
};
let allTraces = [];
let activeFilter = null;

async function loadTraces() {
  const runId = document.getElementById('run-input').value.trim();
  if (!runId) return;
  const err = document.getElementById('error');
  err.style.display = 'none';
  document.getElementById('trace-list').innerHTML = '<div class="empty">Loading…</div>';
  document.getElementById('filters').style.display = 'none';
  document.getElementById('count').textContent = '';
  try {
    const r = await fetch('/v1/runs/' + encodeURIComponent(runId) + '/trace?limit=500');
    if (!r.ok) { const j = await r.json(); throw new Error(j.detail || r.statusText); }
    allTraces = await r.json();
    activeFilter = null;
    renderFilters();
    renderTraces(allTraces);
  } catch(e) {
    err.textContent = '✗ ' + e.message;
    err.style.display = 'block';
    document.getElementById('trace-list').innerHTML = '';
  }
}

function renderFilters() {
  const counts = {};
  allTraces.forEach(t => counts[t.trace_type] = (counts[t.trace_type]||0)+1);
  const el = document.getElementById('filters');
  el.style.display = 'flex';
  el.innerHTML = ['all', ...Object.keys(counts)].map(k => {
    const n = k === 'all' ? allTraces.length : counts[k];
    return `<button class="filter-btn${activeFilter===k||(!activeFilter&&k==='all')?' active':''}"
      onclick="setFilter('${k}')">${k} <span style="opacity:.6">${n}</span></button>`;
  }).join('');
}

function setFilter(f) {
  activeFilter = f === 'all' ? null : f;
  renderFilters();
  renderTraces(activeFilter ? allTraces.filter(t => t.trace_type === activeFilter) : allTraces);
}

function renderTraces(traces) {
  const cnt = document.getElementById('count');
  cnt.textContent = traces.length + ' trace' + (traces.length !== 1 ? 's' : '');
  const el = document.getElementById('trace-list');
  if (!traces.length) { el.innerHTML = '<div class="empty">No traces found.</div>'; return; }
  el.innerHTML = traces.map((t, i) => {
    const badge = BADGE[t.trace_type] || 'badge-default';
    const ts = new Date(t.created_at).toLocaleTimeString();
    const ctxKeys = Object.keys(t.context || {});
    const ctxStr = ctxKeys.length ? ctxKeys.map(k => k+'='+JSON.stringify(t.context[k])).join('  ') : '';
    return `<div class="trace-card">
      <div class="trace-header" onclick="toggle(${i})">
        <span class="badge ${badge}">${t.trace_type}</span>
        <span class="agent-pill">${t.agent_role}</span>
        <span style="font-size:12px;color:#71717a;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 8px">
          ${escHtml(t.content.slice(0,120))}
        </span>
        <span class="trace-time">${ts}</span>
        <span class="chevron" id="chev-${i}">▶</span>
      </div>
      <div id="body-${i}" class="trace-content" style="display:none">
        <div class="trace-body">${escHtml(t.content)}</div>
        ${ctxStr ? `<div class="trace-ctx">${escHtml(ctxStr)}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

function toggle(i) {
  const b = document.getElementById('body-'+i);
  const c = document.getElementById('chev-'+i);
  const open = b.style.display !== 'none';
  b.style.display = open ? 'none' : 'block';
  c.classList.toggle('open', !open);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

document.getElementById('run-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') loadTraces();
});

// Auto-load if run_id in URL
const params = new URLSearchParams(location.search);
if (params.get('run_id')) {
  document.getElementById('run-input').value = params.get('run_id');
  loadTraces();
}
</script>
</body>
</html>"""


@portal_router.get("/traces", response_class=HTMLResponse, include_in_schema=False)
async def traces_portal():
    """Agent traces portal — timeline view of soul traces per run."""
    return HTMLResponse(_TRACES_PORTAL_HTML)
