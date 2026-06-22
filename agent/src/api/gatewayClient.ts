import type { GatewayLoginResponse } from "../types";

/**
 * Thin client for the FastAPI gateway. Talks to /api/auth/login and
 * /api/chat. No token is ever persisted to localStorage/sessionStorage —
 * it lives only in memory for the session, per the gateway's own
 * short-lived bearer token model.
 */

export class GatewayError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
    this.name = "GatewayError";
  }
}

export async function login(username: string, password: string): Promise<GatewayLoginResponse> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

  if (!res.ok) {
    const detail = await safeDetail(res);
    throw new GatewayError(detail ?? `Login failed (${res.status})`, res.status);
  }

  return (await res.json()) as GatewayLoginResponse;
}

export interface ChatStreamHandlers {
  onToken: (text: string) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

/**
 * Streams a chat completion from the gateway's /api/chat endpoint, which
 * proxies raw NDJSON chunks from Ollama. Each line is a JSON object; for
 * the /api/chat (non-generate) endpoint, Ollama emits
 * { message: { role, content }, done: boolean, ... } per line.
 */
export async function streamChat(
  token: string,
  model: string,
  messages: { role: string; content: string }[],
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ model, messages, stream: true }),
      signal,
    });
  } catch (err) {
    handlers.onError(err instanceof Error ? err.message : "Network error contacting gateway");
    return;
  }

  if (!res.ok || !res.body) {
    const detail = await safeDetail(res);
    handlers.onError(detail ?? `Gateway returned ${res.status}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) continue;
        handleLine(line, handlers);
      }
    }
    // Flush whatever remains in the buffer after the stream closes.
    const tail = buffer.trim();
    if (tail) handleLine(tail, handlers);

    handlers.onDone();
  } catch (err) {
    if (signal?.aborted) {
      handlers.onDone();
      return;
    }
    handlers.onError(err instanceof Error ? err.message : "Stream interrupted");
  }
}

function handleLine(line: string, handlers: ChatStreamHandlers): void {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    // Non-JSON line from a misbehaving upstream — surface raw text rather
    // than silently dropping it, but never throw and kill the stream.
    handlers.onToken(line);
    return;
  }

  if (isErrorPayload(parsed)) {
    handlers.onError(parsed.error);
    return;
  }

  const content = extractContent(parsed);
  if (content) handlers.onToken(content);
}

function isErrorPayload(value: unknown): value is { error: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as { error: unknown }).error === "string"
  );
}

function extractContent(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const obj = value as Record<string, unknown>;

  // Ollama /api/chat shape: { message: { role, content }, done }
  if (typeof obj.message === "object" && obj.message !== null) {
    const msg = obj.message as Record<string, unknown>;
    if (typeof msg.content === "string") return msg.content;
  }

  // Fallback: Ollama /api/generate shape: { response: "..." }
  if (typeof obj.response === "string") return obj.response;

  return null;
}

async function safeDetail(res: Response): Promise<string | null> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* response wasn't JSON — ignore */
  }
  return null;
}
