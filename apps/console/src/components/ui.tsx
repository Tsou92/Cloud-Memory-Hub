import { Check, Copy, Loader2, X } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";

export type Tone =
  | "indigo"
  | "emerald"
  | "amber"
  | "danger"
  | "blue"
  | "neutral";

export const MEMORY_LAYER_META: Record<
  string,
  { label: string; tone: Tone }
> = {
  l0_basic_info: { label: "基本信息", tone: "blue" },
  l1_raw: { label: "原始对话", tone: "neutral" },
  l2_fact: { label: "事实记忆", tone: "indigo" },
  l3_summary: { label: "会话摘要", tone: "blue" },
  l4_identity: { label: "个人画像", tone: "emerald" },
  l5_knowledge: { label: "关联知识", tone: "indigo" },
  l6_schema: { label: "经验模式", tone: "amber" },
  l7_intention: { label: "计划与意图", tone: "emerald" },
};

export function layerMeta(layer: string | null | undefined): {
  label: string;
  tone: Tone;
} {
  if (!layer) return { label: "长期记忆", tone: "indigo" };
  return MEMORY_LAYER_META[layer] ?? { label: layer, tone: "neutral" };
}

export function formatTimestamp(
  secondsOrIso: number | string | null | undefined,
): string {
  if (secondsOrIso === null || secondsOrIso === undefined || secondsOrIso === "")
    return "时间未知";
  const date =
    typeof secondsOrIso === "number"
      ? new Date(secondsOrIso * 1000)
      : new Date(secondsOrIso);
  if (!Number.isFinite(date.getTime())) return "时间未知";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatFullTimestamp(
  secondsOrIso: number | string | null | undefined,
): string {
  if (secondsOrIso === null || secondsOrIso === undefined || secondsOrIso === "")
    return "暂无记录";
  const date =
    typeof secondsOrIso === "number"
      ? new Date(secondsOrIso * 1000)
      : new Date(secondsOrIso);
  if (!Number.isFinite(date.getTime())) return "暂无记录";
  return date.toLocaleString("zh-CN");
}

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  disabled,
  className = "",
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
}) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={`cmh-btn cmh-btn--${variant} cmh-btn--${size}${className ? ` ${className}` : ""}`}
      {...rest}
    >
      {loading ? <Loader2 size={15} className="cmh-spin" aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

export function Badge({
  tone = "neutral",
  dot = false,
  children,
  className = "",
}: {
  tone?: Tone;
  dot?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`cmh-badge cmh-badge--${tone}${className ? ` ${className}` : ""}`}
    >
      {dot ? <i className="cmh-badge__dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

export function Switch({
  checked,
  onChange,
  disabled = false,
  label,
}: {
  checked: boolean;
  onChange(next: boolean): void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`cmh-switch${checked ? " is-checked" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span className="cmh-switch__thumb" />
    </button>
  );
}

export function Modal({
  open,
  title,
  subtitle,
  onClose,
  busy = false,
  width = "md",
  footer,
  children,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose(): void;
  busy?: boolean;
  width?: "sm" | "md" | "lg";
  footer?: ReactNode;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previous?.isConnected) previous.focus();
    };
  }, [open, busy, onClose]);

  if (!open) return null;
  return (
    <div
      className="cmh-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`cmh-modal cmh-modal--${width}`}
      >
        <header className="cmh-modal__header">
          <div>
            <h2>{title}</h2>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <button
            type="button"
            className="cmh-icon-btn"
            aria-label="关闭弹窗"
            disabled={busy}
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>
        <div className="cmh-modal__body">{children}</div>
        {footer ? <footer className="cmh-modal__footer">{footer}</footer> : null}
      </div>
    </div>
  );
}

export function Drawer({
  open,
  title,
  subtitle,
  onClose,
  actions,
  children,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose(): void;
  actions?: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previous?.isConnected) previous.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="cmh-drawer-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="cmh-drawer"
      >
        <header className="cmh-drawer__header">
          <div className="cmh-drawer__titles">
            <h2>{title}</h2>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <div className="cmh-drawer__header-actions">
            {actions}
            <button
              type="button"
              className="cmh-icon-btn"
              aria-label="关闭详情面板"
              onClick={onClose}
            >
              <X size={18} />
            </button>
          </div>
        </header>
        <div className="cmh-drawer__body">{children}</div>
      </aside>
    </div>
  );
}

export function CopyBlock({
  label,
  code,
  copyLabel = "复制",
  onCopied,
}: {
  label?: string;
  code: string;
  copyLabel?: string;
  onCopied?(ok: boolean): void;
}) {
  const [copied, setCopied] = useState(false);
  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      onCopied?.(true);
      window.setTimeout(() => setCopied(false), 2200);
    } catch {
      setCopied(false);
      onCopied?.(false);
    }
  }
  return (
    <div className="cmh-codeblock">
      <div className="cmh-codeblock__bar">
        <span>{label ?? "命令 / 内容"}</span>
        <button
          type="button"
          className="cmh-btn cmh-btn--ghost cmh-btn--sm"
          onClick={() => void handleCopy()}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
          {copied ? "已复制" : copyLabel}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  actions,
  children,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="cmh-empty" role="note">
      {icon ? <div className="cmh-empty__icon">{icon}</div> : null}
      <h3>{title}</h3>
      <p>{description}</p>
      {children}
      {actions ? <div className="cmh-empty__actions">{actions}</div> : null}
    </div>
  );
}
