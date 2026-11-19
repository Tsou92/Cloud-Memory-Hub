import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  FolderInput,
  MessageSquareText,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { summarizeHistoryItem } from "../api/history-display";
import {
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
  formatTimestamp,
  Modal,
} from "../components/ui";
import { useHub } from "../context/HubContext";

const IMPORT_COMMAND =
  "cloud-memory-connector capture-init --state-dir /path/to/private-state \\\n" +
  "  --connection-file /path/to/private/connection.json \\\n" +
  "  --source-file /path/to/private/source.json\n" +
  "cloud-memory-connector collect --state-dir /path/to/private-state \\\n" +
  "  --connection-file /path/to/private/connection.json \\\n" +
  "  --source-file /path/to/private/source.json";

export function HistoryView({ project }: { project: HubProject }) {
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
  const [selectedItem, setSelectedItem] = useState<HubItem | null>(null);
  const [importModalOpen, setImportModalOpen] = useState(false);

  const reqTicket = useRef(0);

  const load = useCallback(
    async (pageOffset: number, searchTerm: string) => {
      const ticket = ++reqTicket.current;
      setLoading(true);
      setError("");
      try {
        const page = searchTerm
          ? await readHub(instance, scope, "history-search", {
              query: searchTerm,
              limit: 10,
              offset: pageOffset,
            })
          : await readHub(instance, scope, "history-list", {
              limit: 10,
              offset: pageOffset,
            });
        if (ticket !== reqTicket.current) return;
        setItems(page.items);
        setNextOffset(page.next_offset);
      } catch (reason) {
        if (ticket === reqTicket.current) setError(hubErrorMessage(reason));
      } finally {
        if (ticket === reqTicket.current) setLoading(false);
      }
    },
    [instance, scope],
  );

  useEffect(() => {
    void load(offset, activeSearch);
  }, [offset, activeSearch, load]);

  function handleSearch(event: FormEvent) {
    event.preventDefault();
    setOffset(0);
    setActiveSearch(query.trim());
  }

  function clearSearch() {
    setQuery("");
    setOffset(0);
    setActiveSearch("");
  }

  return (
    <div className="cmh-page">
      <header className="cmh-page-header">
        <div>
          <h1>历史原文</h1>
          <p>
            按 Hub 接纳归档展示原始对话、任务交接单与显式记忆；已忘记或彻底删除的内容不会出现在列表与搜索中。
          </p>
        </div>
        <div className="cmh-page-header__actions">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setImportModalOpen(true)}
          >
            <FolderInput size={15} />
            导入本机已有会话
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={loading}
            onClick={() => void load(offset, activeSearch)}
          >
            <RefreshCw size={14} className={loading ? "cmh-spin" : ""} />
            刷新
          </Button>
        </div>
      </header>

      <form className="cmh-search-bar" onSubmit={handleSearch}>
        <div className="cmh-search-input-wrap">
          <Search size={16} className="cmh-search-icon" />
          <input
            className="cmh-input cmh-search-input"
            aria-label="搜索历史原文"
            placeholder="在当前项目已接纳的原始对话与交接单中搜索关键词…"
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
          检索原文
        </Button>
        {activeSearch ? (
          <Badge tone="indigo">关键词：{activeSearch}</Badge>
        ) : null}
      </form>

      {error ? (
        <div role="alert" className="cmh-alert cmh-alert--danger">
          <span>{error}</span>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void load(offset, activeSearch)}
          >
            重试
          </Button>
        </div>
      ) : null}

      {!loading && !error && items.length === 0 ? (
        <EmptyState
          icon={<MessageSquareText size={24} />}
          title={activeSearch ? "未检索到匹配的原始记录" : "当前范围还没有历史原文"}
          description={
            activeSearch
              ? "尝试精简搜索词，或清除搜索条件查看最新接纳的时间线记录。"
              : "当已连接的设备上传对话轮次、提交显式记忆或发起任务交接后，原始记录会立即归档在这里。"
          }
          actions={
            activeSearch ? (
              <Button variant="secondary" onClick={clearSearch}>
                返回全部原文
              </Button>
            ) : (
              <Button
                variant="secondary"
                onClick={() => setImportModalOpen(true)}
              >
                了解如何导入电脑旧会话
              </Button>
            )
          }
        />
      ) : (
        <section className="cmh-panel" aria-label="历史原文时间线">
          <div className="cmh-timeline-meta">
            <span>
              {loading
                ? "正在读取归档记录…"
                : `${activeSearch ? "搜索命中" : "当前页"} ${items.length} 条记录`}
            </span>
            <span className="cmh-mono">点击条目打开右侧结构化阅读与原始信封</span>
          </div>

          <ul className="cmh-timeline-list">
            {items.map((item) => {
              const summary = summarizeHistoryItem({
                raw_id: item.record_id,
                content: item.content,
                truncated: item.next_content_offset !== null,
              });
              return (
                <li key={item.record_id}>
                  <button
                    type="button"
                    className="cmh-timeline-card"
                    onClick={() => setSelectedItem(item)}
                  >
                    <div className="cmh-timeline-card__top">
                      <div className="cmh-timeline-card__badges">
                        <Badge tone={summary.badgeTone}>
                          {summary.kindLabel}
                        </Badge>
                        {item.next_content_offset !== null ? (
                          <Badge tone="amber">超长分段</Badge>
                        ) : null}
                      </div>
                      <span className="cmh-mono cmh-timeline-card__time">
                        {item.source ? `${item.source} · ` : ""}
                        {formatTimestamp(item.memory_at)}
                      </span>
                    </div>

                    <h3 className="cmh-timeline-card__headline">
                      {summary.headline}
                    </h3>
                    <p className="cmh-timeline-card__sub">{summary.subline}</p>

                    <div className="cmh-timeline-card__foot cmh-mono">
                      <span>ID: {item.record_id}</span>
                      {item.session_id ? (
                        <span>会话: {item.session_id}</span>
                      ) : null}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>

          {offset > 0 || nextOffset !== null ? (
            <nav className="cmh-pagination" aria-label="历史原文分页">
              <Button
                variant="secondary"
                size="sm"
                disabled={loading || offset === 0}
                onClick={() => setOffset(Math.max(0, offset - 10))}
              >
                <ChevronLeft size={15} /> 上一页
              </Button>
              <span className="cmh-mono cmh-muted">偏移 #{offset}</span>
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
        </section>
      )}

      <OriginalDrawer
        instance={instance}
        scope={scope}
        recordId={selectedItem?.record_id ?? null}
        initialItem={selectedItem}
        onClose={() => setSelectedItem(null)}
      />

      <Modal
        open={importModalOpen}
        width="lg"
        title="导入本机已有历史会话"
        subtitle="历史会话文件保存在每台电脑本地，NAS 服务端不会越权扫描电脑磁盘，需由已配对电脑上的连接器执行过滤上传"
        onClose={() => setImportModalOpen(false)}
        footer={
          <div className="cmh-inline-actions">
            <a
              className="cmh-btn cmh-btn--secondary"
              href="https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/source-capture.md"
              target="_blank"
              rel="noreferrer"
            >
              查看完整来源采集文档 <ArrowUpRight size={14} />
            </a>
            <Button variant="primary" onClick={() => setImportModalOpen(false)}>
              关闭
            </Button>
          </div>
        }
      >
        <div className="cmh-stack cmh-prose-sm">
          <ol className="cmh-numbered-steps">
            <li>
              在「设备与接入」为目标电脑登记一个{" "}
              <code className="cmh-mono">claude-code</code>、{" "}
              <code className="cmh-mono">codex</code> 或{" "}
              <code className="cmh-mono">deepseek-harness</code> 来源，记录来源 UUID 与当前项目 UUID（
              <code className="cmh-mono">{scope}</code>）。
            </li>
            <li>
              在电脑本地创建权限为 <code className="cmh-mono">0600</code> 的{" "}
              <code className="cmh-mono">source.json</code>，将{" "}
              <code className="cmh-mono">source_root</code> 指向会话目录，且{" "}
              <code className="cmh-mono">workspace</code> 与原生记录中的项目工作目录精确一致。
            </li>
            <li>
              在目标电脑运行以下命令分批扫描并过滤凭证入队，重复运行{" "}
              <code className="cmh-mono">collect</code> 直至输出{" "}
              <code className="cmh-mono">queued=0</code>：
            </li>
          </ol>
          <CopyBlock
            label="客户端历史采集命令"
            code={IMPORT_COMMAND}
            copyLabel="复制导入命令"
          />
          <p className="cmh-muted">
            注：导入的是过滤后的原文，不代表立即完成长期记忆提炼；已忘记或彻底删除的内容在重导入时同样会被自动拦截拒绝。
          </p>
        </div>
      </Modal>
    </div>
  );
}
