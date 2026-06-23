import { useCallback, useEffect, useRef, useState } from "react";
import "./styles/theme.css";
import "./styles/themes.css";
import "./styles/layout.css";
import "./styles/console.css";
import "./styles/workflow.css";
import "./styles/workspace.css";
import "./styles/telemetry.css";
import "./styles/ssh.css";
import "./styles/osiris.css";

import { LoginScreen } from "./components/LoginScreen";
import { ConsoleHeader } from "./components/ConsoleHeader";
import { ChatConsole } from "./components/ChatConsole";
import { InputBar } from "./components/InputBar";
import { BrainWorkflow } from "./components/BrainWorkflow";
import { WorkspacePanel } from "./components/WorkspacePanel";
import { TelemetryPanel } from "./components/TelemetryPanel";
import { SSHPanel } from "./components/SSHPanel";
import { OsirisPanel } from "./components/OsirisPanel";
import { streamChat } from "./api/gatewayClient";
import { uploadFile } from "./api/workspaceClient";
import { classifyIntent, deriveSwarmGoals } from "./lib/classify";
import { QWEN_CODER_SYSTEM_PROMPT, TASK_MODE_TELEMETRY_ADDENDUM } from "./lib/systemPrompt";
import { parseFileBlocks, parseTelemetry, buildObservableEvent, extractPostHoc } from "./lib/telemetry";
import type {
  ChatMessage,
  GatewaySession,
  SwarmTask,
  TelemetryEvent,
  ThemeId,
  WorkflowStage,
} from "./types";

// Strip leading dashes and whitespace that can appear when the
// VITE_OLLAMA_MODEL build-arg is resolved from a compose variable
// with a malformed :-- default (e.g. ${OLLAMA_MODEL:--qwen2.5-coder:0.5b}).
const MODEL = ((import.meta.env.VITE_OLLAMA_MODEL as string | undefined) ?? "qwen2.5-coder:0.5b")
  .trim()
  .replace(/^-+/, "")
  || "qwen2.5-coder:0.5b";
const THEME_STORAGE_KEY = "blackbox-console-theme";

let idCounter = 0;
function nextId(): string {
  idCounter += 1;
  return `m${idCounter}-${Date.now()}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadStoredTheme(): ThemeId {
  if (typeof window === "undefined") return "phosphor";
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  const valid: ThemeId[] = ["phosphor", "cyberpunk", "matrix", "tron", "amber"];
  return valid.includes(stored as ThemeId) ? (stored as ThemeId) : "phosphor";
}

/** Rejects any path that could escape the project directory client-side,
 * matching (defensively, not as the source of truth — the gateway still
 * validates server-side) the traversal rules the gateway enforces. */
function isSafeRelativePath(path: string): boolean {
  if (!path || path.startsWith("/")) return false;
  const parts = path.split("/");
  return !parts.some((p) => p === ".." || p === "");
}

export default function App() {
  const [session, setSession] = useState<GatewaySession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stage, setStage] = useState<WorkflowStage>("idle");
  const [intent, setIntent] = useState<"chat" | "task" | null>(null);
  const [swarmTasks, setSwarmTasks] = useState<SwarmTask[]>([]);
  const [busy, setBusy] = useState(false);
  const [sideTab, setSideTab] = useState<"workflow" | "telemetry" | "workspace" | "ssh" | "osiris">("workflow");
  const [theme, setTheme] = useState<ThemeId>(loadStoredTheme);
  const [activeProject, setActiveProject] = useState<string | null>(null);
  const [telemetry, setTelemetry] = useState<TelemetryEvent[]>([]);
  const [autosaveNotice, setAutosaveNotice] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  const handleLogout = useCallback(() => {
    abortRef.current?.abort();
    setSession(null);
    setMessages([]);
    setStage("idle");
    setIntent(null);
    setSwarmTasks([]);
    setBusy(false);
    setActiveProject(null);
    setTelemetry([]);
  }, []);

  const runSwarmAnimation = useCallback(async (goals: string[]) => {
    const tasks: SwarmTask[] = goals.map((goal, i) => ({
      id: `t${i}`,
      goal,
      status: "PENDING",
    }));
    setSwarmTasks(tasks);

    for (let i = 0; i < tasks.length; i++) {
      setSwarmTasks((prev) => prev.map((t, idx) => (idx === i ? { ...t, status: "RUNNING" } : t)));
      await sleep(420);
    }
    for (let i = 0; i < tasks.length; i++) {
      setSwarmTasks((prev) => prev.map((t, idx) => (idx === i ? { ...t, status: "COMPLETED" } : t)));
      await sleep(180);
    }
  }, []);

  /** After a turn finishes streaming: run all three telemetry layers, then
   * auto-save any path-tagged file blocks to the active workspace project. */
  const handleAssistantComplete = useCallback(
    async (
      messageId: string,
      content: string,
      opts: {
        classification: "chat" | "task";
        durationMs: number;
        errorOccurred: boolean;
      },
    ) => {
      const { classification, durationMs, errorOccurred } = opts;
      const allEvents: TelemetryEvent[] = [];

      // ── Layer 1: inline parse (best-effort, often empty at 0.5B) ──
      const inlineEvents = parseTelemetry(messageId, content);
      allEvents.push(...inlineEvents);

      // ── Layer 2: observable metrics (always works) ──
      const fileBlocks = parseFileBlocks(content).filter((f) => isSafeRelativePath(f.path));
      const filesDetected = fileBlocks.map((f) => f.path);
      const filesSaved: string[] = [];

      if (session && activeProject && fileBlocks.length > 0) {
        for (const block of fileBlocks) {
          try {
            const file = new File([block.content], block.path.split("/").pop() || "file.txt");
            await uploadFile(session.token, activeProject, file, block.path);
            filesSaved.push(block.path);
          } catch { /* best-effort */ }
        }
        if (filesSaved.length > 0) {
          setAutosaveNotice(`Saved to ${activeProject}: ${filesSaved.join(", ")}`);
          setTimeout(() => setAutosaveNotice(null), 5000);
        }
      }

      const observableEvent = buildObservableEvent(messageId, {
        classification,
        durationMs,
        tokenEstimate: Math.round(content.length / 4),
        filesDetected,
        filesSaved,
        errorOccurred,
      });
      allEvents.push(observableEvent);
      setTelemetry((prev) => [...prev, ...allEvents]);

      // Reflect any inline action goals onto the swarm lane display
      const actionGoals = inlineEvents
        .filter((e) => e.kind === "action" && e.action)
        .map((e) => `${e.action!.taskId}: ${e.action!.command || e.action!.reason}`);
      if (actionGoals.length > 0) {
        setSwarmTasks((prev) =>
          prev.length === actionGoals.length
            ? prev.map((t, i) => ({ ...t, goal: actionGoals[i] }))
            : prev,
        );
      }

      // ── Layer 3: post-hoc extraction (background, task turns only) ──
      // Only fires when inline parse found nothing, to avoid duplication.
      if (classification === "task" && inlineEvents.length === 0 && session && content.trim()) {
        extractPostHoc(session.token, MODEL, messageId, content)
          .then((extracted) => {
            if (extracted.length > 0) {
              setTelemetry((prev) => [...prev, ...extracted]);
              // Update swarm lanes with real extracted actions
              const extractedGoals = extracted
                .filter((e) => e.kind === "action" && e.action)
                .map((e) => `${e.action!.taskId}: ${e.action!.command || e.action!.reason}`);
              if (extractedGoals.length > 0) {
                setSwarmTasks((prev) =>
                  prev.map((t, i) =>
                    extractedGoals[i] ? { ...t, goal: extractedGoals[i] } : t,
                  ),
                );
              }
            }
          })
          .catch(() => { /* silent — post-hoc is best-effort */ });
      }
    },
    [session, activeProject],
  );

  const handleSend = useCallback(
    async (text: string) => {
      if (!session) return;

      const userMsg: ChatMessage = { id: nextId(), role: "user", content: text };
      const assistantId = nextId();
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        streaming: true,
      };

      const history = [...messages, userMsg];
      setMessages([...history, assistantMsg]);
      setBusy(true);
      setSwarmTasks([]);

      const detectedIntent = classifyIntent(text);
      setIntent(detectedIntent);
      setStage("classifying");

      const controller = new AbortController();
      abortRef.current = controller;

      const animation = (async () => {
        await sleep(380);
        if (detectedIntent === "chat") {
          setStage("chat");
          return;
        }
        setStage("planning");
        await sleep(420);
        setStage("swarm");
        await runSwarmAnimation(deriveSwarmGoals(text));
        setStage("consolidating");
      })();

      const systemContent =
        QWEN_CODER_SYSTEM_PROMPT + (detectedIntent === "task" ? TASK_MODE_TELEMETRY_ADDENDUM : "");
      // Build the API message list from the conversation history, but:
      // 1. Exclude console-injected "system" role messages (transmission
      //    error notices) — these are for the UI only, not for the model.
      // 2. Exclude any message whose content is empty or whitespace-only
      //    (an assistant message that errored mid-stream stays "" and would
      //    trigger a gateway 422 if sent as-is).
      const apiMessages = [
        { role: "system", content: systemContent },
        ...history
          .filter((m) => m.role !== "system" && m.content.trim().length > 0)
          .map((m) => ({ role: m.role, content: m.content })),
      ];

      let finalContent = "";
      let errorOccurred = false;
      const sendTime = Date.now();

      const network = streamChat(
        session.token,
        MODEL,
        apiMessages,
        {
          onToken: (chunk) => {
            finalContent += chunk;
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + chunk } : m)),
            );
          },
          onDone: () => {
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m)),
            );
          },
          onError: (message) => {
            errorOccurred = true;
            setMessages((prev) => [
              ...prev.filter((m) => m.id !== assistantId),
              {
                id: nextId(),
                role: "system",
                content: `TRANSMISSION ERROR: ${message}`,
              },
            ]);
            setStage("error");
          },
        },
        controller.signal,
      );

      await Promise.all([animation, network]);

      await handleAssistantComplete(assistantId, finalContent, {
        classification: detectedIntent,
        durationMs: Date.now() - sendTime,
        errorOccurred,
      });

      setStage((prev) => (prev === "error" ? prev : "done"));
      setBusy(false);
      await sleep(1400);
      setStage((prev) => (prev === "done" ? "idle" : prev));
    },
    [messages, session, runSwarmAnimation, handleAssistantComplete],
  );

  if (!session) {
    return (
      <div className="app-shell">
        <div className="crt-overlay" />
        <div className="crt-vignette" />
        <LoginScreen onAuthenticated={setSession} />
      </div>
    );
  }

  return (
    <div className="app-shell">
      <div className="crt-overlay" />
      <div className="crt-vignette" />
      <ConsoleHeader
        expiresAt={session.expiresAt}
        onLogout={handleLogout}
        theme={theme}
        onThemeChange={setTheme}
      />
      <div className="main-grid">
        <section className="panel">
          <ChatConsole messages={messages} />
          {autosaveNotice && <div className="autosave-notice">💾 {autosaveNotice}</div>}
          <div className="active-project-bar">
            <span>WORKSPACE:</span>
            <span className={activeProject ? "active-project-bar__name" : "active-project-bar__none"}>
              {activeProject ?? "none — file auto-save off"}
            </span>
            {activeProject && (
              <button className="active-project-bar__clear" onClick={() => setActiveProject(null)}>
                CLEAR
              </button>
            )}
          </div>
          <InputBar disabled={busy} onSubmit={handleSend} />
        </section>
        <aside className="panel panel--side">
          <div className="panel-tabs">
            <button className={`panel-tab ${sideTab === "workflow" ? "panel-tab--active" : ""}`} onClick={() => setSideTab("workflow")}>WORKFLOW</button>
            <button className={`panel-tab ${sideTab === "telemetry" ? "panel-tab--active" : ""}`} onClick={() => setSideTab("telemetry")}>TELEMETRY</button>
            <button className={`panel-tab ${sideTab === "workspace" ? "panel-tab--active" : ""}`} onClick={() => setSideTab("workspace")}>WORKSPACE</button>
            <button className={`panel-tab ${sideTab === "osiris" ? "panel-tab--active" : ""}`} onClick={() => setSideTab("osiris")}>OSIRIS</button>
            <button className={`panel-tab ${sideTab === "ssh" ? "panel-tab--active" : ""}`} onClick={() => setSideTab("ssh")}>SSH</button>
          </div>

          {sideTab === "workflow" && (
            <>
              <BrainWorkflow stage={stage} intent={intent} swarmTasks={swarmTasks} />
              <div className="workflow-meta">
                <div className="workflow-meta__row">
                  <span>MODEL</span>
                  <span>{MODEL}</span>
                </div>
                <div className="workflow-meta__row">
                  <span>RE_ACT_MAX_LOOP</span>
                  <span>3</span>
                </div>
                <div className="workflow-meta__row">
                  <span>LINK</span>
                  <span>GATEWAY :: /api/chat</span>
                </div>
              </div>
            </>
          )}

          {sideTab === "telemetry" && <TelemetryPanel events={telemetry} />}

          {sideTab === "workspace" && (
            <WorkspacePanel
              token={session.token}
              activeProject={activeProject}
              onActiveProjectChange={setActiveProject}
            />
          )}

          {sideTab === "ssh" && <SSHPanel token={session.token} />}
          {sideTab === "osiris" && <OsirisPanel token={session.token} />}
        </aside>
      </div>
    </div>
  );
}
