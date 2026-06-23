import type { SshJob, SshTarget } from "../types";
import { GatewayError } from "./gatewayClient";

async function authedFetch(token: string, path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(path, {
    ...init,
    headers: { ...(init.headers ?? {}), Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = await res.clone().json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch { /* not JSON */ }
    throw new GatewayError(detail ?? `Request failed (${res.status})`, res.status);
  }
  return res;
}

export async function listSshTargets(token: string): Promise<SshTarget[]> {
  const res = await authedFetch(token, "/api/ssh/targets");
  return (await res.json()) as SshTarget[];
}

export interface SshExecuteParams {
  target_ids: string[];
  procedure: string;
  timeout: number;
  label?: string;
}

export async function executeSshJob(token: string, params: SshExecuteParams): Promise<SshJob> {
  const res = await authedFetch(token, "/api/ssh/execute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return (await res.json()) as SshJob;
}

export async function listSshJobs(token: string, limit = 30): Promise<SshJob[]> {
  const res = await authedFetch(token, `/api/ssh/jobs?limit=${limit}`);
  return (await res.json()) as SshJob[];
}

export async function getSshJob(token: string, jobId: string): Promise<SshJob> {
  const res = await authedFetch(token, `/api/ssh/jobs/${encodeURIComponent(jobId)}`);
  return (await res.json()) as SshJob;
}
