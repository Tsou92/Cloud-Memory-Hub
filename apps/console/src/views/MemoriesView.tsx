import {
  ChevronLeft,
  ChevronRight,
  Database,
  Edit3,
  FileText,
  HelpCircle,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
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
  CopyBlock,
  EmptyState,
  formatFullTimestamp,
  layerMeta,
  Modal,
} from "../components/ui";
import { useHub } from "../context/HubContext";
import type { ConsoleView, SettingsTab } from "../router";
import { GovernanceModal } from "./GovernanceModal";

export function MemoriesView({
  project,
  onNavigate,
}: {
  project: HubProject;
  onNavigate(view: ConsoleView, tab?: SettingsTab): void;
}) {
  const { bootstrap } = useHub();
  const instance = bootstrap.instance_id;
  const scope = project.scope_id;

  const [items, setItems] = useState<HubItem[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [activeSearch, setActiveSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<HubItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  const [sourceId, setSourceId] = useState<string | null>(null);
  const [governanceId, setGovernanceId] = useState<string | null>(null);
  const [pendingSources, setPendingSources] = useState<string[]>([]);
  const [retainedSources, setRetainedSources] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [guideOpen, setGuideOpen] = useState(false);

  const listReq = useRef(0);
  const detailAbort = useRef<AbortController | null>(null);

  const loadList = useCallback(
    async (pageOffset: number, searchTerm: string) => {
      const ticket = ++listReq.current;
      setLoading(true);
      setError("");
      try {
        const page = searchTerm
          ? await readHub(instance, scope, "memory-search", {
              query: searchTerm,
              limit: 10,
            })
          : await readHub(instance, scope, "memory-list", {
              limit: 10,
              offset: pageOffset,
            });
        if (ticket !== listReq.current) return;
        setItems(page.items);
        setNextOffset(page.next_offset);
        if (page.items.length > 0) {
          setSelectedId((prev) =>
            prev && page.items.some((r) => r.record_id === prev)
              ? prev
              : page.items[0].record_id,
          );
        } else {
          setSelectedId(null);
          setDetail(null);
        }
      } catch (reason) {
        if (ticket === listReq.current) setError(hubErrorMessage(reason));
      } finally {
        if (ticket === listReq.current) setLoading(false);
      }
    },
    [instance, scope],
  );

  useEffect(() => {
    void loadList(offset, activeSearch);
  }, [offset, activeSearch, loadList]);

  const loadDetail = useCallback(
    async (recordId: string) => {
      detailAbort.current?.abort();
      const controller = new AbortController();
      detailAbort.current = controller;
      setDetailLoading(true);
      setDetailError("");
      setEditing(false);
      try {
        const page = await readHub(
          instance,
          scope,
          "memory-get",
          { record_id: recordId, content_offset: 0 },
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setDetail(page.items[0]);
          setDraft(page.items[0].content);
        }
      } catch (reason) {
        if (!controller.signal.aborted) setDetailError(hubErrorMessage(reason));
      } finally {
        if (!controller.signal.aborted) setDetailLoading(false);
      }
    },
    [instance, scope],
  );

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
    return () => detailAbort.current?.abort();
  }, [selectedId, loadDetail]);

  function handleSearchSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    setOffset(0);
    setActiveSearch(trimmed);
  }

  function clearSearch() {
    setQuery("");
    setOffset(0);
    setActiveSearch("");
  }

  async function loadMoreContent() {
    if (!detail || detail.next_content_offset === null || detailLoading) return;
    setDetailLoading(true);
    setDetailError("");
    try {
      const next = await readHub(instance, scope, "memory-get", {
        record_id: detail.record_id,
        content_offset: detail.next_content_offset,
      });
      const merged = {
        ...next.items[0],
        content: detail.content + next.items[0].content,
      };
      setDetail(merged);
      setDraft(merged.content);
    } catch (reason) {
      setDetailError(hubErrorMessage(reason));
    } finally {
      setDetailLoading(false);
    }
  }

  async function saveCorrection() {
    if (!detail || !draft.trim() || saving) return;
    setSaving(true);
    setDetailError("");
    try {
      const saved = await correctHubMemory(scope, detail.record_id, draft);
      const updated = { ...detail, content: saved.content };
      setDetail(updated);
      setDraft(saved.content);
      setEditing(false);
      setNotice("记忆内容已纠正保存。");
      setItems((rows) =>
        rows.map((r) =>
          r.record_id === saved.record_id ? { ...r, content: saved.content } : r,
        ),
      );
    } catch (reason) {
      setDetailError(hubErrorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="cmh-page">
      <header className="cmh-page-header">
        <div>
          <h1>记忆库</h1>
          <p>浏览由模型从原文中提炼出的结构化长期记忆，支持语义检索、原地纠正与遗忘治理。</p>
        </div>
        <div className="cmh-page-header__actions">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setGuideOpen(true)}
          >
            <HelpCircle size={15} />
            如何验证首次共享？
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={loading}
            onClick={() => void loadList(offset, activeSearch)}
          >
            <RefreshCw size={14} className={loading ? "cmh-spin" : ""} />
            刷新列表
          </Button>
        </div>
      </header>

      {notice ? (
        <div role="status" className="cmh-alert cmh-alert--emerald">
          <span>{notice}</span>
          <button
            type="button"
            className="cmh-icon-btn"
            aria-label="关闭提示"
            onClick={() => setNotice("")}
          >
            <X size={15} />
          </button>
        </div>
      ) : null}

      {retainedSources.length > 0 ? (
        <aside className="cmh-callout" aria-label="忘记后保留的来源">
          <div className="cmh-callout__body">
            <strong>相关记忆已停止自动召回，原始历史仍保留。</strong>
            <p>仅在点击下方按钮时按记录编号主动读取，原文不会重新进入自动召回。</p>
          </div>
          <div className="cmh-inline-actions">
            {retainedSources.map((id, idx) => (
              <Button
                key={id}
                variant="secondary"
                size="sm"
                onClick={() => setSourceId(id)}
              >
                查看保留来源{retainedSources.length > 1 ? ` ${idx + 1}` : ""}
              </Button>
            ))}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setRetainedSources([])}
            >
              收起
            </Button>
          </div>
        </aside>
      ) : null}

      {/* Search Bar */}
      <form className="cmh-search-bar" onSubmit={handleSearchSubmit}>
        <div className="cmh-search-input-wrap">
          <Search size={16} className="cmh-search-icon" />
          <input
            className="cmh-input cmh-search-input"
            aria-label="语义搜索当前项目记忆"
            placeholder="输入问题或关键词，在当前范围语义检索记忆…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {activeSearch || query ? (
            <button
              type="button"
              className="cmh-icon-btn cmh-search-clear"
              aria-label="清除搜索"
              onClick={clearSearch}
            >
              <X size={15} />
            </button>
          ) : null}
        </div>
        <Button type="submit" variant="primary" disabled={loading || !query.trim()}>
          搜索
        </Button>
        {activeSearch ? (
          <Badge tone="indigo">搜索中：{activeSearch}</Badge>
        ) : null}
      </form>

      {error ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {error}
        </p>
      ) : null}

      {!loading && !error && items.length === 0 ? (
        activeSearch ? (
          <EmptyState
            icon={<Search size={24} />}
            title="没有匹配的记忆条目"
            description="尝试更换关键词，或清除搜索条件查看当前项目的全部记忆。"
            actions={
              <Button variant="secondary" onClick={clearSearch}>
                清除搜索返回完整列表
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={<Database size={24} />}
            title="记忆库还是空的"
            description="这并不等于连接失败。这里显示的是已由模型提炼进长期记忆的条目；刚上传的内容会先进入「历史原文」。"
            actions={
              <>
                <Button variant="secondary" onClick={() => onNavigate("history")}>
                  查看历史原文
                </Button>
                <Button
                  variant="primary"
                  onClick={() => onNavigate("settings", "model")}
                >
                  配置记忆整理模型
                </Button>
              </>
            }
          >
            <ul className="cmh-empty__tips">
              <li>尚未配置并启用模型：原文与任务交接可正常使用，但不会自动提炼长期记忆。</li>
              <li>刚发送的内容：后台队列提炼需要数秒到数十秒，稍后点击刷新查看。</li>
            </ul>
          </EmptyState>
        )
      ) : (
        <div className="cmh-split-workspace">
          {/* Master List */}
          <div className="cmh-master-pane">
            <div className="cmh-master-pane__meta">
              <span>{loading ? "正在加载…" : `当前展示 ${items.length} 条`}</span>
              {!activeSearch ? (
                <span className="cmh-mono">偏移 #{offset}</span>
              ) : null}
            </div>

            <ul className="cmh-record-list" aria-label="记忆列表">
              {items.map((item) => {
                const meta = layerMeta(item.layer);
                const selected = item.record_id === selectedId;
                return (
                  <li key={item.record_id}>
                    <button
                      type="button"
                      className={`cmh-record-item${selected ? " is-selected" : ""}`}
                      onClick={() => setSelectedId(item.record_id)}
                    >
                      <div className="cmh-record-item__top">
                        <Badge tone={meta.tone}>{meta.label}</Badge>
                        {item.source_raw_ids.length > 0 ? (
                          <span className="cmh-record-item__sources">
                            {item.source_raw_ids.length} 条来源
                          </span>
                        ) : null}
                      </div>
                      <p className="cmh-record-item__snippet">{item.content}</p>
                    </button>
                  </li>
                );
              })}
            </ul>

            {!activeSearch && (offset > 0 || nextOffset !== null) ? (
              <nav className="cmh-pagination" aria-label="记忆列表分页">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={loading || offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - 10))}
                >
                  <ChevronLeft size={15} /> 上一页
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={loading || nextOffset === null}
                  onClick={() => {
                    if (nextOffset !== null) setOffset(nextOffset);
                  }}
                >
                  下一页 <ChevronRight size={15} />
                </Button>
              </nav>
            ) : null}
          </div>

          {/* Detail & Inline Correction Pane */}
          <section className="cmh-detail-pane" aria-label="记忆详情">
            {detailLoading && !detail ? (
              <p className="cmh-muted">正在读取记忆详情…</p>
            ) : !detail ? (
              <p className="cmh-muted">在左侧选择一条记忆查看完整正文、来源依据或执行修改。</p>
            ) : (
              <div className="cmh-stack">
                <header className="cmh-detail-pane__header">
                  <div className="cmh-detail-pane__badges">
                    <Badge tone={layerMeta(detail.layer).tone}>
                      {layerMeta(detail.layer).label}
                    </Badge>
                    <span className="cmh-mono cmh-muted">
                      {formatFullTimestamp(detail.memory_at)}
                    </span>
                  </div>
                  <div className="cmh-inline-actions">
                    {!editing ? (
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={detail.next_content_offset !== null}
                        title={
                          detail.next_content_offset !== null
                            ? "请先加载完整正文后再编辑"
                            : undefined
                        }
                        onClick={() => {
                          setDraft(detail.content);
                          setEditing(true);
                        }}
                      >
                        <Edit3 size={14} /> 纠正内容
                      </Button>
                    ) : null}
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => {
                        setPendingSources([
                          ...new Set(detail.source_raw_ids.filter(Boolean)),
                        ]);
                        setRetainedSources([]);
                        setNotice("");
                        setGovernanceId(detail.record_id);
                      }}
                    >
                      <Trash2 size={14} /> 忘记 / 删除
                    </Button>
                  </div>
                </header>

                {detailError ? (
                  <p role="alert" className="cmh-alert cmh-alert--danger">
                    {detailError}
                  </p>
                ) : null}

                {editing ? (
                  <div className="cmh-stack">
                    <div className="cmh-field">
                      <label htmlFor="cmh-memory-editor">编辑记忆正文</label>
                      <textarea
                        id="cmh-memory-editor"
                        className="cmh-textarea"
                        rows={8}
                        value={draft}
                        disabled={saving}
                        onChange={(event) => setDraft(event.target.value)}
                      />
                    </div>
                    <div className="cmh-inline-actions">
                      <Button
                        variant="primary"
                        loading={saving}
                        disabled={!draft.trim() || draft === detail.content}
                        onClick={() => void saveCorrection()}
                      >
                        保存修改
                      </Button>
                      <Button
                        variant="ghost"
                        disabled={saving}
                        onClick={() => {
                          setDraft(detail.content);
                          setEditing(false);
                        }}
                      >
                        取消
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="cmh-detail-body">
                    <p className="cmh-prose">{detail.content}</p>
                    {detail.next_content_offset !== null ? (
                      <Button
                        variant="secondary"
                        size="sm"
                        loading={detailLoading}
                        onClick={() => void loadMoreContent()}
                      >
                        加载后续完整正文
                      </Button>
                    ) : null}
                  </div>
                )}

                <div className="cmh-detail-meta">
                  <div>
                    <span>记忆 ID</span>
                    <code className="cmh-mono">{detail.record_id}</code>
                  </div>
                  {detail.agent_id ? (
                    <div>
                      <span>产出标识</span>
                      <code className="cmh-mono">{detail.agent_id}</code>
                    </div>
                  ) : null}
                </div>

                {/* Source Provenance Chips */}
                <div className="cmh-provenance-box">
                  <h4>来源依据追溯</h4>
                  {detail.source_raw_ids.filter(Boolean).length > 0 ? (
                    <div className="cmh-chip-list">
                      {detail.source_raw_ids.filter(Boolean).map((rawId, idx) => (
                        <button
                          key={rawId}
                          type="button"
                          className="cmh-source-chip"
                          onClick={() => setSourceId(rawId)}
                        >
                          <FileText size={13} />
                          <span>来源原文 #{idx + 1}</span>
                          <code className="cmh-mono">{rawId.slice(0, 14)}…</code>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <p className="cmh-muted">该记忆条目未附带可打开的原始记录引用。</p>
                  )}
                  {detail.references_truncated ? (
                    <small className="cmh-muted">
                      仅展示前段关联引用，完整影响范围可在「忘记 / 删除」预览中查看。
                    </small>
                  ) : null}
                </div>
              </div>
            )}
          </section>
        </div>
      )}

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
        onClose={() => {
          setGovernanceId(null);
          setPendingSources([]);
        }}
        onCompleted={(_result, completedAction) => {
          setNotice(
            completedAction === "purge"
              ? "彻底删除已完成，相关正文已在证明范围内清除。"
              : "忘记已完成。相关内容已停止自动使用，原始历史仍保留。",
          );
          setRetainedSources(
            completedAction === "forget" && governanceId ? pendingSources : [],
          );
          setPendingSources([]);
          setGovernanceId(null);
          void loadList(offset, activeSearch);
        }}
      />

      <Modal
        open={guideOpen}
        title="第一次共享怎么验证？"
        subtitle="按真实读写链路确认连接，不只看状态指示灯"
        onClose={() => setGuideOpen(false)}
      >
        <div className="cmh-stack cmh-prose-sm">
          <p>1. 在已连接当前项目的第一台设备 Agent 中发送下面这句话：</p>
          <CopyBlock
            label="测试指令"
            code="请用 Cloud Memory Hub 记住：这是我的第一次共享测试。"
          />
          <p>
            2. 打开本后台的「历史原文」，找到刚才这句话，即可确认 NAS 已持久接纳。
          </p>
          <p>
            3. 在连接同一项目的另一台设备上，让 Agent 读取刚才保存的测试内容；配置并启用整理模型后，提炼结果也会出现在「记忆库」中。
          </p>
        </div>
      </Modal>
    </div>
  );
}
