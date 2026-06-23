import type { OsirisBody, ProcedureOut, RunOut, ScheduleOut, StepLogOut } from "../types";
import { GatewayError } from "./gatewayClient";

async function authedFetch(token: string, path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(path, {
    ...init,
    headers: { ...(init.headers ?? {}), Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    let detail: string | null = null;
    try { const b = await res.clone().json(); if (typeof b?.detail === "string") detail = b.detail; } catch { /**/ }
    throw new GatewayError(detail ?? `Request failed (${res.status})`, res.status);
  }
  return res;
}

export async function listProcedures(token: string): Promise<ProcedureOut[]> {
  return (await authedFetch(token, "/api/procedures")).json();
}

export async function getProcedure(token: string, id: string): Promise<{ procedure_id: string; name: string; description?: string; version: number; body: OsirisBody }> {
  return (await authedFetch(token, `/api/procedures/${encodeURIComponent(id)}`)).json();
}

export async function createProcedure(token: string, payload: { name: string; description?: string; body: OsirisBody }): Promise<ProcedureOut> {
  return (await authedFetch(token, "/api/procedures", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })).json();
}

export async function updateProcedure(token: string, id: string, payload: { name?: string; description?: string; body?: OsirisBody }): Promise<ProcedureOut> {
  return (await authedFetch(token, `/api/procedures/${encodeURIComponent(id)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })).json();
}

export async function deleteProcedure(token: string, id: string): Promise<void> {
  await authedFetch(token, `/api/procedures/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function startRun(token: string, procedure_id: string, target_ids: string[], timeout_per_step = 120): Promise<RunOut> {
  return (await authedFetch(token, "/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ procedure_id, target_ids, timeout_per_step }) })).json();
}

export async function listRuns(token: string, limit = 50): Promise<RunOut[]> {
  return (await authedFetch(token, `/api/runs?limit=${limit}`)).json();
}

export async function getRun(token: string, run_id: string): Promise<RunOut> {
  return (await authedFetch(token, `/api/runs/${encodeURIComponent(run_id)}`)).json();
}

export async function getRunLogs(token: string, run_id: string, target_id?: string): Promise<StepLogOut[]> {
  const qs = target_id ? `?target_id=${encodeURIComponent(target_id)}` : "";
  return (await authedFetch(token, `/api/runs/${encodeURIComponent(run_id)}/logs${qs}`)).json();
}

export async function listSchedules(token: string): Promise<ScheduleOut[]> {
  return (await authedFetch(token, "/api/schedules")).json();
}

export async function createSchedule(token: string, payload: { procedure_id: string; target_ids: string[]; cron_expr: string; label?: string; enabled?: boolean }): Promise<ScheduleOut> {
  return (await authedFetch(token, "/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })).json();
}

export async function deleteSchedule(token: string, schedule_id: string): Promise<void> {
  await authedFetch(token, `/api/schedules/${encodeURIComponent(schedule_id)}`, { method: "DELETE" });
}
