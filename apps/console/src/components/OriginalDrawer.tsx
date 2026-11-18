import { useEffect, useState } from "react";
import {
  type HistoryDisplay,
  historyDisplay,
  summarizeHistoryItem,
} from "../api/history-display";
import {
  type HubItem,
  hubErrorMessage,
  readHub,
} from "../api/hub-api";
import { Badge, Button, CopyBlock, Drawer, formatFullTimestamp } from "./ui";

export function OriginalDrawer({
  instance,
  scope,
  recordId,
  initialItem = null,
  onClose,
}: {
  instance: string;
  scope: string;
  recordId: string | null;
  initialItem?: HubItem | null;
  onClose(): void;
}) {
  const [item, setItem] = useState<HubItem | null>(initialItem);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"readable" | "raw">("readable");

  useEffect(() => {
    if (!recordId) {
      setItem(null);
      setError("");
      return;
    }
    setMode("readable");
    if (initialItem && initialItem.record_id === recordId) {
      setItem(initialItem);
      setError("");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setItem(null);
    void readHub(
      instance,
      scope,
      "history-get",
      { record_id: recordId, content_offset: 0 },
      controller.signal,
    )
      .then(
        (page) => {
          if (!controller.signal.aborted) setItem(page.items[0]);
        },
        (reason) => {
          if (!controller.signal.aborted) setError(hubErrorMessage(reason));
        },
      )
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [instance, scope, recordId, initialItem]);

  async function loadMore() {
    if (!item || item.next_content_offset === null || loadingMore) return;
    setLoadingMore(true);
    setError("");
    try {
      const next = await readHub(instance, scope, "history-get", {
        record_id: item.record_id,
        content_offset: item.next_content_offset,
      });
      const chunk = next.items[0];
      setItem({
        ...chunk,
        content: item.content + chunk.content,
      });
    } catch (reason) {
      setError(hubErrorMessage(reason));
    } finally {
      setLoadingMore(false);
    }
  }

  const parsed = item
    ? historyDisplay({
        raw_id: item.record_id,
        content: item.content,
        truncated: item.next_content_offset !== null,
      })
    : null;
  const summary = item
    ? summarizeHistoryItem({
        raw_id: item.record_id,
        content: item.content,
        truncated: item.next_content_offset !== null,
      })
    : null;

  return (
    <Drawer
      open={Boolean(recordId)}
      title="原始记录详情"
      subtitle={recordId ? `记录编号：${recordId}` : undefined}
      onClose={onClose}
      actions={
        parsed ? (
          <div className="cmh-segmented" role="tablist" aria-label="视图模式">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "readable"}
              className={mode === "readable" ? "is-active" : ""}
              onClick={() => setMode("readable")}
            >
              结构化阅读
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "raw"}
              className={mode === "raw" ? "is-active" : ""}
              onClick={() => setMode("raw")}
            >
              原始信封
            </button>
          </div>
        ) : undefined
      }
    >
      {loading ? (
        <p role="status" className="cmh-muted">
          正在读取原始归档记录…
        </p>
      ) : error ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {error}
        </p>
      ) : item ? (
        <div className="cmh-stack">
          <div className="cmh-meta-grid">
            <div>
              <span>记录类型</span>
              <strong>
                <Badge tone={summary?.badgeTone ?? "neutral"}>
                  {summary?.kindLabel ?? "原始记录"}
                </Badge>
              </strong>
            </div>
            <div>
              <span>接纳时间</span>
              <strong className="cmh-mono">
                {formatFullTimestamp(item.memory_at)}
              </strong>
            </div>
            <div>
              <span>来源标识</span>
              <strong className="cmh-mono">{item.source || "未标记"}</strong>
            </div>
            <div>
              <span>会话 ID</span>
              <strong className="cmh-mono">{item.session_id || "未提供"}</strong>
            </div>
          </div>

          {item.next_content_offset !== null ? (
            <div className="cmh-alert cmh-alert--amber">
              <span>当前展示前段内容，完整结构化解析需加载完全部正文后生效。</span>
              <Button
                size="sm"
                variant="secondary"
                loading={loadingMore}
                onClick={() => void loadMore()}
              >
                继续读取后续正文
              </Button>
            </div>
          ) : null}

          {parsed && mode === "readable" ? (
            <ReadableOriginalView display={parsed} />
          ) : (
            <CopyBlock
              label="完整原始归档内容"
              code={item.content}
              copyLabel="复制原文"
            />
          )}
        </div>
      ) : null}
    </Drawer>
  );
}

export function ReadableOriginalView({ display }: { display: HistoryDisplay }) {
  if (display.kind === "remember") {
    return (
      <div className="cmh-panel cmh-panel--inset">
        <div className="cmh-panel__eyebrow">Agent 显式提交内容</div>
        <p className="cmh-prose">{display.text}</p>
      </div>
    );
  }
  if (display.kind === "handoff") {
    const doc = display.handoff;
    return (
      <div className="cmh-handoff-card">
        <div className="cmh-handoff-section">
          <h4>任务目标</h4>
          <p className="cmh-prose">{doc.goal}</p>
        </div>
        <div className="cmh-handoff-section">
          <h4>当前进展</h4>
          <p className="cmh-prose">{doc.state}</p>
        </div>
        <div className="cmh-handoff-cols">
          <HandoffGroup title="已定决策" items={doc.decisions} />
          <HandoffGroup title="下一步计划" items={doc.next_steps} />
          <HandoffGroup title="当前阻碍" items={doc.blockers} />
        </div>
      </div>
    );
  }
  return (
    <div className="cmh-transcript">
      {display.messages.map((msg, idx) => {
        const roleMeta = {
          user: { label: "用户", tone: "indigo" as const },
          assistant: { label: "助手", tone: "emerald" as const },
          tool: { label: "工具", tone: "neutral" as const },
        }[msg.role];
        return (
          <div
            key={`${idx}-${msg.role}`}
            className={`cmh-bubble cmh-bubble--${msg.role}`}
          >
            <div className="cmh-bubble__head">
              <Badge tone={roleMeta.tone}>{roleMeta.label}</Badge>
            </div>
            <p className="cmh-prose">{msg.text}</p>
          </div>
        );
      })}
    </div>
  );
}

function HandoffGroup({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="cmh-handoff-group">
      <h5>{title}</h5>
      {items.length > 0 ? (
        <ul>
          {items.map((entry, idx) => (
            <li key={`${idx}-${entry.slice(0, 12)}`}>{entry}</li>
          ))}
        </ul>
      ) : (
        <p className="cmh-muted">未记录</p>
      )}
    </div>
  );
}
