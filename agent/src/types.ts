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

export interface TelemetryEvent {
  id: string;
  messageId: string;
  kind: "plan" | "action";
  /** For "plan" events, the ordered plan steps. */
  planSteps?: string[];
  /** For "action" events, one swarm-worker-reported action. */
  action?: TelemetryAction;
  timestamp: string;
}

export type ThemeId = "phosphor" | "cyberpunk" | "matrix" | "tron" | "amber";
