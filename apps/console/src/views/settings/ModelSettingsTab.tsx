import {
  CheckCircle2,
  Cpu,
  Edit3,
  ListFilter,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  XCircle,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  activateModelProfile,
  createModelProfile,
  deleteModelProfile,
  getModelCatalog,
  getModelProfiles,
  getSedimentation,
  ModelApiError,
  type ModelProbeResult,
  type ModelProfile,
  modelErrorMessage,
  type ProviderKind,
  probeModelProfile,
  readPendingProbe,
  setSedimentation,
  updateModelProfile,
} from "../../api/model-profiles";
import { Badge, Button, EmptyState, Modal, Switch } from "../../components/ui";

const PRESETS: {
  label: string;
  provider: ProviderKind;
  baseUrl: string;
  placeholderModel: string;
}[] = [
  {
    label: "DeepSeek",
    provider: "openai_compatible",
    baseUrl: "https://api.deepseek.com/v1",
    placeholderModel: "deepseek-chat",
  },
  {
    label: "Anthropic Claude",
    provider: "anthropic",
    baseUrl: "https://api.anthropic.com",
    placeholderModel: "claude-sonnet-4-20250514",
  },
  {
    label: "OpenAI",
    provider: "openai_compatible",
    baseUrl: "https://api.openai.com/v1",
    placeholderModel: "gpt-4.1-mini",
  },
  {
    label: "Gemini (OpenAI 兼容)",
    provider: "openai_compatible",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    placeholderModel: "gemini-2.5-flash",
  },
  {
    label: "本地 Ollama",
    provider: "ollama",
    baseUrl: "http://host.docker.internal:11434",
    placeholderModel: "qwen2.5:14b",
  },
];

const PROVIDER_LABELS: Record<ProviderKind, string> = {
  openai_compatible: "OpenAI 兼容接口",
  anthropic: "Anthropic Messages",
  ollama: "Ollama 本地接口",
  hy_default: "内置默认接口",
  local_onnx: "本地 ONNX",
};

const STAGE_LABELS: Record<string, string> = {
  endpoint: "服务端点连接",
  provider: "协议兼容握手",
  authentication: "API 密钥鉴权",
  model: "模型生成响应",
  structured_output: "JSON 结构化输出",
  embedding: "检索向量就绪",
};

export function ModelSettingsTab() {
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [sedimentation, setSedimentationState] = useState<boolean | null>(null);
  const [sedimentationBusy, setSedimentationBusy] = useState(false);

  const [probingId, setProbingId] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<ModelProbeResult | null>(null);
  const [pendingProbeProfileId, setPendingProbeProfileId] = useState<
    string | null
  >(() => readPendingProbe()?.profileId ?? null);

  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [deletingTarget, setDeletingTarget] = useState<ModelProfile | null>(
    null,
  );
  const [deleting, setDeleting] = useState(false);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editingProfile, setEditingProfile] = useState<ModelProfile | null>(
    null,
  );

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [rows, sed] = await Promise.all([
        getModelProfiles(),
        getSedimentation().catch(() => null),
      ]);
      setProfiles(rows);
      if (sed) setSedimentationState(sed.enabled);
      setPendingProbeProfileId(readPendingProbe()?.profileId ?? null);
    } catch (reason) {
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : "无法读取模型配置，请检查服务连接。",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  async function toggleSedimentation(next: boolean) {
    if (sedimentationBusy) return;
    setSedimentationBusy(true);
    setError("");
    try {
      const saved = await setSedimentation(next);
      setSedimentationState(saved.enabled);
      setNotice(
        saved.enabled
          ? "已开启自动记忆整理：新接纳的原文将由当前启用模型异步提炼。"
          : "已暂停自动记忆整理：原文与任务交接仍正常保留，不再自动调用整理模型。",
      );
    } catch (reason) {
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : "自动整理状态未能确认，请重试。",
      );
    } finally {
      setSedimentationBusy(false);
    }
  }

  async function handleProbe(profile: ModelProfile) {
    if (probingId) return;
    setProbingId(profile.profile_id);
    setError("");
    setNotice("");
    setProbeResult(null);
    try {
      const result = await probeModelProfile(
        profile.profile_id,
        profile.revision,
      );
      setProbeResult(result);
      setPendingProbeProfileId(readPendingProbe()?.profileId ?? null);
      const allOk = result.stages.every((s) => s.ok);
      if (allOk) {
        setNotice(`模型「${profile.name}」连接测试全部通过！`);
      } else {
        setError(`模型「${profile.name}」部分能力检查未通过，请查看下方分阶段诊断。`);
      }
      await loadAll();
    } catch (reason) {
      setPendingProbeProfileId(readPendingProbe()?.profileId ?? null);
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : modelErrorMessage("probe_failed"),
      );
    } finally {
      setProbingId(null);
    }
  }

  async function handleActivate(profile: ModelProfile) {
    if (activatingId) return;
    setActivatingId(profile.profile_id);
    setError("");
    setNotice("");
    try {
      await activateModelProfile(profile.profile_id);
      setNotice(`已将「${profile.name}」设为当前启用模型。`);
      await loadAll();
    } catch (reason) {
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : "启用模型未完成，请先执行连接测试或检查密钥。",
      );
    } finally {
      setActivatingId(null);
    }
  }

  async function confirmDelete() {
    if (!deletingTarget || deleting) return;
    setDeleting(true);
    setError("");
    try {
      await deleteModelProfile(deletingTarget.profile_id);
      setNotice(`已删除模型配置「${deletingTarget.name}」。`);
      setDeletingTarget(null);
      await loadAll();
    } catch (reason) {
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : "删除模型未完成，请重试。",
      );
      setDeletingTarget(null);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="cmh-stack">
      {/* Sedimentation Toggle Card */}
      <section className="cmh-panel cmh-setting-banner" aria-label="自动记忆整理开关">
        <div>
          <h3>自动提炼长期记忆</h3>
          <p>
            开启后，Hub 会使用下方「已启用」的模型将接纳的对话原文整理为结构化长期记忆。关闭后仍可正常同步会话摘要与任务交接，不消耗外部模型额度。
          </p>
        </div>
        <div className="cmh-inline-actions">
          <Badge tone={sedimentation ? "emerald" : "neutral"}>
            {sedimentation === null
              ? "读取中"
              : sedimentation
                ? "自动整理已开启"
                : "已暂停"}
          </Badge>
          <Switch
            label="自动提炼长期记忆"
            checked={Boolean(sedimentation)}
            disabled={sedimentation === null || sedimentationBusy}
            onChange={(next) => void toggleSedimentation(next)}
          />
        </div>
      </section>

      {notice ? (
        <p role="status" className="cmh-alert cmh-alert--emerald">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {error}
        </p>
      ) : null}

      {/* Probe Result Diagnostics Card */}
      {probeResult ? (
        <section className="cmh-panel cmh-panel--inset" aria-label="模型连接测试结果">
          <div className="cmh-panel__header">
            <h3>最近一次连接测试诊断</h3>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setProbeResult(null)}
            >
              收起
            </Button>
          </div>
          <ul className="cmh-probe-stages">
            {probeResult.stages.map((st) => (
              <li key={st.stage} className="cmh-probe-stage">
                {st.ok ? (
                  <CheckCircle2 size={16} className="cmh-text-emerald" />
                ) : (
                  <XCircle size={16} className="cmh-text-danger" />
                )}
                <strong>{STAGE_LABELS[st.stage] ?? st.stage}</strong>
                <code className="cmh-mono">{st.code}</code>
                <span className="cmh-mono cmh-muted">{st.latency_ms} ms</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* Model Profiles List */}
      <section className="cmh-panel" aria-label="模型配置列表">
        <div className="cmh-panel__header">
          <div>
            <h2>模型服务配置 ({profiles.length})</h2>
            <p>API 密钥加密存储在 NAS 私有配置卷中，保存后永不向网页回显明文</p>
          </div>
          <div className="cmh-inline-actions">
            <Button
              variant="secondary"
              size="sm"
              disabled={loading}
              onClick={() => void loadAll()}
            >
              <RefreshCw size={14} className={loading ? "cmh-spin" : ""} /> 刷新
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setEditingProfile(null);
                setEditorOpen(true);
              }}
            >
              <Plus size={14} /> 添加模型配置
            </Button>
          </div>
        </div>

        {loading && profiles.length === 0 ? (
          <p className="cmh-muted">正在加载模型配置…</p>
        ) : profiles.length === 0 ? (
          <EmptyState
            icon={<Cpu size={24} />}
            title="尚未添加记忆整理模型"
            description="支持 DeepSeek、Anthropic Claude、OpenAI、Gemini 等兼容接口；默认检索向量由容器内置本地模型提供，无需额外配置 Embedding Key。"
            actions={
              <Button
                variant="primary"
                onClick={() => {
                  setEditingProfile(null);
                  setEditorOpen(true);
                }}
              >
                <Plus size={15} /> 添加第一个模型配置
              </Button>
            }
          />
        ) : (
          <div className="cmh-model-list">
            {profiles.map((prof) => {
              const hasPendingProbe = pendingProbeProfileId === prof.profile_id;
              return (
                <article
                  key={prof.profile_id}
                  className={`cmh-model-card${prof.is_active ? " is-active" : ""}`}
                >
                  <div className="cmh-model-card__main">
                    <div className="cmh-model-card__title-row">
                      <h3>{prof.name}</h3>
                      {prof.is_active ? (
                        <Badge tone="emerald" dot>
                          当前启用
                        </Badge>
                      ) : (
                        <Badge tone="neutral">备用</Badge>
                      )}
                      <Badge tone="indigo">
                        {PROVIDER_LABELS[prof.provider_kind] ?? prof.provider_kind}
                      </Badge>
                      <Badge tone={prof.has_api_key ? "blue" : "amber"}>
                        {prof.has_api_key ? "密钥已保存" : "无 API 密钥"}
                      </Badge>
                    </div>
                    <div className="cmh-model-card__meta cmh-mono">
                      <span>模型：{prof.model}</span>
                      <span>端点：{prof.base_url || "默认端点"}</span>
                      <span>r{prof.revision}</span>
                    </div>
                  </div>

                  <div className="cmh-inline-actions">
                    <Button
                      variant="secondary"
                      size="sm"
                      loading={probingId === prof.profile_id}
                      onClick={() => void handleProbe(prof)}
                    >
                      <Play size={13} />
                      {hasPendingProbe ? "核对测试结果" : "测试连接"}
                    </Button>
                    {!prof.is_active ? (
                      <Button
                        variant="primary"
                        size="sm"
                        loading={activatingId === prof.profile_id}
                        onClick={() => void handleActivate(prof)}
                      >
                        设为启用
                      </Button>
                    ) : null}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setEditingProfile(prof);
                        setEditorOpen(true);
                      }}
                    >
                      <Edit3 size={14} /> 编辑
                    </Button>
                    {!prof.is_active ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setDeletingTarget(prof)}
                      >
                        <Trash2 size={14} /> 删除
                      </Button>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <ModelProfileEditorModal
        open={editorOpen}
        existing={editingProfile}
        onClose={() => setEditorOpen(false)}
        onSaved={async (saved) => {
          setEditorOpen(false);
          setNotice(`模型配置「${saved.name}」已保存，建议点击「测试连接」验证可用性。`);
          await loadAll();
        }}
      />

      <Modal
        open={Boolean(deletingTarget)}
        title="确认删除模型配置？"
        subtitle="删除操作会一并移除该配置关联的加密 API 密钥"
        busy={deleting}
        onClose={() => setDeletingTarget(null)}
        footer={
          <div className="cmh-inline-actions">
            <Button
              variant="ghost"
              disabled={deleting}
              onClick={() => setDeletingTarget(null)}
            >
              取消
            </Button>
            <Button
              variant="danger"
              loading={deleting}
              onClick={() => void confirmDelete()}
            >
              确认删除
            </Button>
          </div>
        }
      >
        <p className="cmh-prose-sm">
          确定要删除模型配置「{deletingTarget?.name}」（{deletingTarget?.model}）吗？
        </p>
      </Modal>
    </div>
  );
}

function ModelProfileEditorModal({
  open,
  existing,
  onClose,
  onSaved,
}: {
  open: boolean;
  existing: ModelProfile | null;
  onClose(): void;
  onSaved(profile: ModelProfile): Promise<void>;
}) {
  const [name, setName] = useState("");
  const [providerKind, setProviderKind] =
    useState<ProviderKind>("openai_compatible");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com/v1");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState("60");
  const [maxConcurrency, setMaxConcurrency] = useState("2");
  const [disableThinking, setDisableThinking] = useState(false);

  const [catalogModels, setCatalogModels] = useState<
    { id: string; display_name: string }[]
  >([]);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [catalogMsg, setCatalogMsg] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setError("");
    setCatalogModels([]);
    setCatalogMsg("");
    setApiKey("");
    if (existing) {
      setName(existing.name);
      setProviderKind(existing.provider_kind);
      setBaseUrl(existing.base_url ?? "");
      setModel(existing.model);
      setTimeoutSeconds(String(existing.timeout_seconds ?? 60));
      setMaxConcurrency(String(existing.max_concurrency ?? 2));
      setDisableThinking(Boolean(existing.disable_thinking));
    } else {
      setName("DeepSeek 记忆提炼");
      setProviderKind("openai_compatible");
      setBaseUrl("https://api.deepseek.com/v1");
      setModel("deepseek-chat");
      setTimeoutSeconds("60");
      setMaxConcurrency("2");
      setDisableThinking(false);
    }
  }, [open, existing]);

  async function fetchCandidates() {
    if (!baseUrl.trim() || catalogBusy) return;
    setCatalogBusy(true);
    setCatalogMsg("");
    setError("");
    try {
      const res = await getModelCatalog({
        provider_kind: providerKind,
        base_url: baseUrl.trim(),
        ...(apiKey.trim()
          ? { api_key: apiKey.trim() }
          : existing
            ? {
                profile_id: existing.profile_id,
                expected_profile_revision: existing.revision,
              }
            : {}),
      });
      if (res.code === "ok") {
        setCatalogModels(res.models);
        if (res.models.length > 0 && !model.trim()) {
          setModel(res.models[0].id);
        }
        setCatalogMsg(`已获取 ${res.models.length} 个候选模型，可在下方选择或手动填写。`);
      } else {
        setCatalogMsg(`服务端返回状态：${res.code}（仍可直接手填模型名称）`);
      }
    } catch (reason) {
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : "获取模型列表失败，可直接手动输入模型 ID。",
      );
    } finally {
      setCatalogBusy(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    const timeout = Number(timeoutSeconds);
    const concurrency = Number(maxConcurrency);
    if (
      !Number.isInteger(timeout) ||
      timeout < 1 ||
      timeout > 600 ||
      !Number.isInteger(concurrency) ||
      concurrency < 1 ||
      concurrency > 32
    ) {
      setError("超时须为 1–600 秒整数，并发上限须为 1–32 整数。");
      return;
    }

    setSaving(true);
    setError("");
    try {
      const saved = existing
        ? await updateModelProfile(existing.profile_id, {
            expected_revision: existing.revision,
            name: name.trim(),
            base_url: baseUrl.trim() || null,
            model: model.trim(),
            timeout_seconds: timeout,
            max_concurrency: concurrency,
            disable_thinking: disableThinking,
            ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
          })
        : await createModelProfile({
            name: name.trim(),
            provider_kind: providerKind,
            base_url: baseUrl.trim() || null,
            model: model.trim(),
            timeout_seconds: timeout,
            max_concurrency: concurrency,
            disable_thinking: disableThinking,
            ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
          });
      await onSaved(saved);
    } catch (reason) {
      setError(
        reason instanceof ModelApiError
          ? reason.message
          : "保存模型配置未完成，请核对各字段。",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      width="lg"
      busy={saving}
      title={existing ? `编辑模型配置：${existing.name}` : "添加记忆整理模型"}
      subtitle="默认检索向量使用内置离线模型，此处仅需填写文本生成模型配置"
      onClose={onClose}
    >
      <form className="cmh-stack" onSubmit={(e) => void handleSubmit(e)}>
        {!existing ? (
          <div className="cmh-field">
            <span className="cmh-field__label">常用服务商快速填充</span>
            <div className="cmh-agent-pills">
              {PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  className="cmh-agent-pill"
                  onClick={() => {
                    setName(`${preset.label} 记忆提炼`);
                    setProviderKind(preset.provider);
                    setBaseUrl(preset.baseUrl);
                    setModel(preset.placeholderModel);
                  }}
                >
                  {preset.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <div className="cmh-two-col">
          <div className="cmh-field">
            <label htmlFor="cmh-mp-name">配置显示名称</label>
            <input
              id="cmh-mp-name"
              className="cmh-input"
              required
              value={name}
              disabled={saving}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="cmh-field">
            <label htmlFor="cmh-mp-provider">接口协议</label>
            <select
              id="cmh-mp-provider"
              className="cmh-select"
              value={providerKind}
              disabled={saving || Boolean(existing)}
              onChange={(e) => setProviderKind(e.target.value as ProviderKind)}
            >
              <option value="openai_compatible">OpenAI 兼容接口 (/v1/chat/completions)</option>
              <option value="anthropic">Anthropic Messages (/v1/messages)</option>
              <option value="ollama">Ollama 本地服务</option>
            </select>
          </div>
        </div>

        <div className="cmh-field">
          <label htmlFor="cmh-mp-url">接口地址 (Base URL)</label>
          <input
            id="cmh-mp-url"
            className="cmh-input cmh-mono"
            placeholder="https://api.deepseek.com/v1"
            value={baseUrl}
            disabled={saving}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </div>

        <div className="cmh-field">
          <label htmlFor="cmh-mp-key">
            API Key {existing?.has_api_key ? "（留空则保持原已保存密钥不变）" : ""}
          </label>
          <input
            id="cmh-mp-key"
            type="password"
            autoComplete="off"
            className="cmh-input cmh-mono"
            placeholder={existing?.has_api_key ? "••••••••••••••••" : "sk-..."}
            value={apiKey}
            disabled={saving}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>

        <div className="cmh-field">
          <div className="cmh-field__row">
            <label htmlFor="cmh-mp-model">模型 ID (Model)</label>
            <Button
              variant="ghost"
              size="sm"
              loading={catalogBusy}
              disabled={saving || !baseUrl.trim()}
              onClick={() => void fetchCandidates()}
            >
              <ListFilter size={13} /> 从服务端拉取候选模型
            </Button>
          </div>
          {catalogModels.length > 0 ? (
            <select
              aria-label="选择候选模型"
              className="cmh-select cmh-mb-2"
              value={catalogModels.some((m) => m.id === model) ? model : ""}
              onChange={(e) => {
                if (e.target.value) setModel(e.target.value);
              }}
            >
              <option value="">从已拉取的列表中选择…</option>
              {catalogModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name} ({m.id})
                </option>
              ))}
            </select>
          ) : null}
          <input
            id="cmh-mp-model"
            className="cmh-input cmh-mono"
            required
            placeholder="例如：deepseek-chat 或 claude-sonnet-4-20250514"
            value={model}
            disabled={saving}
            onChange={(e) => setModel(e.target.value)}
          />
          {catalogMsg ? <small className="cmh-muted">{catalogMsg}</small> : null}
        </div>

        <details className="cmh-CleanDetails">
          <summary>高级调度参数（超时、并发与推理开关）</summary>
          <div className="cmh-two-col cmh-mt-3">
            <div className="cmh-field">
              <label htmlFor="cmh-mp-timeout">单次请求超时（秒）</label>
              <input
                id="cmh-mp-timeout"
                type="number"
                min={1}
                max={600}
                className="cmh-input cmh-mono"
                value={timeoutSeconds}
                onChange={(e) => setTimeoutSeconds(e.target.value)}
              />
            </div>
            <div className="cmh-field">
              <label htmlFor="cmh-mp-conc">最大并发请求数</label>
              <input
                id="cmh-mp-conc"
                type="number"
                min={1}
                max={32}
                className="cmh-input cmh-mono"
                value={maxConcurrency}
                onChange={(e) => setMaxConcurrency(e.target.value)}
              />
            </div>
          </div>
          <label className="cmh-checkbox cmh-mt-3">
            <input
              type="checkbox"
              checked={disableThinking}
              onChange={(e) => setDisableThinking(e.target.checked)}
            />
            <span>关闭推理思考链（适用于混合推理模型以降低整理延迟与 Token 开销）</span>
          </label>
        </details>

        {error ? (
          <p role="alert" className="cmh-alert cmh-alert--danger">
            {error}
          </p>
        ) : null}

        <div className="cmh-modal__inline-footer">
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            取消
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={saving}
            disabled={!name.trim() || !model.trim()}
          >
            {existing ? "保存更新" : "创建配置"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
