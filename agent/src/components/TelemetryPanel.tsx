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
          Send a task-style directive (e.g. "build a function that…") to
          see the orchestrator's plan and the swarm's reported actions
          here.
        </div>
      </div>
    );
  }

  return (
    <div className="telemetry">
      <div className="telemetry__disclaimer">
        ⚠ AGENT-REPORTED — the model describes its plan/actions; nothing
        here is independently verified or executed.
      </div>
      {events.map((ev) => (
        <div key={ev.id} className={`telemetry__entry telemetry__entry--${ev.kind}`}>
          {ev.kind === "plan" ? (
            <>
              <div className="telemetry__entry-header">
                <span className="telemetry__badge telemetry__badge--plan">PLAN</span>
                <span className="telemetry__time">{formatTime(ev.timestamp)}</span>
              </div>
              <ol className="telemetry__plan-list">
                {ev.planSteps?.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </>
          ) : (
            <>
              <div className="telemetry__entry-header">
                <span className="telemetry__badge telemetry__badge--action">ACTION</span>
                <span className="telemetry__task-id">{ev.action?.taskId}</span>
                <span className="telemetry__time">{formatTime(ev.timestamp)}</span>
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
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}
