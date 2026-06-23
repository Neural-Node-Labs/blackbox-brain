import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  createProcedure, createSchedule, deleteProcedure, deleteSchedule,
  getProcedure, getRun, getRunLogs, listProcedures, listRuns,
  listSchedules, startRun, updateProcedure,
} from "../api/osirisClient";
import { listSshTargets } from "../api/sshClient";
import { GatewayError } from "../api/gatewayClient";
import type { OsirisBody, ProcedureOut, RunOut, ScheduleOut, StepLogOut, SshTarget } from "../types";

interface Props { token: string }

type View = "list" | "editor" | "runs" | "run_detail" | "schedules";

const STATUS_CLS: Record<string, string> = {
  PENDING: "osi-badge--pending", RUNNING: "osi-badge--running",
  SUCCESS: "osi-badge--success", PARTIAL: "osi-badge--partial",
  FAILED: "osi-badge--failed", COMPLETED: "osi-badge--success",
  SKIPPED: "osi-badge--partial", FAILED_TERMINAL: "osi-badge--failed",
  FAILED_ATTEMPT: "osi-badge--partial",
};

const TERMINAL = new Set(["SUCCESS", "PARTIAL", "FAILED"]);

function fmt(iso?: string) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "medium" }); } catch { return iso; }
}
function dur(ms?: number) { return ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`; }
function descErr(e: unknown) { return e instanceof GatewayError ? e.message.toUpperCase() : e instanceof Error ? e.message : "ERROR"; }

const BLANK_BODY: OsirisBody = {
  name: "", description: "",
  steps: [{ step_id: "step-1", name: "", action: "shell", command: "", depends_on: [], on_failure: "halt", max_retries: 3, reasoning: "" }],
};

export function OsirisPanel({ token }: Props) {
  const [view, setView] = useState<View>("list");
  const [procedures, setProcedures] = useState<ProcedureOut[]>([]);
  const [runs, setRuns] = useState<RunOut[]>([]);
  const [schedules, setSchedules] = useState<ScheduleOut[]>([]);
  const [targets, setTargets] = useState<SshTarget[]>([]);
  const [selectedProc, setSelectedProc] = useState<ProcedureOut | null>(null);
  const [editBody, setEditBody] = useState<string>("");
  const [activeRun, setActiveRun] = useState<RunOut | null>(null);
  const [runLogs, setRunLogs] = useState<StepLogOut[]>([]);
  const [logTarget, setLogTarget] = useState<string | null>(null);
  const [selectedTargets, setSelectedTargets] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // schedule form
  const [schedProcId, setSchedProcId] = useState("");
  const [schedCron, setSchedCron] = useState("0 2 * * *");
  const [schedLabel, setSchedLabel] = useState("");
  const [schedTargets, setSchedTargets] = useState<Set<string>>(new Set());
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, r, s, t] = await Promise.all([listProcedures(token), listRuns(token), listSchedules(token), listSshTargets(token)]);
      setProcedures(p); setRuns(r); setSchedules(s); setTargets(t);
    } catch (e) { setError(descErr(e)); }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  // Poll active run
  useEffect(() => {
    if (!activeRun || TERMINAL.has(activeRun.status)) { if (pollRef.current) clearInterval(pollRef.current); return; }
    pollRef.current = setInterval(async () => {
      try {
        const updated = await getRun(token, activeRun.run_id);
        setActiveRun(updated);
        setRuns(prev => prev.map(r => r.run_id === updated.run_id ? updated : r));
        const logs = await getRunLogs(token, activeRun.run_id, logTarget ?? undefined);
        setRunLogs(logs);
        if (TERMINAL.has(updated.status)) clearInterval(pollRef.current!);
      } catch { /* silent */ }
    }, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [activeRun?.run_id, activeRun?.status, token, logTarget]);

  // ── Editor helpers ──────────────────────────────────────────────────
  async function openNew() {
    setSelectedProc(null);
    setEditBody(JSON.stringify(BLANK_BODY, null, 2));
    setView("editor");
  }

  async function openEdit(proc: ProcedureOut) {
    setError(null);
    try {
      const full = await getProcedure(token, proc.procedure_id);
      setSelectedProc(proc);
      setEditBody(JSON.stringify(full.body, null, 2));
      setView("editor");
    } catch (e) { setError(descErr(e)); }
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setError(null); setBusy(true);
    let body: OsirisBody;
    try { body = JSON.parse(editBody) as OsirisBody; } catch { setError("Invalid JSON in procedure body"); setBusy(false); return; }
    try {
      if (selectedProc) {
        await updateProcedure(token, selectedProc.procedure_id, { name: body.name, description: body.description, body });
      } else {
        await createProcedure(token, { name: body.name, description: body.description, body });
      }
      await load(); setView("list");
    } catch (e) { setError(descErr(e)); } finally { setBusy(false); }
  }

  async function handleDelete(proc: ProcedureOut) {
    if (!confirm(`Delete "${proc.name}"?`)) return;
    setBusy(true); setError(null);
    try { await deleteProcedure(token, proc.procedure_id); await load(); } catch (e) { setError(descErr(e)); } finally { setBusy(false); }
  }

  async function handleRun(proc: ProcedureOut) {
    if (selectedTargets.size === 0) { setError("Select at least one target"); return; }
    setBusy(true); setError(null);
    try {
      const run = await startRun(token, proc.procedure_id, [...selectedTargets]);
      setRuns(prev => [run, ...prev]);
      setActiveRun(run); setRunLogs([]);
      setView("run_detail");
    } catch (e) { setError(descErr(e)); } finally { setBusy(false); }
  }

  function viewRun(run: RunOut) {
    setActiveRun(run); setRunLogs([]); setLogTarget(null);
    getRunLogs(token, run.run_id).then(setRunLogs).catch(() => {});
    setView("run_detail");
  }

  async function handleCreateSchedule(e: FormEvent) {
    e.preventDefault();
    if (!schedProcId || schedTargets.size === 0) { setError("Select procedure and at least one target"); return; }
    setBusy(true); setError(null);
    try {
      await createSchedule(token, { procedure_id: schedProcId, target_ids: [...schedTargets], cron_expr: schedCron, label: schedLabel || undefined });
      await load(); setSchedLabel(""); setSchedProcId(""); setSchedTargets(new Set());
    } catch (e) { setError(descErr(e)); } finally { setBusy(false); }
  }

  async function handleDeleteSchedule(id: string) {
    setBusy(true); setError(null);
    try { await deleteSchedule(token, id); await load(); } catch (e) { setError(descErr(e)); } finally { setBusy(false); }
  }

  function toggleSchedTarget(id: string) {
    setSchedTargets(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }

  return (
    <div className="osi-panel">
      {/* Nav */}
      <div className="osi-nav">
        {(["list","runs","schedules"] as View[]).map(v => (
          <button key={v} className={`osi-nav-btn ${view === v || (view === "editor" && v === "list") || (view === "run_detail" && v === "runs") ? "osi-nav-btn--active" : ""}`}
            onClick={() => setView(v)}>
            {v === "list" ? "PROCEDURES" : v === "runs" ? "RUNS" : "SCHEDULES"}
          </button>
        ))}
        <button className="osi-nav-btn osi-nav-btn--new" onClick={openNew}>+ NEW</button>
      </div>

      {error && <div className="osi-error">⚠ {error}</div>}

      {/* ── Procedure list ── */}
      {view === "list" && (
        <div className="osi-list">
          {procedures.length === 0 && <div className="osi-empty">No procedures. Click + NEW to create one.</div>}
          {procedures.map(p => (
            <div key={p.procedure_id} className="osi-proc-row">
              <div className="osi-proc-info">
                <span className="osi-proc-name">{p.name}</span>
                <span className="osi-proc-meta">v{p.version} · {p.step_count} steps · {fmt(p.updated_at)}</span>
                {p.description && <span className="osi-proc-desc">{p.description}</span>}
              </div>
              <div className="osi-proc-actions">
                <div className="osi-target-select">
                  {targets.map(t => (
                    <label key={t.id} className={`osi-target-chip ${selectedTargets.has(t.id) ? "osi-target-chip--on" : ""}`}>
                      <input type="checkbox" checked={selectedTargets.has(t.id)}
                        onChange={() => setSelectedTargets(prev => { const n = new Set(prev); n.has(t.id) ? n.delete(t.id) : n.add(t.id); return n; })} />
                      {t.label}
                    </label>
                  ))}
                </div>
                <button className="osi-btn osi-btn--run" onClick={() => handleRun(p)} disabled={busy || selectedTargets.size === 0}>▶ RUN</button>
                <button className="osi-btn osi-btn--edit" onClick={() => openEdit(p)} disabled={busy}>EDIT</button>
                <button className="osi-btn osi-btn--del" onClick={() => handleDelete(p)} disabled={busy}>✕</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Procedure editor ── */}
      {view === "editor" && (
        <form className="osi-editor" onSubmit={handleSave}>
          <div className="osi-editor-header">
            <span>{selectedProc ? `EDIT: ${selectedProc.name}` : "NEW PROCEDURE"}</span>
            <button type="button" className="osi-btn" onClick={() => setView("list")}>← BACK</button>
          </div>
          <div className="osi-editor-hint">
            Edit the JSON body below. Required fields per step: step_id, name, action (shell|docker|kubernetes|terraform|http|ssh), command.
          </div>
          <textarea className="osi-editor-ta" value={editBody} onChange={e => setEditBody(e.target.value)} spellCheck={false} />
          <div className="osi-editor-footer">
            <button className="osi-btn osi-btn--save" type="submit" disabled={busy}>{busy ? "SAVING…" : "SAVE"}</button>
          </div>
        </form>
      )}

      {/* ── Run history ── */}
      {view === "runs" && (
        <div className="osi-run-list">
          {runs.length === 0 && <div className="osi-empty">No runs yet.</div>}
          {runs.map(r => (
            <div key={r.run_id} className="osi-run-row" onClick={() => viewRun(r)}>
              <span className={`osi-badge ${STATUS_CLS[r.status] ?? ""}`}>{r.status}</span>
              <span className="osi-run-id">{r.run_id.slice(0, 8)}…</span>
              <span className="osi-run-meta">{r.target_ids.length} tgt · {r.triggered_by} · {fmt(r.created_at)}</span>
            </div>
          ))}
        </div>
      )}

      {/* ── Run detail + step telemetry ── */}
      {view === "run_detail" && activeRun && (
        <div className="osi-run-detail">
          <div className="osi-run-detail-header">
            <button className="osi-btn" onClick={() => setView("runs")}>← RUNS</button>
            <span className={`osi-badge ${STATUS_CLS[activeRun.status] ?? ""}`}>{activeRun.status}</span>
            <span className="osi-run-id">{activeRun.run_id.slice(0, 8)}…</span>
            {!TERMINAL.has(activeRun.status) && <span className="osi-polling">● LIVE</span>}
          </div>

          <div className="osi-target-tabs">
            <button className={`osi-target-tab ${logTarget === null ? "osi-target-tab--active" : ""}`} onClick={() => { setLogTarget(null); getRunLogs(token, activeRun.run_id).then(setRunLogs).catch(()=>{}); }}>ALL</button>
            {activeRun.target_ids.map(tid => (
              <button key={tid} className={`osi-target-tab ${logTarget === tid ? "osi-target-tab--active" : ""}`}
                onClick={() => { setLogTarget(tid); getRunLogs(token, activeRun.run_id, tid).then(setRunLogs).catch(()=>{}); }}>
                {targets.find(t => t.id === tid)?.label ?? tid}
              </button>
            ))}
          </div>

          <div className="osi-step-log-list">
            {runLogs.length === 0 && <div className="osi-empty">Waiting for step telemetry…</div>}
            {runLogs.map(log => <StepLogCard key={log.id} log={log} />)}
          </div>
        </div>
      )}

      {/* ── Schedules ── */}
      {view === "schedules" && (
        <div className="osi-schedules">
          <form className="osi-sched-form" onSubmit={handleCreateSchedule}>
            <div className="osi-section-label">NEW SCHEDULE</div>
            <select className="osi-field-input" value={schedProcId} onChange={e => setSchedProcId(e.target.value)} required>
              <option value="">— select procedure —</option>
              {procedures.map(p => <option key={p.procedure_id} value={p.procedure_id}>{p.name}</option>)}
            </select>
            <input className="osi-field-input" placeholder="Cron (e.g. 0 2 * * *)" value={schedCron} onChange={e => setSchedCron(e.target.value)} required />
            <input className="osi-field-input" placeholder="Label (optional)" value={schedLabel} onChange={e => setSchedLabel(e.target.value)} />
            <div className="osi-target-chips">
              {targets.map(t => (
                <label key={t.id} className={`osi-target-chip ${schedTargets.has(t.id) ? "osi-target-chip--on" : ""}`}>
                  <input type="checkbox" checked={schedTargets.has(t.id)} onChange={() => toggleSchedTarget(t.id)} />
                  {t.label}
                </label>
              ))}
            </div>
            <button className="osi-btn osi-btn--save" type="submit" disabled={busy}>+ SCHEDULE</button>
          </form>

          <div className="osi-section-label" style={{ marginTop: 14 }}>ACTIVE SCHEDULES</div>
          {schedules.length === 0 && <div className="osi-empty">No schedules configured.</div>}
          {schedules.map(s => (
            <div key={s.schedule_id} className="osi-sched-row">
              <div className="osi-sched-info">
                <span className="osi-sched-label">{s.label ?? s.cron_expr}</span>
                <span className="osi-sched-meta">{s.cron_expr} · {procedures.find(p => p.procedure_id === s.procedure_id)?.name ?? s.procedure_id}</span>
                <span className="osi-sched-meta">Targets: {s.target_ids.join(", ")} · Last: {fmt(s.last_run_at)}</span>
              </div>
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <span className={`osi-badge ${s.enabled ? "osi-badge--success" : "osi-badge--pending"}`}>{s.enabled ? "ON" : "OFF"}</span>
                <button className="osi-btn osi-btn--del" onClick={() => handleDeleteSchedule(s.schedule_id)} disabled={busy}>✕</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function StepLogCard({ log }: { log: StepLogOut }) {
  const [open, setOpen] = useState(false);
  const ok = log.status === "COMPLETED" || log.status === "SKIPPED";
  return (
    <div className={`osi-step-card ${ok ? "osi-step-card--ok" : log.status === "FAILED_TERMINAL" ? "osi-step-card--fail" : "osi-step-card--warn"}`}>
      <div className="osi-step-header" onClick={() => setOpen(o => !o)}>
        <span className="osi-step-arrow">{open ? "▾" : "▸"}</span>
        <span className={`osi-badge ${STATUS_CLS[log.status] ?? ""}`}>{log.status}</span>
        <span className="osi-step-name">{log.step_name ?? log.step_id}</span>
        <span className="osi-step-target">{log.target_id}</span>
        {log.attempt > 1 && <span className="osi-step-attempt">attempt {log.attempt}</span>}
        <span className="osi-step-dur">{dur(log.duration_ms)}</span>
      </div>
      {open && (
        <div className="osi-step-body">
          {log.reasoning && <div className="osi-tele-row"><span className="osi-tele-label">REASONING</span>{log.reasoning}</div>}
          {log.action && <div className="osi-tele-row"><span className="osi-tele-label">ACTION</span>{log.action}</div>}
          {log.command && <pre className="osi-tele-cmd">{log.command}</pre>}
          {log.fix_applied && <div className="osi-tele-row osi-tele-fix"><span className="osi-tele-label">FIX APPLIED</span>{log.fix_applied}</div>}
          {log.stdout && <><div className="osi-tele-label-sm">STDOUT</div><pre className="osi-output">{log.stdout}</pre></>}
          {log.stderr && <><div className="osi-tele-label-sm osi-tele-label-sm--err">STDERR</div><pre className="osi-output osi-output--err">{log.stderr}</pre></>}
          {log.exit_code != null && <div className="osi-tele-row"><span className="osi-tele-label">EXIT CODE</span><span className={log.exit_code === 0 ? "osi-exit--ok" : "osi-exit--fail"}>{log.exit_code}</span></div>}
        </div>
      )}
    </div>
  );
}
