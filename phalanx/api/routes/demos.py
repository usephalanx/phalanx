"""
Demo portal API routes.

GET  /v1/demos            — list all demos
GET  /v1/demos/{id}       — single demo detail
POST /v1/demos/{id}/start — queue a portal_start_demo Celery task
POST /v1/demos/{id}/stop  — queue a portal_stop_demo Celery task
GET  /demos               — HTML portal page (demo.usephalanx.com)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from phalanx.db.models import Demo
from phalanx.db.session import get_db

router = APIRouter(tags=["demos"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class DemoOut(BaseModel):
    id: str
    run_id: str
    slug: str
    title: str
    app_type: str | None
    status: str
    demo_url: str | None
    error: str | None
    last_accessed_at: str | None
    built_at: str | None
    created_at: str | None

    model_config = {"from_attributes": True}


# ── List demos ────────────────────────────────────────────────────────────────

@router.get("/v1/demos", response_model=list[DemoOut])
async def list_demos():
    """Return all demos ordered by creation date descending."""
    async with get_db() as session:
        result = await session.execute(
            select(Demo).order_by(Demo.created_at.desc())
        )
        demos = result.scalars().all()
        return [_to_out(d) for d in demos]


# ── Get single demo ───────────────────────────────────────────────────────────

@router.get("/v1/demos/{demo_id}", response_model=DemoOut)
async def get_demo(demo_id: str):
    async with get_db() as session:
        demo = await session.get(Demo, demo_id)
        if demo is None:
            raise HTTPException(status_code=404, detail="Demo not found")
        return _to_out(demo)


# ── Start demo ────────────────────────────────────────────────────────────────

@router.post("/v1/demos/{demo_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_demo(demo_id: str):
    """
    Queue a portal_start_demo Celery task to spin up the container.

    Returns immediately with the Celery task ID; poll GET /v1/demos/{id}
    until status transitions to RUNNING or FAILED.
    """
    async with get_db() as session:
        demo = await session.get(Demo, demo_id)
        if demo is None:
            raise HTTPException(status_code=404, detail="Demo not found")

    from phalanx.queue.celery_app import celery_app  # noqa: PLC0415

    result = celery_app.send_task(
        "phalanx.agents.sre.portal_start_demo",
        kwargs={"demo_id": demo_id},
        queue="sre",
    )
    return {"queued": True, "celery_task_id": result.id}


# ── Stop demo ─────────────────────────────────────────────────────────────────

@router.post("/v1/demos/{demo_id}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_demo(demo_id: str):
    """Queue a portal_stop_demo Celery task to stop the container."""
    async with get_db() as session:
        demo = await session.get(Demo, demo_id)
        if demo is None:
            raise HTTPException(status_code=404, detail="Demo not found")

    from phalanx.queue.celery_app import celery_app  # noqa: PLC0415

    result = celery_app.send_task(
        "phalanx.agents.sre.portal_stop_demo",
        kwargs={"demo_id": demo_id},
        queue="sre",
    )
    return {"queued": True, "celery_task_id": result.id}


# ── Delete demo ──────────────────────────────────────────────────────────────

@router.delete("/v1/demos/{demo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_demo(demo_id: str):
    """Delete a FAILED or STOPPED demo record. Running demos cannot be deleted."""
    from sqlalchemy import delete as sql_delete  # noqa: PLC0415

    async with get_db() as session:
        demo = await session.get(Demo, demo_id)
        if demo is None:
            raise HTTPException(status_code=404, detail="Demo not found")
        if demo.status == "RUNNING":
            raise HTTPException(status_code=409, detail="Stop the demo before deleting it")
        await session.execute(sql_delete(Demo).where(Demo.id == demo_id))
        await session.commit()


@router.delete("/v1/demos", status_code=status.HTTP_200_OK)
async def delete_failed_demos():
    """Delete all FAILED demo records in one call."""
    from sqlalchemy import delete as sql_delete  # noqa: PLC0415

    async with get_db() as session:
        result = await session.execute(
            sql_delete(Demo).where(Demo.status == "FAILED").returning(Demo.id)
        )
        deleted = [row[0] for row in result.fetchall()]
        await session.commit()
    return {"deleted": len(deleted)}


# ── HTML portal ───────────────────────────────────────────────────────────────

_PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="google" content="notranslate">
<title>Phalanx · Demos</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", sans-serif;
         background: #08080a; color: #e4e4e7; min-height: 100vh; }

  /* ── Header ─────────────────────────────────────────────────────────────── */
  .header {
    background: rgba(10,10,14,.92);
    border-bottom: 1px solid #1a1a20;
    padding: 0 40px;
    height: 60px;
    display: flex;
    align-items: center;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 10;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  /* Logo mark — hexagonal Phalanx glyph */
  .logo-mark {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, #6d28d9 0%, #4f46e5 100%);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    box-shadow: 0 0 0 1px rgba(109,40,217,.4), 0 4px 12px rgba(109,40,217,.25);
  }
  .logo-mark svg { width: 18px; height: 18px; fill: #fff; }

  .logo-wordmark {
    font-size: 15px;
    font-weight: 700;
    color: #f4f4f5;
    letter-spacing: -.025em;
  }
  .logo-wordmark em {
    font-style: normal;
    color: #a78bfa;
  }

  .header-divider {
    width: 1px; height: 20px;
    background: #27272a;
    flex-shrink: 0;
  }

  .header-page { font-size: 14px; color: #71717a; font-weight: 500; }

  .header-sep { flex: 1; }

  .header-stat {
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: #52525b;
  }
  .header-stat .live-dot {
    width: 7px; height: 7px; border-radius: 50%; background: #22c55e;
    animation: pulse-dot 2s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes pulse-dot {
    0%, 100% { box-shadow: 0 0 0 0 rgba(34,197,94,.6); }
    50%       { box-shadow: 0 0 0 4px rgba(34,197,94,0); }
  }
  .header-stat.no-live .live-dot { background: #3f3f46; animation: none; }
  .header-stat span { color: #a1a1aa; }
  .btn-cleanup { padding: 5px 12px; border-radius: 7px; font-size: 11px; font-weight: 600;
                 cursor: pointer; border: 1px solid #27272a; background: transparent;
                 color: #52525b; transition: background .15s, color .15s; }
  .btn-cleanup:hover { background: #18181b; color: #f87171; border-color: rgba(220,38,38,.3); }
  .btn-cleanup.hidden { display: none; }

  /* ── Layout ─────────────────────────────────────────────────────────────── */
  .container { max-width: 1200px; margin: 0 auto; padding: 40px 32px 64px; }

  /* ── Section ────────────────────────────────────────────────────────────── */
  .section { margin-bottom: 40px; }
  .section-header {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 20px;
  }
  .section-label {
    font-size: 11px; font-weight: 700; color: #52525b;
    text-transform: uppercase; letter-spacing: .1em;
  }
  .section-count {
    font-size: 11px; font-weight: 600;
    background: #18181b; border: 1px solid #27272a;
    border-radius: 9999px; padding: 1px 8px; color: #71717a;
  }
  .section-line { flex: 1; height: 1px; background: #18181b; }

  /* ── Card grid ──────────────────────────────────────────────────────────── */
  .demo-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
  }

  /* ── Card ───────────────────────────────────────────────────────────────── */
  .demo-card {
    background: #0e0e12;
    border: 1px solid #1c1c22;
    border-radius: 16px;
    display: flex; flex-direction: column;
    overflow: hidden;
    transition: border-color .2s, box-shadow .2s, transform .15s;
  }
  .demo-card:hover {
    border-color: #2e2e38;
    box-shadow: 0 8px 32px rgba(0,0,0,.5);
    transform: translateY(-1px);
  }
  .demo-card.RUNNING {
    border-color: #14532d;
    box-shadow: 0 0 0 1px rgba(22,163,74,.15), 0 4px 20px rgba(22,163,74,.08);
  }
  .demo-card.RUNNING:hover {
    border-color: #16a34a;
    box-shadow: 0 0 0 1px rgba(22,163,74,.3), 0 8px 32px rgba(22,163,74,.12);
  }

  /* Top accent bar */
  .card-accent { height: 2px; background: #1c1c22; flex-shrink: 0; }
  .card-accent.RUNNING  { background: linear-gradient(90deg, #16a34a 0%, #4ade80 50%, #16a34a 100%); background-size: 200% 100%; animation: slide 3s linear infinite; }
  .card-accent.BUILDING { background: linear-gradient(90deg, #92400e, #f59e0b, #92400e); background-size: 200% 100%; animation: slide 1.5s linear infinite; }
  .card-accent.FAILED   { background: #7f1d1d; }
  @keyframes slide { 0%{background-position:200% 0} 100%{background-position:-200% 0} }

  /* Card body */
  .card-body { padding: 20px 20px 16px; flex: 1; display: flex; flex-direction: column; gap: 12px; }

  /* Title + badge row */
  .card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .card-title {
    font-size: 14px; font-weight: 600; color: #f4f4f5; line-height: 1.45;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    flex: 1;
  }

  /* Status badge */
  .status-badge {
    flex-shrink: 0;
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 10px; font-weight: 700;
    padding: 3px 8px; border-radius: 9999px;
    letter-spacing: .06em; text-transform: uppercase; white-space: nowrap;
  }
  .status-badge.RUNNING  { background: rgba(22,163,74,.12); color: #4ade80; border: 1px solid rgba(22,163,74,.3); }
  .status-badge.BUILDING { background: rgba(245,158,11,.1);  color: #fbbf24; border: 1px solid rgba(245,158,11,.25); }
  .status-badge.STARTING { background: rgba(245,158,11,.1);  color: #fbbf24; border: 1px solid rgba(245,158,11,.25); }
  .status-badge.STOPPED  { background: rgba(63,63,70,.25);   color: #71717a; border: 1px solid #27272a; }
  .status-badge.FAILED   { background: rgba(220,38,38,.1);   color: #f87171; border: 1px solid rgba(220,38,38,.25); }
  .dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
  .dot.RUNNING  { animation: blink 2s ease-in-out infinite; }
  .dot.BUILDING { animation: blink .8s ease-in-out infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.2} }

  /* Meta pills */
  .card-meta { display: flex; flex-wrap: wrap; gap: 5px; }
  .pill {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 11px; color: #52525b;
    background: #111115; border: 1px solid #1f1f26;
    border-radius: 6px; padding: 3px 8px; white-space: nowrap;
  }
  .pill svg { width: 10px; height: 10px; opacity: .5; flex-shrink: 0; }

  /* URL */
  .card-url { font-size: 12px; }
  .card-url a { color: #818cf8; text-decoration: none; word-break: break-all; transition: color .15s; }
  .card-url a:hover { color: #c4b5fd; text-decoration: underline; }

  /* Error */
  .card-error {
    font-size: 11px; color: #f87171;
    background: rgba(127,29,29,.15); border: 1px solid rgba(127,29,29,.4);
    border-radius: 8px; padding: 8px 10px; line-height: 1.6;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    font-family: "SF Mono", "Fira Code", monospace;
  }

  /* Card footer */
  .card-footer {
    padding: 12px 20px;
    border-top: 1px solid #14141a;
    display: flex; gap: 8px; align-items: center;
  }
  .btn {
    padding: 7px 14px; border-radius: 9px; font-size: 12px; font-weight: 600;
    cursor: pointer; border: none; transition: background .15s, opacity .15s, box-shadow .15s;
    display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
  }
  .btn:disabled { opacity: .3; cursor: not-allowed; }
  .btn-open  {
    background: linear-gradient(135deg, #6d28d9, #4f46e5);
    color: #fff; flex: 1; justify-content: center;
    box-shadow: 0 1px 4px rgba(109,40,217,.4);
  }
  .btn-open:hover:not(:disabled) {
    background: linear-gradient(135deg, #7c3aed, #5b52f0);
    box-shadow: 0 2px 8px rgba(109,40,217,.55);
  }
  .btn-start {
    background: #111827; color: #93c5fd;
    border: 1px solid rgba(30,64,175,.5);
  }
  .btn-start:hover:not(:disabled) { background: #1e3a5f; color: #fff; }
  .btn-stop {
    background: transparent; color: #52525b;
    border: 1px solid #27272a;
  }
  .btn-stop:hover:not(:disabled) { background: #18181b; color: #a1a1aa; }
  .btn-building {
    background: rgba(245,158,11,.08); color: #fbbf24;
    border: 1px solid rgba(245,158,11,.2); cursor: default;
  }
  .spacer { flex: 1; }

  /* ── Empty state ────────────────────────────────────────────────────────── */
  .empty {
    text-align: center; padding: 80px 0; color: #3f3f46;
    grid-column: 1 / -1;
  }
  .empty-glyph {
    width: 48px; height: 48px;
    background: #111115; border: 1px solid #1c1c22;
    border-radius: 12px; display: flex; align-items: center; justify-content: center;
    margin: 0 auto 16px;
  }
  .empty-glyph svg { width: 22px; height: 22px; fill: #3f3f46; }
  .empty p { font-size: 13px; line-height: 1.7; max-width: 300px; margin: 0 auto; color: #52525b; }

  /* ── Hero strip (only when running demos exist) ─────────────────────────── */
  .hero-strip {
    background: linear-gradient(135deg, rgba(109,40,217,.06) 0%, rgba(79,70,229,.04) 100%);
    border: 1px solid rgba(109,40,217,.15);
    border-radius: 16px; padding: 24px 28px;
    margin-bottom: 40px;
    display: flex; align-items: center; gap: 20px;
  }
  .hero-strip.hidden { display: none; }
  .hero-icon {
    width: 44px; height: 44px; flex-shrink: 0;
    background: linear-gradient(135deg, #6d28d9, #4f46e5);
    border-radius: 10px; display: flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 14px rgba(109,40,217,.35);
  }
  .hero-icon svg { width: 22px; height: 22px; fill: #fff; }
  .hero-text { flex: 1; }
  .hero-title { font-size: 14px; font-weight: 700; color: #f4f4f5; margin-bottom: 3px; }
  .hero-sub { font-size: 12px; color: #71717a; }
  .hero-count {
    font-size: 28px; font-weight: 800;
    background: linear-gradient(135deg, #a78bfa, #818cf8);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    letter-spacing: -.03em; flex-shrink: 0;
  }

  /* ── Modal ──────────────────────────────────────────────────────────────── */
  .modal-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,.8);
    display: flex; align-items: center; justify-content: center;
    z-index: 100; backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  }
  .modal-overlay.hidden { display: none; }
  .modal {
    background: #0e0e12; border: 1px solid #27272a;
    border-radius: 16px; padding: 28px; max-width: 420px; width: 90%;
    box-shadow: 0 24px 64px rgba(0,0,0,.7);
  }
  .modal-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .modal-icon {
    width: 36px; height: 36px;
    background: linear-gradient(135deg, #6d28d9, #4f46e5);
    border-radius: 8px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .modal-icon svg { width: 18px; height: 18px; fill: #fff; }
  .modal h2 { font-size: 15px; font-weight: 700; color: #f4f4f5; }
  .modal p  { font-size: 13px; color: #a1a1aa; line-height: 1.65; }
  .modal .warn {
    color: #fbbf24; margin-top: 14px; padding: 10px 14px;
    background: rgba(120,53,15,.2); border-radius: 8px; font-size: 12px;
    border: 1px solid rgba(120,53,15,.5); line-height: 1.6;
  }
  .modal-actions { display: flex; gap: 8px; margin-top: 20px; justify-content: flex-end; }
  .btn-confirm {
    background: linear-gradient(135deg, #6d28d9, #4f46e5);
    color: #fff; box-shadow: 0 1px 4px rgba(109,40,217,.4);
  }
  .btn-confirm:hover { background: linear-gradient(135deg, #7c3aed, #5b52f0); }
  .btn-cancel  { background: #18181b; color: #a1a1aa; border: 1px solid #27272a; }
  .btn-cancel:hover { background: #27272a; }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="logo-mark">
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 2L21.5 7.5V16.5L12 22L2.5 16.5V7.5L12 2Z"/>
    </svg>
  </div>
  <div class="logo-wordmark"><em>Phalanx</em></div>
  <div class="header-divider"></div>
  <div class="header-page">Demos</div>
  <div class="header-sep"></div>
  <div class="header-stat" id="header-stat">
    <div class="live-dot"></div>
    <span id="header-count">Loading…</span>
  </div>
  <button class="btn-cleanup hidden" id="btn-cleanup" onclick="cleanupFailed()">Clean up failed</button>
</div>

<div class="container">

  <!-- Hero strip — shown when demos are running -->
  <div class="hero-strip hidden" id="hero-strip">
    <div class="hero-icon">
      <svg viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
    </div>
    <div class="hero-text">
      <div class="hero-title">Live Demos</div>
      <div class="hero-sub">Deployed by the SRE agent · auto-scaled on demand</div>
    </div>
    <div class="hero-count" id="hero-count">0</div>
  </div>

  <!-- RUNNING section -->
  <div class="section hidden" id="section-running">
    <div class="section-header">
      <div class="section-label">Running</div>
      <div class="section-count" id="count-running">0</div>
      <div class="section-line"></div>
    </div>
    <div class="demo-grid" id="grid-running"></div>
  </div>

  <!-- BUILDING section -->
  <div class="section hidden" id="section-building">
    <div class="section-header">
      <div class="section-label">Building</div>
      <div class="section-count" id="count-building">0</div>
      <div class="section-line"></div>
    </div>
    <div class="demo-grid" id="grid-building"></div>
  </div>

  <!-- ALL / HISTORY section -->
  <div class="section" id="section-history">
    <div class="section-header">
      <div class="section-label" id="label-history">All Demos</div>
      <div class="section-count" id="count-history">0</div>
      <div class="section-line"></div>
    </div>
    <div class="demo-grid" id="grid-history">
      <div class="empty">
        <div class="empty-glyph">
          <svg viewBox="0 0 24 24"><path d="M12 2L21.5 7.5V16.5L12 22L2.5 16.5V7.5L12 2Z"/></svg>
        </div>
        <p>Loading demos…</p>
      </div>
    </div>
  </div>

</div>

<!-- Start-demo confirmation modal -->
<div class="modal-overlay hidden" id="modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-icon">
        <svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>
      </div>
      <h2>Start demo?</h2>
    </div>
    <p id="modal-body"></p>
    <div class="warn hidden" id="modal-warn"></div>
    <div class="modal-actions">
      <button class="btn btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn btn-confirm" id="modal-confirm">Start</button>
    </div>
  </div>
</div>

<script>
const API = '';
const MAX_RUNNING = {max_running};  // injected server-side from settings.demo_max_running
let demos = [];
let pendingStartId = null;

const STATUS_ORDER = { RUNNING: 0, BUILDING: 1, STARTING: 1, STOPPED: 2, FAILED: 3 };

async function fetchDemos() {
  try {
    const r = await fetch(API + '/v1/demos');
    demos = await r.json();
    render();
  } catch(e) {
    document.getElementById('grid-history').innerHTML =
      '<div class="empty"><div class="empty-glyph"><svg viewBox="0 0 24 24"><path d="M12 2L21.5 7.5V16.5L12 22L2.5 16.5V7.5L12 2Z"/></svg></div><p>Failed to load demos.</p></div>';
  }
}

function runningCount() { return demos.filter(d => d.status === 'RUNNING').length; }

function lruDemo() {
  const running = demos.filter(d => d.status === 'RUNNING');
  if (!running.length) return null;
  return running.reduce((a,b) => (a.last_accessed_at||'') < (b.last_accessed_at||'') ? a : b);
}

function fmtDate(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function cardHTML(d) {
  const isRunning  = d.status === 'RUNNING';
  const isBuilding = d.status === 'BUILDING';
  const isStarting = d.status === 'STARTING';
  const isStopped  = d.status === 'STOPPED';
  const isFailed   = d.status === 'FAILED';
  const inProgress = isBuilding || isStarting;

  const builtLine = fmtDate(d.built_at || d.created_at);
  const typePill  = d.app_type ? `<span class="pill"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/></svg>${esc(d.app_type)}</span>` : '';
  const datePill  = builtLine  ? `<span class="pill"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>${builtLine}</span>` : '';
  const slugPill  = d.slug     ? `<span class="pill"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>/${esc(d.slug)}</span>` : '';

  const urlRow  = d.demo_url ? `<div class="card-url"><a href="${d.demo_url}" target="_blank" rel="noopener">${esc(d.demo_url)}</a></div>` : '';
  const errRow  = d.error    ? `<div class="card-error">${esc(d.error.substring(0, 180))}</div>` : '';

  const spinnerIcon = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`;
  const openBtn     = (isRunning && d.demo_url) ? `<a href="${d.demo_url}" target="_blank" rel="noopener" style="flex:1;display:flex"><button class="btn btn-open" style="flex:1"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="flex-shrink:0"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>Open</button></a>` : '';
  const startBtn    = (isStopped || isFailed) ? `<button class="btn btn-start" onclick="requestStart('${d.id}')"><svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M5 3l14 9-14 9V3z"/></svg>Start</button>` : '';
  const deleteBtn   = isFailed ? `<button class="btn btn-stop" onclick="deleteDemo('${d.id}')" title="Remove this failed demo"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg></button>` : '';
  const stopBtn     = isRunning               ? `<button class="btn btn-stop" onclick="doStop('${d.id}')"><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="1"/></svg>Stop</button>` : '';
  const buildingBtn = isBuilding              ? `<button class="btn btn-building" disabled>${spinnerIcon}Building…</button>` : '';
  const startingBtn = isStarting              ? `<button class="btn btn-building" disabled>${spinnerIcon}Starting…</button>` : '';

  const cardStatus = isStarting ? 'BUILDING' : d.status;  // STARTING shows same style as BUILDING
  return `<div class="demo-card ${cardStatus}" id="card-${d.id}">
    <div class="card-accent ${cardStatus}"></div>
    <div class="card-body">
      <div class="card-header">
        <div class="card-title">${esc(d.title)}</div>
        <span class="status-badge ${cardStatus}"><span class="dot ${cardStatus}"></span>${d.status}</span>
      </div>
      <div class="card-meta">${typePill}${datePill}${slugPill}</div>
      ${urlRow}${errRow}
    </div>
    <div class="card-footer">${buildingBtn}${startingBtn}${openBtn}${startBtn}<span class="spacer"></span>${deleteBtn}${stopBtn}</div>
  </div>`;
}

function setSection(id, items) {
  const sec = document.getElementById('section-' + id);
  const grid = document.getElementById('grid-' + id);
  const cnt  = document.getElementById('count-' + id);
  if (!items.length) {
    sec.classList.add('hidden');
    return;
  }
  sec.classList.remove('hidden');
  if (cnt) cnt.textContent = items.length;
  grid.innerHTML = items.map(cardHTML).join('');
}

function render() {
  const statEl  = document.getElementById('header-stat');
  const countEl = document.getElementById('header-count');

  if (!demos.length) {
    statEl.classList.add('no-live');
    countEl.textContent = '0 demos';
    document.getElementById('hero-strip').classList.add('hidden');
    document.getElementById('grid-history').innerHTML =
      '<div class="empty"><div class="empty-glyph"><svg viewBox="0 0 24 24" fill="#3f3f46"><path d="M12 2L21.5 7.5V16.5L12 22L2.5 16.5V7.5L12 2Z"/></svg></div><p>No demos yet. Approve a run and the SRE agent will deploy one automatically.</p></div>';
    document.getElementById('count-history').textContent = '0';
    document.getElementById('section-running').classList.add('hidden');
    document.getElementById('section-building').classList.add('hidden');
    return;
  }

  const running  = demos.filter(d => d.status === 'RUNNING');
  const building = demos.filter(d => d.status === 'BUILDING' || d.status === 'STARTING');
  const rest     = demos.filter(d => !['RUNNING','BUILDING','STARTING'].includes(d.status));
  const failed   = demos.filter(d => d.status === 'FAILED');
  const cleanupBtn = document.getElementById('btn-cleanup');
  if (failed.length) cleanupBtn.classList.remove('hidden');
  else cleanupBtn.classList.add('hidden');

  // Header stat
  if (running.length) {
    statEl.classList.remove('no-live');
    countEl.textContent = running.length + ' running · ' + demos.length + ' total';
  } else {
    statEl.classList.add('no-live');
    countEl.textContent = demos.length + ' demo' + (demos.length !== 1 ? 's' : '');
  }

  // Hero strip
  const heroStrip = document.getElementById('hero-strip');
  if (running.length) {
    heroStrip.classList.remove('hidden');
    document.getElementById('hero-count').textContent = running.length;
  } else {
    heroStrip.classList.add('hidden');
  }

  // Sections
  setSection('running', running);
  setSection('building', building);

  // History section — everything else, or "All Demos" label when no running/building
  const histSec   = document.getElementById('section-history');
  const histLabel = document.getElementById('label-history');
  const histCnt   = document.getElementById('count-history');
  const histGrid  = document.getElementById('grid-history');

  if (running.length || building.length) {
    histLabel.textContent = 'History';
  } else {
    histLabel.textContent = 'All Demos';
  }

  if (rest.length) {
    histCnt.textContent = rest.length;
    histGrid.innerHTML  = rest.map(cardHTML).join('');
    histSec.style.display = '';
  } else if (!running.length && !building.length) {
    histCnt.textContent = '0';
    histGrid.innerHTML  = '<div class="empty"><div class="empty-glyph"><svg viewBox="0 0 24 24" fill="#3f3f46"><path d="M12 2L21.5 7.5V16.5L12 22L2.5 16.5V7.5L12 2Z"/></svg></div><p>No demos yet. Approve a run and the SRE agent will deploy one automatically.</p></div>';
    histSec.style.display = '';
  } else {
    histSec.style.display = 'none';
  }
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function requestStart(id) {
  pendingStartId = id;
  const demo = demos.find(d => d.id === id);
  const n = runningCount();
  const lru = lruDemo();
  document.getElementById('modal-body').textContent = 'Start "' + esc(demo.title) + '"?';
  const warnEl = document.getElementById('modal-warn');
  if (n >= MAX_RUNNING && lru) {
    warnEl.classList.remove('hidden');
    warnEl.textContent = 'Max ' + MAX_RUNNING + ' demos can run simultaneously. Starting this will stop "' + esc(lru.title) + '" (least recently used).';
  } else {
    warnEl.classList.add('hidden');
  }
  document.getElementById('modal-confirm').onclick = confirmStart;
  document.getElementById('modal').classList.remove('hidden');
}

async function confirmStart() {
  closeModal();
  const id = pendingStartId;
  if (!id) return;
  pendingStartId = null;
  try {
    const r = await fetch(API + '/v1/demos/' + id + '/start', { method: 'POST' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = demos.find(d => d.id === id);
    if (d) d.status = 'STARTING';
    render();
    pollStatus(id);
  } catch(e) {
    // Refresh to show current state from server
    await fetchDemos();
  }
}

async function doStop(id) {
  try {
    await fetch(API + '/v1/demos/' + id + '/stop', { method: 'POST' });
    const d = demos.find(d => d.id === id);
    if (d) d.status = 'STOPPED';
    render();
  } catch(e) {
    await fetchDemos();
  }
}

async function deleteDemo(id) {
  demos = demos.filter(d => d.id !== id);
  render();
  await fetch(API + '/v1/demos/' + id, { method: 'DELETE' });
}

async function cleanupFailed() {
  demos = demos.filter(d => d.status !== 'FAILED');
  render();
  await fetch(API + '/v1/demos', { method: 'DELETE' });
}

function closeModal() { document.getElementById('modal').classList.add('hidden'); }

async function pollStatus(id) {
  // Poll up to 3 minutes (36 * 5s). Watches STARTING and BUILDING states.
  for (let i = 0; i < 36; i++) {
    await new Promise(r => setTimeout(r, 5000));
    await fetchDemos();
    const d = demos.find(d => d.id === id);
    if (!d || !['STARTING', 'BUILDING'].includes(d.status)) break;
  }
}

// Also add spin keyframe for building spinner
const style = document.createElement('style');
style.textContent = '@keyframes spin { to { transform: rotate(360deg); } }';
document.head.appendChild(style);

fetchDemos();
setInterval(fetchDemos, 15000);
</script>
</body>
</html>"""


@router.get("/demos", response_class=HTMLResponse, include_in_schema=False)
async def demo_portal():
    """Demo portal — AWS-style list with Start/Stop and LRU warning."""
    from phalanx.config.settings import get_settings as _gs  # noqa: PLC0415
    max_running = _gs().demo_max_running
    return HTMLResponse(content=_PORTAL_HTML.replace("{max_running}", str(max_running)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_out(demo: Demo) -> DemoOut:
    return DemoOut(
        id=demo.id,
        run_id=demo.run_id,
        slug=demo.slug,
        title=demo.title,
        app_type=demo.app_type,
        status=demo.status,
        demo_url=demo.demo_url,
        error=demo.error,
        last_accessed_at=demo.last_accessed_at.isoformat() if demo.last_accessed_at else None,
        built_at=demo.built_at.isoformat() if demo.built_at else None,
        created_at=demo.created_at.isoformat() if demo.created_at else None,
    )
