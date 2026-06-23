import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { executeSshJob, getSshJob, listSshJobs, listSshTargets } from "../api/sshClient";
import { GatewayError } from "../api/gatewayClient";
import type { SshJob, SshTarget, SshTargetResult } from "../types";

interface Props { token: string }

const STATUS_COLOR: Record<string, string> = {
  PENDING: "ssh-badge--pending",
  RUNNING: "ssh-badge--running",
  SUCCESS: "ssh-badge--success",
  PARTIAL: "ssh-badge--partial",
  FAILED:  "ssh-badge--failed",
};

function fmt(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}
function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  catch { return iso; }
}
function descErr(err: unknown): string {
  if (err instanceof GatewayError) return err.message.toUpperCase();
  return err instanceof Error ? err.message : "UNKNOWN ERROR";
}

// Auto-poll running/pending jobs every 2 s
const POLL_MS = 2000;
const TERMINAL_STATES = new Set(["SUCCESS", "PARTIAL", "FAILED"]);

export function SSHPanel({ token }: Props) {
  const [targets, setTargets]         = useState<SshTarget[]>([]);
  const [selected, setSelected]       = useState<Set<string>>(new Set());
  const [procedure, setProcedure]     = useState("");
  const [label, setLabel]             = useState("");
  const [timeout, setTimeout_]        = useState(60);
  const [jobs, setJobs]               = useState<SshJob[]>([]);
  const [activeJob, setActiveJob]     = useState<SshJob | null>(null);
  const [busy, setBusy]               = useState(false);
  const [error, setError]             = useState<string | null>(null);
  const [noTargets, setNoTargets]     = useState(false);
  const pollRef                       = useRef<ReturnType<typeof setInterval> | null>(null);

  // Load targets once on mount
  useEffect(() => {
    listSshTargets(token)
      .then(t => { setTargets(t); if (t.length === 0) setNoTargets(true); })
      .catch(() => setNoTargets(true));
  }, [token]);

  // Load job history
  const refreshJobs = useCallback(async () => {
    try {
      const list = await listSshJobs(token, 30);
      setJobs(list);
    } catch { /* silent */ }
  }, [token]);

  useEffect(() => { void refreshJobs(); }, [refreshJobs]);

  // Auto-poll the active job while it is running/pending
  useEffect(() => {
    if (!activeJob || TERMINAL_STATES.has(activeJob.status)) {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    pollRef.current = setInterval(async () => {
      try {
        const updated = await getSshJob(token, activeJob.job_id);
        setActiveJob(updated);
        setJobs(prev => prev.map(j => j.job_id === updated.job_id ? updated : j));
        if (TERMINAL_STATES.has(updated.status)) {
          clearInterval(pollRef.current!);
        }
      } catch { /* silent, will retry */ }
    }, POLL_MS);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [activeJob?.job_id, activeJob?.status, token]);

  function toggleTarget(id: string) {
    setSelected(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  async function handleRun(e: FormEvent) {
    e.preventDefault();
    if (selected.size === 0 || !procedure.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const job = await executeSshJob(token, {
        target_ids: [...selected],
        procedure: procedure.trim(),
        timeout,
        label: label.trim() || undefined,
      });
      setJobs(prev => [job, ...prev]);
      setActiveJob(job);
    } catch (err) {
      setError(descErr(err));
    } finally {
      setBusy(false);
    }
  }

  function selectJob(job: SshJob) {
    setActiveJob(job);
  }

  return (
    <div className="ssh-panel">
      {/* ── Left column: targets + form ── */}
      <form className="ssh-form" onSubmit={handleRun}>
        <div className="ssh-section-label">TARGETS</div>

        {noTargets && (
          <div className="ssh-no-targets">
            No targets configured.
            <br />
            Copy <code>ssh/targets.json.example</code> to <code>ssh/targets.json</code> and restart the gateway.
          </div>
        )}

        <div className="ssh-target-list">
          {targets.map(t => (
            <label key={t.id} className={`ssh-target ${selected.has(t.id) ? "ssh-target--selected" : ""}`}>
              <input
                type="checkbox"
                checked={selected.has(t.id)}
                onChange={() => toggleTarget(t.id)}
                className="ssh-target__cb"
              />
              <span className="ssh-target__label">{t.label}</span>
              <span className="ssh-target__meta">
                {t.user}@{t.host}:{t.port}
              </span>
              {t.tags.length > 0 && (
                <span className="ssh-target__tags">{t.tags.join(" · ")}</span>
              )}
            </label>
          ))}
        </div>

        <div className="ssh-section-label" style={{ marginTop: 12 }}>PROCEDURE</div>
        <textarea
          className="ssh-procedure"
          value={procedure}
          onChange={(e: ChangeEvent<HTMLTextAreaElement>) => setProcedure(e.target.value)}
          placeholder={"# single command or multi-line shell script\nuname -a\ndf -h"}
          spellCheck={false}
          rows={6}
        />

        <div className="ssh-controls">
          <div className="ssh-field">
            <span className="ssh-field__label">LABEL (opt)</span>
            <input className="ssh-field__input" value={label} onChange={e => setLabel(e.target.value)} placeholder="e.g. health-check" maxLength={200} />
          </div>
          <div className="ssh-field">
            <span className="ssh-field__label">TIMEOUT (s)</span>
            <input className="ssh-field__input ssh-field__input--small" type="number" value={timeout} min={5} max={600}
              onChange={e => setTimeout_(Math.max(5, Math.min(600, Number(e.target.value))))} />
          </div>
        </div>

        {error && <div className="ssh-error">⚠ {error}</div>}

        <button
          className="ssh-run-btn"
          type="submit"
          disabled={busy || selected.size === 0 || !procedure.trim()}
        >
          {busy ? "DISPATCHING…" : `▶ RUN ON ${selected.size || "?"} TARGET${selected.size !== 1 ? "S" : ""}`}
        </button>
      </form>

      {/* ── Right column: job history + detail ── */}
      <div className="ssh-results">
        <div className="ssh-section-label">JOB HISTORY</div>
        <div className="ssh-job-list">
          {jobs.length === 0 && <div className="ssh-no-targets">No jobs yet.</div>}
          {jobs.map(j => (
            <div
              key={j.job_id}
              className={`ssh-job-row ${activeJob?.job_id === j.job_id ? "ssh-job-row--active" : ""}`}
              onClick={() => selectJob(j)}
            >
              <span className={`ssh-badge ${STATUS_COLOR[j.status] ?? ""}`}>{j.status}</span>
              <span className="ssh-job-row__label">{j.label ?? j.procedure.slice(0, 40).replace(/\n/g, " ↵")}</span>
              <span className="ssh-job-row__meta">{j.target_ids.length} tgt · {fmtTime(j.created_at)}</span>
            </div>
          ))}
        </div>

        {activeJob && (
          <div className="ssh-detail">
            <div className="ssh-detail__header">
              <span className={`ssh-badge ${STATUS_COLOR[activeJob.status] ?? ""}`}>{activeJob.status}</span>
              <span className="ssh-detail__id">{activeJob.job_id.slice(0, 8)}…</span>
              {!TERMINAL_STATES.has(activeJob.status) && (
                <span className="ssh-detail__polling">● POLLING</span>
              )}
            </div>

            {activeJob.results
              ? Object.entries(activeJob.results).map(([tid, res]) => (
                  <TargetResult key={tid} targetId={tid} targets={targets} res={res as SshTargetResult} />
                ))
              : <div className="ssh-no-targets">Waiting for results…</div>
            }
          </div>
        )}
      </div>
    </div>
  );
}

function TargetResult({ targetId, targets, res }: { targetId: string; targets: SshTarget[]; res: SshTargetResult }) {
  const tgt = targets.find(t => t.id === targetId);
  const label = tgt ? tgt.label : targetId;
  const ok = res.exit_code === 0 && !res.error;
  const [expanded, setExpanded] = useState(true);

  return (
    <div className={`ssh-target-result ${ok ? "ssh-target-result--ok" : "ssh-target-result--fail"}`}>
      <div className="ssh-target-result__header" onClick={() => setExpanded(e => !e)}>
        <span className="ssh-target-result__arrow">{expanded ? "▾" : "▸"}</span>
        <span className="ssh-target-result__label">{label}</span>
        <span className={`ssh-exit ${ok ? "ssh-exit--ok" : "ssh-exit--fail"}`}>
          exit {res.exit_code}
        </span>
        <span className="ssh-target-result__dur">{fmt(res.duration_ms)}</span>
      </div>
      {expanded && (
        <div className="ssh-target-result__body">
          {res.error && <div className="ssh-output ssh-output--error">ERR: {res.error}</div>}
          {res.stdout && (
            <>
              <div className="ssh-output-label">STDOUT</div>
              <pre className="ssh-output">{res.stdout}</pre>
            </>
          )}
          {res.stderr && (
            <>
              <div className="ssh-output-label">STDERR</div>
              <pre className="ssh-output ssh-output--stderr">{res.stderr}</pre>
            </>
          )}
          {!res.error && !res.stdout && !res.stderr && (
            <div className="ssh-output ssh-output--empty">(no output)</div>
          )}
        </div>
      )}
    </div>
  );
}
