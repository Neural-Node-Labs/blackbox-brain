import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";

interface Props {
  messages: ChatMessage[];
}

const MARKERS: Record<ChatMessage["role"], string> = {
  user: ">> OPERATOR",
  assistant: ":: BRAIN",
  system: "!! SYSTEM",
};

export function ChatConsole({ messages }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="console-log">
        <div className="console-log__empty">
          NO ACTIVE TRANSMISSIONS.
          <br />
          Enter a directive below to engage the orchestrator.
        </div>
      </div>
    );
  }

  return (
    <div className="console-log">
      {messages.map((m) => (
        <div key={m.id} className={`log-entry log-entry--${m.role}`}>
          <div className="log-entry__meta">
            <span className="log-entry__marker">{MARKERS[m.role]}</span>
          </div>
          <div className="log-entry__body">
            {m.content}
            {m.streaming && <span className="cursor-blink" />}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
