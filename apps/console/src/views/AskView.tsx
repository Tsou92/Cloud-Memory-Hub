import {
  BookOpen,
  Edit3,
  FileText,
  Globe2,
  Send,
  Sparkles,
  Square,
  Trash2,
} from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import {
  type AskCitation,
  askHubStream,
  type HubAskAnswer,
} from "../api/hub-ask";
import {
  correctHubMemory,
  type HubItem,
  type HubProject,
  hubErrorMessage,
  readHub,
} from "../api/hub-api";
import { OriginalDrawer } from "../components/OriginalDrawer";
import {
  Badge,
  Button,
  Drawer,
  EmptyState,
  layerMeta,
} from "../components/ui";
import { useHub } from "../context/HubContext";
import { GovernanceModal } from "./GovernanceModal";

const SUGGESTIONS = [
  "总结这个项目目前已经确定的技术方案和架构约束",
  "最近几次任务交接里还留有哪些待办或未解决的问题？",
  "我们在测试和部署流程上有哪些明确约定？",
];

export function AskView({ project }: { project: HubProject }) {
  const { bootstrap, select } = useHub();
  const instance = bootstrap.instance_id;
  const scope = project.scope_id;
  const enabled = bootstrap.features.includes("scoped_console_ask");
  const isGlobal = scope === "global";

  const [question, setQuestion] = useState("");
  const [submittedQuestion, setSubmittedQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<"idle" | "recalling" | "streaming">("idle");
  const [partialText, setPartialText] = useState("");
  const [recalled, setRecalled] = useState<AskCitation[]>([]);
  const [answer, setAnswer] = useState<HubAskAnswer | null>(null);
  const [error, setError] = useState("");

  // Citation detail drawer state (replaces bottom-stacked Russian-doll sections)
  const [citationId, setCitationId] = useState<string | null>(null);
  const [citationDetail, setCitationDetail] = useState<HubItem | null>(null);
  const [citationLoading, setCitationLoading] = useState(false);
  const [citationError, setCitationError] = useState("");
  const [editingCitation, setEditingCitation] = useState(false);
  const [citationDraft, setCitationDraft] = useState("");
  const [savingCitation, setSavingCitation] = useState(false);

  const [sourceId, setSourceId] = useState<string | null>(null);
  const [governanceId, setGovernanceId] = useState<string | null>(null);

  const controllerRef = useRef<AbortController | null>(null);
  useEffect(() => () => controllerRef.current?.abort(), []);

  async function runQuestion(promptText: string) {
    const trimmed = promptText.trim();
    if (!enabled || busy || !trimmed) return;
    const next = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = next;

    setSubmittedQuestion(trimmed);
    setAnswer(null);
    setPartialText("");
    setRecalled([]);
    setCitationId(null);
    setError("");
    setBusy(true);
    setStage("recalling");

    try {
      const finalResult = await askHubStream(scope, trimmed, next.signal, {
        onRecall(memories) {
          setRecalled(memories);
          setStage("streaming");
        },
        onDelta(text) {
          setStage("streaming");
          setPartialText(text);
        },
      });
      if (!next.signal.aborted) {
        setAnswer(finalResult);
      }
    } catch (reason) {
      if (!next.signal.aborted) {
        setError(hubErrorMessage(reason));
      }
    } finally {
      if (!next.signal.aborted) {
        setBusy(false);
        setStage("idle");
      }
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void runQuestion(question);
  }

  useEffect(() => {
    if (!citationId) {
      setCitationDetail(null);
      setEditingCitation(false);
      return;
    }
    const ctrl = new AbortController();
    setCitationLoading(true);
    setCitationError("");
    setEditingCitation(false);
    void readHub(
      instance,
      scope,
      "memory-get",
      { record_id: citationId, content_offset: 0 },
      ctrl.signal,
    )
      .then(
        (page) => {
          if (!ctrl.signal.aborted) {
            setCitationDetail(page.items[0]);
            setCitationDraft(page.items[0].content);
          }
        },
        (reason) => {
          if (!ctrl.signal.aborted) setCitationError(hubErrorMessage(reason));
        },
      )
      .finally(() => {
        if (!ctrl.signal.aborted) setCitationLoading(false);
      });
    return () => ctrl.abort();
  }, [citationId, instance, scope]);

  async function saveCitationCorrection() {
    if (!citationDetail || !citationDraft.trim() || savingCitation) return;
    setSavingCitation(true);
    setCitationError("");
    try {
      const saved = await correctHubMemory(
        scope,
        citationDetail.record_id,
        citationDraft,
      );
      setCitationDetail({ ...citationDetail, content: saved.content });
      setCitationDraft(saved.content);
      setEditingCitation(false);
      setAnswer((prev) =>
        prev
          ? {
              ...prev,
              memories: prev.memories.map((m) =>
                m.memory_id === saved.record_id
                  ? { ...m, content: saved.content }
                  : m,
              ),
            }
          : prev,
      );
    } catch (reason) {
      setCitationError(hubErrorMessage(reason));
    } finally {
      setSavingCitation(false);
    }
  }

  const modeBadge = answer
    ? {
        generated: { label: "模型综合回答", tone: "indigo" as const },
        extractive: { label: "记忆摘录（未启用生成模型）", tone: "blue" as const },
        empty: { label: "未命中相关记忆", tone: "neutral" as const },
      }[answer.mode]
    : null;

  const citations = answer ? answer.memories : recalled;

  return (
    <div className="cmh-page">
      <header className="cmh-page-header">
        <div>
          <div className="cmh-page-header__title-row">
            <h1>问记忆</h1>
            <Badge tone={isGlobal ? "blue" : "indigo"}>
              范围：{project.display_name}
            </Badge>
          </div>
          <p>
            仅检索当前选定范围的记忆并附带可核查引用。项目之间严格隔离；若需查询跨项目通用习惯，可切换至「全局偏好」。
          </p>
        </div>
        {!isGlobal ? (
          <div className="cmh-page-header__actions">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => select("global")}
            >
              <Globe2 size={14} />
              切换到全局偏好提问
            </Button>
          </div>
        ) : null}
      </header>

      {!enabled ? (
        <EmptyState
          title="当前服务尚未开放网页问答"
          description="请升级 Hub 镜像后刷新页面；在此之前仍可在「记忆库」与「历史原文」中检索浏览。"
        />
      ) : (
        <div className="cmh-ask-workbench">
          <form className="cmh-ask-composer" onSubmit={handleSubmit}>
            <label htmlFor="cmh-ask-input" className="cmh-sr-only">
              你的问题
            </label>
            <textarea
              id="cmh-ask-input"
              aria-label="你的问题"
              className="cmh-ask-composer__textarea"
              rows={3}
              maxLength={4096}
              placeholder={`向「${project.display_name}」提问，按 Cmd/Ctrl + Enter 发送…`}
              value={question}
              disabled={busy}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
                  void runQuestion(question);
                }
              }}
            />
            <div className="cmh-ask-composer__bar">
              <span className="cmh-muted">
                已启用模型时生成归纳回答；未启用模型时直接返回匹配的记忆摘录。
              </span>
              <div className="cmh-inline-actions">
                {busy ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      controllerRef.current?.abort();
                      setBusy(false);
                      setStage("idle");
                    }}
                  >
                    <Square size={13} /> 停止
                  </Button>
                ) : null}
                <Button
                  type="submit"
                  variant="primary"
                  loading={busy}
                  disabled={!question.trim()}
                >
                  <Send size={14} /> 提问
                </Button>
              </div>
            </div>
          </form>

          {!busy && !answer && !error ? (
            <section className="cmh-ask-suggestions" aria-label="快捷提问示例">
              <span className="cmh-ask-suggestions__label">试试这样问：</span>
              <div className="cmh-ask-suggestions__list">
                {SUGGESTIONS.map((item) => (
                  <button
                    key={item}
                    type="button"
                    className="cmh-suggestion-pill"
                    onClick={() => {
                      setQuestion(item);
                      void runQuestion(item);
                    }}
                  >
                    <Sparkles size={13} />
                    <span>{item}</span>
                  </button>
                ))}
              </div>
            </section>
          ) : null}

          {error ? (
            <p role="alert" className="cmh-alert cmh-alert--danger">
              {error}
            </p>
          ) : null}

          {busy || answer ? (
            <section className="cmh-panel cmh-answer-card" aria-label="问答结果">
              <header className="cmh-answer-card__head">
                <div>
                  <span className="cmh-panel__eyebrow">提问内容</span>
                  <h2>{submittedQuestion}</h2>
                </div>
                {busy ? (
                  <Badge tone="indigo" dot>
                    {stage === "recalling" ? "正在召回相关记忆…" : "正在生成回答…"}
                  </Badge>
                ) : modeBadge ? (
                  <Badge tone={modeBadge.tone}>{modeBadge.label}</Badge>
                ) : null}
              </header>

              <div className="cmh-answer-card__body">
                {answer ? (
                  <p className="cmh-prose">{answer.answer}</p>
                ) : partialText ? (
                  <p className="cmh-prose">{partialText}</p>
                ) : (
                  <p role="status" className="cmh-muted">
                    正在当前范围检索匹配记忆…
                  </p>
                )}
              </div>

              <footer className="cmh-answer-card__citations">
                <div className="cmh-answer-card__cite-title">
                  <BookOpen size={15} />
                  <span>
                    引用记忆依据（{citations.length} 条）— 点击卡片在右侧抽屉查看详情或追溯原文
                  </span>
                </div>
                {citations.length > 0 ? (
                  <ul className="cmh-citation-grid">
                    {citations.map((cite, idx) => (
                      <li key={cite.memory_id}>
                        <button
                          type="button"
                          className={`cmh-citation-card${citationId === cite.memory_id ? " is-active" : ""}`}
                          onClick={() => setCitationId(cite.memory_id)}
                        >
                          <span className="cmh-citation-card__index cmh-mono">
                            [{idx + 1}]
                          </span>
                          <p>{cite.content}</p>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : !busy ? (
                  <p className="cmh-muted">本次回答没有命中可引用的长期记忆条目。</p>
                ) : null}
              </footer>
            </section>
          ) : null}
        </div>
      )}

      {/* Citation Detail Drawer */}
      <Drawer
        open={Boolean(citationId)}
        title="引用记忆详情"
        subtitle={citationId ? `记忆编号：${citationId}` : undefined}
        onClose={() => setCitationId(null)}
        actions={
          citationDetail && !editingCitation ? (
            <div className="cmh-inline-actions">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setCitationDraft(citationDetail.content);
                  setEditingCitation(true);
                }}
              >
                <Edit3 size={13} /> 纠正
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => setGovernanceId(citationDetail.record_id)}
              >
                <Trash2 size={13} /> 忘记 / 删除
              </Button>
            </div>
          ) : undefined
        }
      >
        {citationLoading ? (
          <p className="cmh-muted">正在读取引用记忆…</p>
        ) : citationError ? (
          <p role="alert" className="cmh-alert cmh-alert--danger">
            {citationError}
          </p>
        ) : citationDetail ? (
          <div className="cmh-stack">
            <div>
              <Badge tone={layerMeta(citationDetail.layer).tone}>
                {layerMeta(citationDetail.layer).label}
              </Badge>
            </div>

            {editingCitation ? (
              <div className="cmh-stack">
                <textarea
                  className="cmh-textarea"
                  rows={7}
                  value={citationDraft}
                  disabled={savingCitation}
                  onChange={(event) => setCitationDraft(event.target.value)}
                />
                <div className="cmh-inline-actions">
                  <Button
                    variant="primary"
                    loading={savingCitation}
                    disabled={
                      !citationDraft.trim() ||
                      citationDraft === citationDetail.content
                    }
                    onClick={() => void saveCitationCorrection()}
                  >
                    保存修改
                  </Button>
                  <Button
                    variant="ghost"
                    disabled={savingCitation}
                    onClick={() => setEditingCitation(false)}
                  >
                    取消
                  </Button>
                </div>
              </div>
            ) : (
              <p className="cmh-prose">{citationDetail.content}</p>
            )}

            <div className="cmh-provenance-box">
              <h4>追溯底层来源原文</h4>
              {citationDetail.source_raw_ids.filter(Boolean).length > 0 ? (
                <div className="cmh-chip-list">
                  {citationDetail.source_raw_ids.filter(Boolean).map((rawId, i) => (
                    <button
                      key={rawId}
                      type="button"
                      className="cmh-source-chip"
                      onClick={() => setSourceId(rawId)}
                    >
                      <FileText size={13} />
                      <span>打开来源原文 #{i + 1}</span>
                      <code className="cmh-mono">{rawId.slice(0, 14)}…</code>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="cmh-muted">该记忆无关联的原始记录 ID。</p>
              )}
            </div>
          </div>
        ) : null}
      </Drawer>

      <OriginalDrawer
        instance={instance}
        scope={scope}
        recordId={sourceId}
        onClose={() => setSourceId(null)}
      />

      <GovernanceModal
        instance={instance}
        scope={scope}
        recordId={governanceId}
        onClose={() => setGovernanceId(null)}
        onCompleted={() => {
          setGovernanceId(null);
          setCitationId(null);
        }}
      />
    </div>
  );
}
