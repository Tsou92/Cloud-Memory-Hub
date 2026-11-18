import { Coins, Cpu, RefreshCw, Zap } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { getHubUsage, type HubUsageReport } from "../../api/hub-usage";
import { Badge, Button } from "../../components/ui";

export function UsageSettingsTab() {
  const [report, setReport] = useState<HubUsageReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setError("");
    try {
      const next = await getHubUsage(ctrl.signal);
      if (!ctrl.signal.aborted) setReport(next);
    } catch (reason) {
      if (!ctrl.signal.aborted) {
        setError(
          reason instanceof Error && reason.message === "unauthorized"
            ? "管理员登录已失效，请重新登录。"
            : "当前无法读取用量统计，请检查服务连接后重试。",
        );
      }
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => abortRef.current?.abort();
  }, [refresh]);

  const promptPct =
    report && report.total_tokens > 0
      ? Math.round((report.prompt_tokens / report.total_tokens) * 100)
      : 0;

  return (
    <div className="cmh-stack">
      <section className="cmh-panel" aria-label="模型调用与费用估算">
        <div className="cmh-panel__header">
          <div>
            <h2>模型调用统计与费用估算</h2>
            <p>
              汇总记忆整理后台发起的模型请求与 Token 消耗；费用按内置价格表估算，实际结算以模型服务商账单为准。
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={loading}
            onClick={() => void refresh()}
          >
            <RefreshCw size={14} className={loading ? "cmh-spin" : ""} />
            刷新用量
          </Button>
        </div>

        {error ? (
          <p role="alert" className="cmh-alert cmh-alert--danger">
            {error}
          </p>
        ) : null}

        {loading && !report ? (
          <p role="status" className="cmh-muted">
            正在读取审计统计…
          </p>
        ) : report ? (
          <div className="cmh-stack">
            <div className="cmh-usage-kpis">
              <div className="cmh-usage-kpi">
                <div className="cmh-usage-kpi__head">
                  <span>累计请求次数</span>
                  <Cpu size={16} />
                </div>
                <strong className="cmh-mono">
                  {report.request_count.toLocaleString("zh-CN")}
                </strong>
                <div className="cmh-usage-kpi__sub">
                  <Badge tone="emerald">
                    成功 {report.succeeded.toLocaleString("zh-CN")}
                  </Badge>
                  {report.failed > 0 ? (
                    <Badge tone="danger">
                      失败 {report.failed.toLocaleString("zh-CN")}
                    </Badge>
                  ) : null}
                  {report.in_flight > 0 ? (
                    <Badge tone="blue">进行中 {report.in_flight}</Badge>
                  ) : null}
                </div>
              </div>

              <div className="cmh-usage-kpi">
                <div className="cmh-usage-kpi__head">
                  <span>合计消耗 Tokens</span>
                  <Zap size={16} />
                </div>
                <strong className="cmh-mono">
                  {report.total_tokens.toLocaleString("zh-CN")}
                </strong>
                <span className="cmh-muted cmh-mono">
                  输入 {report.prompt_tokens.toLocaleString("zh-CN")} · 输出{" "}
                  {report.completion_tokens.toLocaleString("zh-CN")}
                </span>
              </div>

              <div className="cmh-usage-kpi">
                <div className="cmh-usage-kpi__head">
                  <span>预估累计费用</span>
                  <Coins size={16} />
                </div>
                <strong className="cmh-mono">
                  {!report.cost
                    ? "未启用定价"
                    : report.cost.priced_requests === 0
                      ? "暂无可定价模型"
                      : `${report.cost.currency} ${report.cost.total.toFixed(4)}`}
                </strong>
                <span className="cmh-muted">
                  {report.cost && report.cost.priced_requests > 0
                    ? `覆盖 ${report.cost.priced_requests.toLocaleString("zh-CN")} 次已定价调用`
                    : "尚未命中内置定价模型的请求"}
                </span>
              </div>
            </div>

            {report.total_tokens > 0 ? (
              <div className="cmh-panel cmh-panel--inset">
                <div className="cmh-token-bar-head">
                  <span>输入 Prompt ({promptPct}%)</span>
                  <span>输出 Completion ({100 - promptPct}%)</span>
                </div>
                <div className="cmh-composition__bar">
                  <span
                    className="cmh-composition__seg is-indigo"
                    style={{ width: `${promptPct}%` }}
                  />
                  <span
                    className="cmh-composition__seg is-emerald"
                    style={{ width: `${100 - promptPct}%` }}
                  />
                </div>
              </div>
            ) : null}

            {report.cost && report.cost.unpriced_models.length > 0 ? (
              <div className="cmh-alert cmh-alert--amber">
                以下模型不在内置单价表中，其消耗未计入上方金额（可通过环境变量{" "}
                <code className="cmh-mono">CMH_MODEL_PRICES</code> 补充定价）：
                <strong className="cmh-mono">
                  {" "}
                  {report.cost.unpriced_models.join("、")}
                </strong>
              </div>
            ) : null}

            <div className="cmh-prose-sm cmh-muted">
              <p>
                计费规则说明：内置单价覆盖 Claude、DeepSeek（按官方 UTC 峰谷时段分时计价）、OpenAI 与 Gemini 常用模型。网页「问记忆」的流式问答暂未合并入此整理审计表。
              </p>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
