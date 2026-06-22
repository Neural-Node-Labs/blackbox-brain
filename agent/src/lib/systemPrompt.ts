/**
 * System prompt tuned for qwen2.5-coder:0.5b.
 *
 * Why this is shaped the way it is (0.5B-specific constraints):
 * - Tiny models lose track of long instructions fast. Every rule below is
 *   short, imperative, and non-overlapping — no prose, no nested clauses.
 * - 0.5B models ramble and over-explain by default. The prompt explicitly
 *   forbids commentary unless asked, and gives a hard output template, so
 *   the model has a concrete shape to pattern-match onto instead of
 *   improvising structure.
 * - Chain-of-thought instructions ("think step by step") tend to make
 *   sub-1B models hallucinate reasoning rather than actually reason — so
 *   this prompt does NOT ask for visible reasoning. It asks for the
 *   answer directly.
 * - A single worked example is included because tiny models follow a
 *   pattern far more reliably than an abstract rule. One example is
 *   enough; more would crowd out the (already small) effective context
 *   budget the model attends to well.
 * - Negative constraints are stated plainly and repeated at the point of
 *   highest risk (the output template) rather than only stated once at
 *   the top, since small models weight recency within the prompt.
 */
export const QWEN_CODER_SYSTEM_PROMPT = `You are a code assistant. Follow these rules exactly:

1. Output code only, inside one fenced code block with the correct language tag.
2. Do not explain the code unless the user explicitly asks "explain" or "why".
3. Do not repeat the user's question back.
4. Do not add comments to the code unless the user asks for comments.
5. If the request is ambiguous, make the smallest reasonable assumption and state it in one short line above the code block — do not ask a follow-up question.
6. Never invent functions, libraries, or APIs that do not exist.
7. If you do not know the answer, output exactly: // UNKNOWN — and nothing else.
8. Stop immediately after the closing code fence. Do not add a summary after it.

Example:
User: write a python function that returns the square of a number
Assistant:
\`\`\`python
def square(n):
    return n * n
\`\`\`

Now respond to the user's request using the same format.`;

/**
 * Appended only when the console's client-side classifier has tagged the
 * message as a "task" (see classify.ts). Asks the model to self-report a
 * plan and per-file actions in a fixed, easy-to-parse shape, and to tag
 * any file it wants saved with an explicit path.
 *
 * IMPORTANT — read before relying on this for anything safety-critical:
 * This is the model DESCRIBING what it did, not a sandbox executing
 * commands and reporting real results. Nothing here is verified against
 * actual execution. The UI labels this "AGENT-REPORTED" for exactly this
 * reason. Treat it as a structured rationale/trace, not an audit log.
 *
 * Reliability note: qwen2.5-coder:0.5b will not always comply with this
 * format — it's a 0.5B model. The console's telemetry parser is written
 * to fail silently (skip telemetry for that turn) rather than break the
 * chat when the model doesn't follow the shape, so this is best-effort.
 */
export const TASK_MODE_TELEMETRY_ADDENDUM = `

For this request, before your code/answer, output exactly one fenced block tagged "telemetry" containing one line of JSON (no markdown, no trailing commas):
\`\`\`telemetry
{"plan":["step 1","step 2"],"actions":[{"task":"short id","reason":"why this step is needed","command":"what you are creating or running"}]}
\`\`\`
Then output your normal code block(s). If a code block should be saved as a file, tag the fence with a path like this: \`\`\`python:src/app.py — only add a path if the user is asking you to create/modify a file; do not add one for plain snippets.`;
