export type WorkflowStage =
  | "idle"
  | "classifying"
  | "chat"
  | "planning"
  | "swarm"
  | "consolidating"
  | "done"
  | "error";

export type MessageRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  /** When true, the assistant message is still streaming in. */
  streaming?: boolean;
}

export interface SwarmTask {
  id: string;
  goal: string;
  status: "PENDING" | "RUNNING" | "COMPLETED";
}

export interface GatewayLoginResponse {
  token: string;
  expires_at: string;
}

export interface GatewaySession {
  token: string;
  expiresAt: string;
}

export interface ProjectInfo {
  name: string;
  file_count: number;
  total_bytes: number;
}

export interface FileInfo {
  path: string;
  size_bytes: number;
  modified_at: string;
}

export interface TelemetryAction {
  taskId: string;
  reason: string;
  command: string;
}

/** Observable metrics captured from the client side — never depends on
 *  model compliance. Always emitted for every completed turn. */
export interface ObservableMetrics {
  classification: "chat" | "task";
  durationMs: number;
  /** Rough estimate: content.length / 4 */
  tokenEstimate: number;
  filesDetected: string[];
  filesSaved: string[];
  errorOccurred: boolean;
}

export type TelemetryEventKind = "plan" | "action" | "observable" | "extraction";

export interface TelemetryEvent {
  id: string;
  messageId: string;
  kind: TelemetryEventKind;
  timestamp: string;
  /** kind === "plan" */
  planSteps?: string[];
  /** kind === "action" */
  action?: TelemetryAction;
  /** kind === "observable" */
  observable?: ObservableMetrics;
  /** kind === "extraction" — post-hoc model summary, best-effort */
  extractedSummary?: string;
}

export type ThemeId = "phosphor" | "cyberpunk" | "matrix" | "tron" | "amber";

// SSH remote execution
export interface SshTarget {
  id: string;
  label: string;
  host: string;
  user: string;
  port: number;
  auth: string;
  tags: string[];
}

export interface SshTargetResult {
  stdout: string;
  stderr: string;
  exit_code: number;
  error: string | null;
  duration_ms: number;
}

export interface SshJob {
  job_id: string;
  status: "PENDING" | "RUNNING" | "SUCCESS" | "PARTIAL" | "FAILED";
  target_ids: string[];
  procedure: string;
  label: string | null;
  results: Record<string, SshTargetResult> | null;
  created_at: string;
  completed_at: string | null;
}

export interface SshTargetInfo {
  id: string;
  label: string;
  host: string;
  user: string;
  port: number;
  auth: string;
  tags: string[];
}

export interface SshTargetResult {
  stdout: string;
  stderr: string;
  exit_code: number;
  error: string | null;
  duration_ms: number;
}

export interface SshJob {
  job_id: string;
  status: "PENDING" | "RUNNING" | "SUCCESS" | "PARTIAL" | "FAILED";
  target_ids: string[];
  procedure: string;
  label: string | null;
  results: Record<string, SshTargetResult> | null;
  created_at: string;
  completed_at: string | null;
}

// ── Osiris Procedure Engine ──────────────────────────────────────────────

export interface OsirisValidationSpec {
  type: string;
  expected?: string;
  path?: string;
}

export interface OsirisStep {
  step_id: string;
  name: string;
  action: string;
  command: string;
  depends_on: string[];
  validation?: OsirisValidationSpec;
  on_failure: string;
  max_retries: number;
  reasoning?: string;
}

export interface OsirisBody {
  name: string;
  description?: string;
  steps: OsirisStep[];
}

export interface ProcedureOut {
  procedure_id: string;
  name: string;
  description?: string;
  version: number;
  step_count: number;
  created_at: string;
  updated_at: string;
}

export interface StepLogOut {
  id: number;
  run_id: string;
  target_id: string;
  step_id: string;
  step_name?: string;
  action?: string;
  command?: string;
  reasoning?: string;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  status: string;
  attempt: number;
  fix_applied?: string;
  duration_ms?: number;
  logged_at: string;
}

export interface RunOut {
  run_id: string;
  procedure_id?: string;
  status: "PENDING" | "RUNNING" | "SUCCESS" | "PARTIAL" | "FAILED";
  target_ids: string[];
  triggered_by: string;
  results?: Record<string, unknown>;
  created_at: string;
  completed_at?: string;
}

export interface ScheduleOut {
  schedule_id: string;
  procedure_id: string;
  target_ids: string[];
  cron_expr: string;
  label?: string;
  enabled: boolean;
  last_run_at?: string;
  next_run_at?: string;
  created_at: string;
}
