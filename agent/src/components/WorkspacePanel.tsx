import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  createProject,
  deleteFile,
  deleteProject,
  downloadProjectZip,
  listFiles,
  listProjects,
  uploadFile,
} from "../api/workspaceClient";
import { GatewayError } from "../api/gatewayClient";
import type { FileInfo, ProjectInfo } from "../types";

interface Props {
  token: string;
  activeProject: string | null;
  onActiveProjectChange: (name: string | null) => void;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function WorkspacePanel({ token, activeProject, onActiveProjectChange }: Props) {
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [files, setFiles] = useState<FileInfo[]>([]);
  const [newProjectName, setNewProjectName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const refreshProjects = useCallback(async () => {
    try {
      const list = await listProjects(token);
      setProjects(list);
      return list;
    } catch (err) {
      setError(describeError(err));
      return [];
    }
  }, [token]);

  const refreshFiles = useCallback(
    async (project: string) => {
      try {
        const list = await listFiles(token, project);
        setFiles(list);
      } catch (err) {
        setError(describeError(err));
      }
    },
    [token],
  );

  useEffect(() => {
    void refreshProjects();
  }, [refreshProjects]);

  useEffect(() => {
    if (activeProject) void refreshFiles(activeProject);
    else setFiles([]);
  }, [activeProject, refreshFiles]);

  async function handleCreateProject(e: FormEvent) {
    e.preventDefault();
    const name = newProjectName.trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      await createProject(token, name);
      setNewProjectName("");
      await refreshProjects();
      onActiveProjectChange(name);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteProject(name: string) {
    setBusy(true);
    setError(null);
    try {
      await deleteProject(token, name);
      if (activeProject === name) onActiveProjectChange(null);
      await refreshProjects();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleUpload(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || !activeProject) return;
    setBusy(true);
    setError(null);
    try {
      await uploadFile(token, activeProject, file);
      await refreshFiles(activeProject);
      await refreshProjects();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleDeleteFile(path: string) {
    if (!activeProject) return;
    setBusy(true);
    setError(null);
    try {
      await deleteFile(token, activeProject, path);
      await refreshFiles(activeProject);
      await refreshProjects();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleDownload() {
    if (!activeProject) return;
    setError(null);
    try {
      await downloadProjectZip(token, activeProject);
    } catch (err) {
      setError(describeError(err));
    }
  }

  return (
    <div className="workspace">
      <form className="workspace__new-project" onSubmit={handleCreateProject}>
        <input
          className="field__input field__input--compact"
          placeholder="new-project-name"
          value={newProjectName}
          onChange={(e) => setNewProjectName(e.target.value)}
          disabled={busy}
        />
        <button className="workspace__btn" type="submit" disabled={busy || !newProjectName.trim()}>
          + PROJECT
        </button>
      </form>

      <div className="workspace__projects">
        {projects.length === 0 && <div className="workspace__empty">NO PROJECTS ON RECORD</div>}
        {projects.map((p) => (
          <div
            key={p.name}
            className={`workspace__project ${activeProject === p.name ? "workspace__project--active" : ""}`}
            onClick={() => onActiveProjectChange(p.name)}
          >
            <span className="workspace__project-name">{p.name}</span>
            <span className="workspace__project-meta">
              {p.file_count} FILES · {formatBytes(p.total_bytes)}
            </span>
            <button
              className="workspace__icon-btn"
              title="Delete project"
              onClick={(e) => {
                e.stopPropagation();
                handleDeleteProject(p.name);
              }}
              disabled={busy}
            >
              ✕
            </button>
          </div>
        ))}
      </div>

      {activeProject && (
        <div className="workspace__detail">
          <div className="workspace__detail-header">
            <span>{activeProject}</span>
            <div className="workspace__detail-actions">
              <label className="workspace__btn workspace__btn--upload">
                UPLOAD
                <input
                  ref={fileInputRef}
                  type="file"
                  onChange={handleUpload}
                  disabled={busy}
                  style={{ display: "none" }}
                />
              </label>
              <button className="workspace__btn" onClick={handleDownload} disabled={busy}>
                ⇩ ZIP
              </button>
            </div>
          </div>

          <div className="workspace__files">
            {files.length === 0 && <div className="workspace__empty">EMPTY WORKSPACE</div>}
            {files.map((f) => (
              <div key={f.path} className="workspace__file">
                <span className="workspace__file-path">{f.path}</span>
                <span className="workspace__file-size">{formatBytes(f.size_bytes)}</span>
                <button
                  className="workspace__icon-btn"
                  title="Delete file"
                  onClick={() => handleDeleteFile(f.path)}
                  disabled={busy}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {error && <div className="workspace__error">⚠ {error}</div>}
    </div>
  );
}

function describeError(err: unknown): string {
  if (err instanceof GatewayError) {
    if (err.status === 429) return "RATE LIMITED — slow down";
    return err.message.toUpperCase();
  }
  return err instanceof Error ? err.message : "UNKNOWN ERROR";
}
