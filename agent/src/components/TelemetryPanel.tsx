import type { TelemetryEvent } from "../types";

interface Props {
  events: TelemetryEvent[];
}

export function TelemetryPanel({ events }: Props) {
  if (events.length === 0) {
    return (
      <div className="telemetry">
        <div className="telemetry__empty">
          NO TELEMETRY YET.
          <br />
          Send any message to see observable metrics here. Task-style
          directives ("build", "create", "fix"…) also trigger plan/action
          extraction.
        </div>
      </div>
    );
  }

  // Group events by messageId so each turn shows as one block
  const grouped = groupByMessage(events);

  return (
    <div className="telemetry">
      {grouped.map(([msgId, msgEvents]) => (
        <TurnBlock key={msgId} events={msgEvents} />
      ))}
    </div>
  );
}

function groupByMessage(events: TelemetryEvent[]): [string, TelemetryEvent[]][] {
  const map = new Map<string, TelemetryEvent[]>();
  for (const e of events) {
    if (!map.has(e.messageId)) map.set(e.messageId, []);
    map.get(e.messageId)!.push(e);
  }
  return [...map.entries()].reverse(); // newest first
}

function TurnBlock({ events }: { events: TelemetryEvent[] }) {
  const obs = events.find((e) => e.kind === "observable");
  const plans = events.filter((e) => e.kind === "plan");
  const actions = events.filter((e) => e.kind === "action");
  const isTask = obs?.observable?.classification === "task";

  return (
    <div className="telemetry__turn">
      {/* Always-present observable summary */}
      {obs?.observable && (
        <div className="telemetry__observable">
          <div className="telemetry__obs-row">
            <span className={`telemetry__class-badge telemetry__class-badge--${obs.observable.classification}`}>
              {obs.observable.classification.toUpperCase()}
            </span>
            <span className="telemetry__obs-stat">
              ~{obs.observable.tokenEstimate} tok
            </span>
            <span className="telemetry__obs-stat">
              {(obs.observable.durationMs / 1000).toFixed(1)}s
            </span>
            <span className="telemetry__time">{formatTime(obs.timestamp)}</span>
          </div>

          {obs.observable.errorOccurred && (
            <div className="telemetry__obs-error">⚠ TRANSMISSION FAILED</div>
          )}

          {obs.observable.filesDetected.length > 0 && (
            <div className="telemetry__files">
              {obs.observable.filesDetected.map((f) => {
                const saved = obs.observable!.filesSaved.includes(f);
                return (
                  <span key={f} className={`telemetry__file ${saved ? "telemetry__file--saved" : ""}`}>
                    {saved ? "💾 " : "📄 "}{f}
                  </span>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Orchestrator plan */}
      {isTask && plans.length > 0 && (
        <div className="telemetry__entry telemetry__entry--plan">
          <div className="telemetry__entry-header">
            <span className="telemetry__badge telemetry__badge--plan">PLAN</span>
            <span className="telemetry__source">
              {plans[0].id.includes("extracted") ? "post-hoc" : "inline"}
            </span>
          </div>
          <ol className="telemetry__plan-list">
            {plans[0].planSteps?.map((step, i) => <li key={i}>{step}</li>)}
          </ol>
        </div>
      )}

      {/* Swarm actions */}
      {isTask && actions.length > 0 && (
        <div className="telemetry__actions">
          {actions.map((ev) => (
            <div key={ev.id} className="telemetry__entry telemetry__entry--action">
              <div className="telemetry__entry-header">
                <span className="telemetry__badge telemetry__badge--action">ACTION</span>
                <span className="telemetry__task-id">{ev.action?.taskId}</span>
                <span className="telemetry__source">
                  {ev.id.includes("extracted") ? "post-hoc" : "inline"}
                </span>
              </div>
              {ev.action?.reason && (
                <div className="telemetry__field">
                  <span className="telemetry__field-label">REASON</span>
                  {ev.action.reason}
                </div>
              )}
              {ev.action?.command && (
                <div className="telemetry__field telemetry__field--command">
                  <span className="telemetry__field-label">COMMAND</span>
                  {ev.action.command}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Task with no plan/actions yet */}
      {isTask && plans.length === 0 && actions.length === 0 && (
        <div className="telemetry__pending">
          Extracting plan… (post-hoc pass running in background)
        </div>
      )}
    </div>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch { return iso; }
}
