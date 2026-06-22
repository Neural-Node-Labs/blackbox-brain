import { FormEvent, useState } from "react";
import { login, GatewayError } from "../api/gatewayClient";
import type { GatewaySession } from "../types";

interface Props {
  onAuthenticated: (session: GatewaySession) => void;
}

export function LoginScreen({ onAuthenticated }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await login(username, password);
      onAuthenticated({ token: res.token, expiresAt: res.expires_at });
    } catch (err) {
      if (err instanceof GatewayError && err.status === 429) {
        setError("ACCESS THROTTLED — too many attempts. Stand by.");
      } else if (err instanceof GatewayError && err.status === 401) {
        setError("CREDENTIALS REJECTED");
      } else {
        setError(err instanceof Error ? err.message.toUpperCase() : "CONNECTION FAILED");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-screen">
      <form className="login-panel" onSubmit={handleSubmit}>
        <div className="login-panel__eyebrow">// RESTRICTED ACCESS</div>
        <h1 className="login-panel__title">BLACKBOX BRAIN</h1>

        <div className="field">
          <label className="field__label" htmlFor="username">
            OPERATOR ID
          </label>
          <input
            id="username"
            className="field__input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </div>

        <div className="field">
          <label className="field__label" htmlFor="password">
            PASSPHRASE
          </label>
          <input
            id="password"
            type="password"
            className="field__input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>

        <button className="submit-btn" type="submit" disabled={submitting}>
          {submitting ? "AUTHENTICATING…" : "ESTABLISH CONNECTION"}
        </button>

        {error && <div className="login-error">⚠ {error}</div>}

        <p className="login-panel__footnote">
          Session tokens expire automatically and are held in memory only for
          this terminal session. Closing this tab clears your credentials.
        </p>
      </form>
    </div>
  );
}
