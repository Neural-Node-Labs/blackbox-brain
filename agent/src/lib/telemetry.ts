import type { ObservableMetrics, TelemetryEvent } from "../types";

// ---------------------------------------------------------------------------
// Layer 1 — inline parse
// Tries to parse the ```telemetry block the model may emit mid-generation.
// Will be empty most of the time at 0.5B scale but costs nothing to attempt.
// ---------------------------------------------------------------------------
export function parseTelemetry(messageId: string, content: string): TelemetryEvent[] {
  const match = content.match(/```telemetry\s*\n([\s\S]*?)```/);
  if (!match) return [];

  let parsed: unknown;
  try { parsed = JSON.parse(match[1].trim()); } catch { return []; }

  if (typeof parsed !== "object" || parsed === null) return [];
  const obj = parsed as Record<string, unknown>;
  const events: TelemetryEvent[] = [];
  const now = new Date().toISOString();

  if (Array.isArray(obj.plan)) {
    const steps = obj.plan.filter((s): s is string => typeof s === "string").slice(0, 20);
    if (steps.length > 0) {
      events.push({ id: `${messageId}-plan`, messageId, kind: "plan", planSteps: steps, timestamp: now });
    }
  }

  if (Array.isArray(obj.actions)) {
    obj.actions.slice(0, 20).forEach((raw, i) => {
      if (typeof raw !== "object" || raw === null) return;
      const a = raw as Record<string, unknown>;
      const taskId   = typeof a.task    === "string" ? a.task    : `task-${i + 1}`;
      const reason   = typeof a.reason  === "string" ? a.reason  : "";
      const command  = typeof a.command === "string" ? a.command : "";
      if (!reason && !command) return;
      events.push({
        id: `${messageId}-action-${i}`,
        messageId,
        kind: "action",
        action: { taskId, reason, command },
        timestamp: now,
      });
    });
  }

  return events;
}

// ---------------------------------------------------------------------------
// Layer 2 — observable metrics
// Captures client-side measurable facts for every completed turn.
// Zero model cooperation required.
// ---------------------------------------------------------------------------
export function buildObservableEvent(
  messageId: string,
  metrics: ObservableMetrics,
): TelemetryEvent {
  return {
    id: `${messageId}-observable`,
    messageId,
    kind: "observable",
    observable: metrics,
    timestamp: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Layer 3 — post-hoc extraction
// After streaming completes, makes ONE extra non-streaming call asking
// the model to summarise what it just produced as JSON.
// Dramatically more reliable than asking it to do so mid-generation
// because the extraction prompt is the ONLY task in that request.
//
// Uses stream:false + low temperature so the model focuses on JSON format.
// Fails silently — if the model doesn't comply, zero events are returned.
// ---------------------------------------------------------------------------

const EXTRACTION_PROMPT = (response: string) =>
  `You just produced the following response. Extract a structured summary.
Return ONLY a single line of JSON in this exact shape — nothing else, no markdown, no explanation:
{"plan":["step1","step2"],"actions":[{"task":"T1","reason":"why","command":"what you created"}]}

Your response was:
${response.slice(0, 2000)}`;

interface RawMessage { role: string; content: string }

/** Calls /api/chat with stream:false and returns the first text block. */
async function fetchSync(
  token: string,
  model: string,
  messages: RawMessage[],
): Promise<string> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      model,
      messages,
      stream: false,
      options: { temperature: 0.1, num_predict: 256 },
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = await res.json() as Record<string, unknown>;
  const msg = body.message as Record<string, unknown> | undefined;
  if (typeof msg?.content === "string") return msg.content;
  // Non-streaming Ollama also sometimes returns NDJSON — try parsing last line
  const text = typeof body.response === "string" ? body.response : JSON.stringify(body);
  return text;
}

export async function extractPostHoc(
  token: string,
  model: string,
  messageId: string,
  assistantContent: string,
): Promise<TelemetryEvent[]> {
  if (!assistantContent.trim()) return [];

  let raw: string;
  try {
    raw = await fetchSync(token, model, [
      { role: "user", content: EXTRACTION_PROMPT(assistantContent) },
    ]);
  } catch {
    return [];   // network / auth failure — silent
  }

  // Strip any markdown fences the model adds despite being told not to
  const cleaned = raw.replace(/```(?:json)?/g, "").replace(/```/g, "").trim();

  // Find first JSON object in the response
  const jsonMatch = cleaned.match(/\{[\s\S]*\}/);
  if (!jsonMatch) return [];

  let parsed: unknown;
  try { parsed = JSON.parse(jsonMatch[0]); } catch { return []; }

  if (typeof parsed !== "object" || parsed === null) return [];
  const obj = parsed as Record<string, unknown>;
  const events: TelemetryEvent[] = [];
  const now = new Date().toISOString();

  if (Array.isArray(obj.plan)) {
    const steps = obj.plan.filter((s): s is string => typeof s === "string").slice(0, 20);
    if (steps.length > 0) {
      events.push({ id: `${messageId}-extracted-plan`, messageId, kind: "plan", planSteps: steps, timestamp: now });
    }
  }

  if (Array.isArray(obj.actions)) {
    obj.actions.slice(0, 20).forEach((raw, i) => {
      if (typeof raw !== "object" || raw === null) return;
      const a = raw as Record<string, unknown>;
      events.push({
        id: `${messageId}-extracted-action-${i}`,
        messageId,
        kind: "action",
        action: {
          taskId: typeof a.task === "string" ? a.task : `task-${i + 1}`,
          reason:  typeof a.reason  === "string" ? a.reason  : "",
          command: typeof a.command === "string" ? a.command : "",
        },
        timestamp: now,
      });
    });
  }

  return events;
}

// ---------------------------------------------------------------------------
// File-block parser (unchanged)
// ---------------------------------------------------------------------------
export interface ParsedFileBlock { path: string; language: string; content: string }

export function parseFileBlocks(content: string): ParsedFileBlock[] {
  const blocks: ParsedFileBlock[] = [];
  const re = /```([A-Za-z0-9_+-]+):([^\n`]+)\n([\s\S]*?)```/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    const path = m[2].trim();
    if (!path) continue;
    blocks.push({ path, language: m[1], content: m[3] });
  }
  return blocks;
}
