import type { FileInfo, ProjectInfo } from "../types";
import { GatewayError } from "./gatewayClient";

async function authedFetch(token: string, path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
  });
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = await res.clone().json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new GatewayError(detail ?? `Request failed (${res.status})`, res.status);
  }
  return res;
}

export async function listProjects(token: string): Promise<ProjectInfo[]> {
  const res = await authedFetch(token, "/api/projects");
  return (await res.json()) as ProjectInfo[];
}

export async function createProject(token: string, name: string): Promise<ProjectInfo> {
  const res = await authedFetch(token, "/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return (await res.json()) as ProjectInfo;
}

export async function deleteProject(token: string, name: string): Promise<void> {
  await authedFetch(token, `/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function listFiles(token: string, project: string): Promise<FileInfo[]> {
  const res = await authedFetch(token, `/api/projects/${encodeURIComponent(project)}/files`);
  return (await res.json()) as FileInfo[];
}

export async function uploadFile(
  token: string,
  project: string,
  file: File,
  destPath?: string,
): Promise<FileInfo> {
  const form = new FormData();
  form.append("file", file);
  if (destPath) form.append("path", destPath);

  const res = await authedFetch(token, `/api/projects/${encodeURIComponent(project)}/files`, {
    method: "POST",
    body: form,
  });
  return (await res.json()) as FileInfo;
}

export async function deleteFile(token: string, project: string, path: string): Promise<void> {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  await authedFetch(token, `/api/projects/${encodeURIComponent(project)}/files/${encodedPath}`, {
    method: "DELETE",
  });
}

/**
 * Downloads the project zip and triggers a browser save via a temporary
 * object URL. The blob is revoked immediately after the click to avoid
 * leaking memory across repeated downloads.
 */
export async function downloadProjectZip(token: string, project: string): Promise<void> {
  const res = await authedFetch(token, `/api/projects/${encodeURIComponent(project)}/download`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${project}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
