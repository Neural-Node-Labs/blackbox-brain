import type { SwarmTask, WorkflowStage } from "../types";

interface Props {
  stage: WorkflowStage;
  intent: "chat" | "task" | null;
  swarmTasks: SwarmTask[];
}

interface NodeDef {
  key: string;
  label: string;
  detail?: string;
  /** Stages at which this node should render as "done" once passed. */
  satisfiedBy: WorkflowStage[];
  activeOn: WorkflowStage[];
}

const CHAT_NODES: NodeDef[] = [
  { key: "classify", label: "01 · CLASSIFIER", detail: "Routing intent", activeOn: ["classifying"], satisfiedBy: ["chat", "done"] },
  { key: "respond", label: "02 · DIRECT RESPONSE", detail: "Single-pass reply", activeOn: ["chat"], satisfiedBy: ["done"] },
];

const TASK_NODES: NodeDef[] = [
  { key: "classify", label: "01 · CLASSIFIER", detail: "Routing intent", activeOn: ["classifying"], satisfiedBy: ["planning", "swarm", "consolidating", "done"] },
  { key: "plan", label: "02 · ORCHESTRATOR / PLANNER", detail: "Decomposing objective", activeOn: ["planning"], satisfiedBy: ["swarm", "consolidating", "done"] },
  { key: "swarm", label: "03 · SWARM EXECUTION", detail: "Parallel ReAct workers · max 3 loops", activeOn: ["swarm"], satisfiedBy: ["consolidating", "done"] },
  { key: "consolidate", label: "04 · CONSOLIDATION", detail: "Merging task artifacts", activeOn: ["consolidating"], satisfiedBy: ["done"] },
];

function nodeStatus(node: NodeDef, stage: WorkflowStage): "idle" | "active" | "done" | "error" {
  if (stage === "error") return "idle";
  if (node.satisfiedBy.includes(stage)) return "done";
  if (node.activeOn.includes(stage)) return "active";
  return "idle";
}

export function BrainWorkflow({ stage, intent, swarmTasks }: Props) {
  const nodes = intent === "task" ? TASK_NODES : CHAT_NODES;

  return (
    <div className="workflow">
      {nodes.map((node, i) => {
        const status = nodeStatus(node, stage);
        const isLast = i === nodes.length - 1;
        return (
          <div key={node.key} className={`workflow__node workflow__node--${status}`}>
            {!isLast && <span className="workflow__connector" />}
            <span className="workflow__bullet">{status === "done" ? "✓" : i + 1}</span>
            <div>
              <div className="workflow__label">{node.label}</div>
              {node.detail && <div className="workflow__detail">{node.detail}</div>}

              {node.key === "swarm" && swarmTasks.length > 0 && (
                <div className="swarm-lanes">
                  {swarmTasks.map((t) => (
                    <div
                      key={t.id}
                      className={`swarm-lane ${
                        t.status === "RUNNING"
                          ? "swarm-lane--running"
                          : t.status === "COMPLETED"
                            ? "swarm-lane--completed"
                            : ""
                      }`}
                    >
                      <span className="swarm-lane__status" />
                      <span className="swarm-lane__goal">{t.goal}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}

      {stage === "error" && (
        <div className="workflow__node workflow__node--error">
          <span className="workflow__bullet">!</span>
          <div>
            <div className="workflow__label">PIPELINE FAULT</div>
            <div className="workflow__detail">Transmission interrupted — see console log</div>
          </div>
        </div>
      )}
    </div>
  );
}
