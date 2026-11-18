import { useEffect, useRef, useState } from "react";
import type { HubProject } from "../api/hub-api";
import {
  createProject,
  pairingMessage,
  rejectedBeforeWrite,
  validLabel,
} from "../api/hub-pairing";
import { Button, Modal } from "./ui";

export function CreateProjectModal({
  open,
  instance,
  currentProject,
  onClose,
  onCreated,
  onRefreshProjects,
}: {
  open: boolean;
  instance: string;
  currentProject: HubProject | null;
  onClose(): void;
  onCreated(project: HubProject): void;
  onRefreshProjects(): void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unknown, setUnknown] = useState(false);
  const [endConfirmed, setEndConfirmed] = useState(false);
  const [storageError, setStorageError] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const key = `cmh-project-create:${instance}`;

  useEffect(() => {
    if (!open) return;
    setError("");
    try {
      setUnknown(sessionStorage.getItem(key) !== null);
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
    return () => controller.current?.abort();
  }, [open, key]);

  async function handleSubmit() {
    if (busy || unknown || storageError) return;
    let label: string;
    try {
      label = validLabel(name);
    } catch (reason) {
      setError(pairingMessage(reason));
      return;
    }
    try {
      sessionStorage.setItem(key, "unconfirmed");
    } catch {
      setStorageError(true);
      return;
    }
    const current = new AbortController();
    controller.current = current;
    setBusy(true);
    setError("");
    setUnknown(true);
    try {
      const created = await createProject(label, current.signal);
      if (current.signal.aborted) return;
      sessionStorage.removeItem(key);
      setUnknown(false);
      setName("");
      onCreated(created);
      onClose();
    } catch (reason) {
      if (!current.signal.aborted) {
        if (rejectedBeforeWrite(reason)) {
          sessionStorage.removeItem(key);
          setUnknown(false);
        }
        setError(pairingMessage(reason));
      }
    } finally {
      if (!current.signal.aborted) setBusy(false);
    }
  }

  function clearUnknown() {
    try {
      sessionStorage.removeItem(key);
      setUnknown(false);
      setEndConfirmed(false);
      setError("");
    } catch {
      setStorageError(true);
    }
  }

  return (
    <Modal
      open={open}
      title="新建共享项目"
      subtitle="同一项目的不同设备与 Agent 共享记忆与会话接续；不同项目默认隔离。"
      busy={busy}
      onClose={onClose}
    >
      {storageError ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          当前浏览器无法使用会话存储记录创建状态，为避免重复建项目，暂不发起请求。请恢复站点存储权限后重试。
        </p>
      ) : unknown ? (
        <div className="cmh-stack">
          <p role="status" className="cmh-alert cmh-alert--amber">
            上一次项目创建请求结果尚未确认。请先刷新项目列表查看是否已创建成功，系统不会自动重复提交。
          </p>
          <div className="cmh-inline-actions">
            <Button onClick={onRefreshProjects} disabled={busy}>
              刷新项目列表核对
            </Button>
            {currentProject && currentProject.scope_id !== "global" ? (
              <Button
                variant="primary"
                disabled={busy}
                onClick={() => {
                  clearUnknown();
                  onClose();
                }}
              >
                使用当前已选项目（{currentProject.display_name}）
              </Button>
            ) : null}
          </div>
          <label className="cmh-checkbox">
            <input
              type="checkbox"
              checked={endConfirmed}
              disabled={busy}
              onChange={(event) => setEndConfirmed(event.target.checked)}
            />
            <span>我已刷新核对过项目列表，确认结束本次未知创建记录。</span>
          </label>
          <div className="cmh-inline-actions">
            <Button
              variant="secondary"
              disabled={busy || !endConfirmed}
              onClick={clearUnknown}
            >
              结束核对并重新填写
            </Button>
          </div>
        </div>
      ) : (
        <form
          className="cmh-stack"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
        >
          <div className="cmh-field">
            <label htmlFor="cmh-new-project-name">项目名称</label>
            <input
              id="cmh-new-project-name"
              className="cmh-input"
              value={name}
              placeholder="例如：Memory Hub 开发、个人知识库"
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
            />
            <small>建议使用清晰的工作主题名称（最多 80 个字符）。</small>
          </div>
          {error ? (
            <p role="alert" className="cmh-alert cmh-alert--danger">
              {error}
            </p>
          ) : null}
          <div className="cmh-modal__inline-footer">
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              取消
            </Button>
            <Button
              type="submit"
              variant="primary"
              loading={busy}
              disabled={!name.trim()}
            >
              创建并进入项目
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
