import { ThemeSwitcher } from "./ThemeSwitcher";
import type { ThemeId } from "../types";

interface Props {
  expiresAt: string | null;
  onLogout: () => void;
  theme: ThemeId;
  onThemeChange: (theme: ThemeId) => void;
}

export function ConsoleHeader({ expiresAt, onLogout, theme, onThemeChange }: Props) {
  return (
    <header className="console-header">
      <div className="console-header__brand">
        <span className="console-header__title">BLACKBOX BRAIN</span>
        <span className="console-header__subtitle">SECURE TERMINAL // ORCHESTRATOR LINK</span>
      </div>
      <div className="console-header__status">
        <ThemeSwitcher theme={theme} onChange={onThemeChange} />
        <span className="clearance-badge">CLEARANCE: OPERATOR</span>
        <span>
          <span className="session-dot" />
          {expiresAt ? `SESSION ACTIVE — EXP ${formatTime(expiresAt)}` : "SESSION ACTIVE"}
        </span>
        <button className="logout-btn" onClick={onLogout}>
          TERMINATE SESSION
        </button>
      </div>
    </header>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}
