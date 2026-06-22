import type { TelemetryEvent } from "../types";

/**
 * Parses the optional ```telemetry JSON block a model may emit (see
 * TASK_MODE_TELEMETRY_ADDENDUM in systemPrompt.ts) into TelemetryEvent
 * records. Never throws — a model that doesn't comply with the format
 * (very possible at 0.5B scale) simply yields zero events for that turn.
 */
export function parseTelemetry(messageId: string, content: string): TelemetryEvent[] {
  const match = content.match(/```telemetry\s*\n([\s\S]*?)```/);
  if (!match) return [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(match[1].trim());
  } catch {
    return [];
  }

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
      const taskId = typeof a.task === "string" ? a.task : `task-${i + 1}`;
      const reason = typeof a.reason === "string" ? a.reason : "";
      const command = typeof a.command === "string" ? a.command : "";
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

export interface ParsedFileBlock {
  path: string;
  language: string;
  content: string;
}

/**
 * Finds fenced code blocks explicitly tagged with a save path, e.g.
 * ```python:src/app.py ... ``` — only these are eligible for workspace
 * auto-save. Plain ```python blocks (no path) are left as in-chat
 * snippets and never written to disk, so the model has to opt in
 * per-file rather than everything it ever outputs landing on disk.
 */
export function parseFileBlocks(content: string): ParsedFileBlock[] {
  const blocks: ParsedFileBlock[] = [];
  const re = /```([A-Za-z0-9_+-]+):([^\n`]+)\n([\s\S]*?)```/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    const [, language, rawPath, body] = m;
    const path = rawPath.trim();
    if (!path) continue;
    blocks.push({ path, language, content: body });
  }
  return blocks;
}
