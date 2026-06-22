/**
 * Lightweight heuristic classifier, mirroring the "Gateway & Router"
 * concept from the orchestrator design: route a message into CHAT vs
 * TASK before deciding how to visualize the brain workflow.
 *
 * This is intentionally simple — the actual generation always goes
 * through the same /api/chat call against the gateway. The classifier
 * only decides which workflow stages the console lights up, since the
 * gateway itself does not expose a separate planner/swarm API.
 */

const TASK_VERBS = [
  "build",
  "create",
  "generate",
  "deploy",
  "fix",
  "refactor",
  "write",
  "implement",
  "design",
  "debug",
  "analyze",
  "analyse",
  "optimize",
  "optimise",
  "plan",
  "migrate",
  "automate",
  "configure",
  "set up",
  "setup",
  "investigate",
  "research",
];

export type Intent = "chat" | "task";

export function classifyIntent(message: string): Intent {
  const normalized = message.trim().toLowerCase();
  if (!normalized) return "chat";

  const startsWithVerb = TASK_VERBS.some(
    (verb) => normalized.startsWith(verb + " ") || normalized === verb,
  );
  const containsImperative = TASK_VERBS.some((verb) => normalized.includes(` ${verb} `));
  const looksLikeQuestion = /^(who|what|when|where|why|how|is|are|do|does|can|could|would)\b/.test(
    normalized,
  );

  if ((startsWithVerb || containsImperative) && !looksLikeQuestion) {
    return "task";
  }
  return "chat";
}

/**
 * Derives a small set of synthetic sub-tasks for the swarm visualization
 * when intent is "task". Purely cosmetic — the gateway handles the
 * actual single inference call regardless of this breakdown.
 */
export function deriveSwarmGoals(message: string): string[] {
  const trimmed = message.trim().replace(/\s+/g, " ");
  const short = trimmed.length > 60 ? trimmed.slice(0, 57) + "…" : trimmed;
  return [`Gather context: ${short}`, `Execute primary objective`, `Validate against criteria`];
}
