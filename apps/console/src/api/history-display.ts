type JsonObject = Record<string, unknown>;

export interface HandoffDocument {
  goal: string;
  state: string;
  decisions: string[];
  next_steps: string[];
  blockers: string[];
}

export type HistoryDisplay =
  | { kind: "remember"; text: string }
  | { kind: "handoff"; handoff: HandoffDocument }
  | {
      kind: "conversation";
      messages: { role: "user" | "assistant" | "tool"; text: string }[];
    };

export interface HistorySummaryPreview {
  kindLabel: string;
  badgeTone: "indigo" | "blue" | "emerald" | "neutral";
  headline: string;
  subline: string;
}

const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const eventKeys = [
  "protocol_version",
  "event_id",
  "instance_id",
  "device_id",
  "source_id",
  "scope_id",
  "conversation_id",
  "record_id",
  "revision",
  "kind",
  "occurred_at",
  "content",
  "privacy_policy",
];
const identityKeys = [
  "event_id",
  "instance_id",
  "device_id",
  "source_id",
  "conversation_id",
  "record_id",
];

function object(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function onlyKeys(value: JsonObject, keys: string[]) {
  return Object.keys(value).every((key) => keys.includes(key));
}
function text(value: unknown, maxLength: number): value is string {
  return (
    typeof value === "string" && value.length > 0 && value.length <= maxLength
  );
}
function metadata(value: unknown) {
  return value === undefined || object(value);
}
function handoff(value: unknown): value is HandoffDocument {
  return (
    object(value) &&
    onlyKeys(value, ["goal", "state", "decisions", "next_steps", "blockers"]) &&
    text(value.goal, 4096) &&
    !!value.goal.trim() &&
    text(value.state, 4096) &&
    !!value.state.trim() &&
    [value.decisions, value.next_steps, value.blockers].every(
      (items) =>
        Array.isArray(items) &&
        items.length <= 8 &&
        items.every((item) => text(item, 1024) && !!item.trim()),
    )
  );
}

export function historyDisplay(item: {
  raw_id: string;
  content: string;
  truncated: boolean;
}): HistoryDisplay | null {
  if (
    !item.raw_id.startsWith("cmhe:") ||
    !uuid.test(item.raw_id.slice(5)) ||
    item.truncated
  )
    return null;
  try {
    const event: unknown = JSON.parse(item.content);
    if (
      !object(event) ||
      !onlyKeys(event, eventKeys) ||
      Object.keys(event).length !== eventKeys.length ||
      event.protocol_version !== 1 ||
      event.privacy_policy !== "cmh-credentials-v1" ||
      !identityKeys.every(
        (key) => typeof event[key] === "string" && uuid.test(event[key]),
      ) ||
      !(
        event.scope_id === "global" ||
        (typeof event.scope_id === "string" && uuid.test(event.scope_id))
      ) ||
      !Number.isSafeInteger(event.revision) ||
      Number(event.revision) < 1 ||
      !(
        event.occurred_at === null ||
        (typeof event.occurred_at === "string" &&
          /(?:Z|[+-]\d{2}:\d{2})$/.test(event.occurred_at) &&
          Number.isFinite(Date.parse(event.occurred_at)))
      ) ||
      !object(event.content)
    )
      return null;
    const content = event.content;
    if (
      event.kind === "operation" &&
      onlyKeys(content, ["text", "metadata"]) &&
      text(content.text, 1_000_000) &&
      object(content.metadata) &&
      content.metadata.agent_access_version === 1
    ) {
      if (content.metadata.agent_access_kind === "remember") {
        return { kind: "remember", text: content.text };
      }
      if (content.metadata.agent_access_kind === "handoff") {
        const document: unknown = JSON.parse(content.text);
        if (handoff(document)) return { kind: "handoff", handoff: document };
      }
    }
    if (
      event.kind === "conversation_turn" &&
      onlyKeys(content, ["messages", "metadata"]) &&
      metadata(content.metadata) &&
      Array.isArray(content.messages) &&
      content.messages.length > 0 &&
      content.messages.length <= 200 &&
      content.messages.every(
        (message) =>
          object(message) &&
          onlyKeys(message, ["role", "text", "metadata"]) &&
          (message.role === "user" ||
            message.role === "assistant" ||
            message.role === "tool") &&
          text(message.text, 1_000_000) &&
          metadata(message.metadata),
      )
    ) {
      return {
        kind: "conversation",
        messages: content.messages.map((message) => ({
          role: message.role as "user" | "assistant" | "tool",
          text: message.text as string,
        })),
      };
    }
  } catch {
    // Unrecognized / malformed content must remain visible in its original form.
  }
  return null;
}

/** Clean XML wrapper noise (like <task-notification> or <system-reminder>) ONLY for
 *  compact list previews; full detail views always render the untouched original. */
export function cleanPreviewSnippet(raw: string, maxChars = 140): string {
  const stripped = raw
    .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/gi, " ")
    .replace(/<task-notification>[\s\S]*?<summary>([\s\S]*?)<\/summary>[\s\S]*?<\/task-notification>/gi, "$1 ")
    .replace(/<\/?[a-z][a-z0-9_-]*>/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
  const base = stripped || raw.replace(/\s+/g, " ").trim();
  if ([...base].length <= maxChars) return base;
  return `${[...base].slice(0, maxChars).join("")}…`;
}

export function summarizeHistoryItem(item: {
  raw_id: string;
  content: string;
  truncated: boolean;
}): HistorySummaryPreview {
  const parsed = historyDisplay(item);
  if (!parsed) {
    return {
      kindLabel: item.truncated ? "长文记录" : "原始记录",
      badgeTone: "neutral",
      headline: cleanPreviewSnippet(item.content, 120) || "（空白记录）",
      subline: item.truncated ? "内容较长，点击查看并分段加载完整原文" : "原始归档记录",
    };
  }
  if (parsed.kind === "handoff") {
    return {
      kindLabel: "任务交接",
      badgeTone: "blue",
      headline: cleanPreviewSnippet(parsed.handoff.goal, 120),
      subline: `进展：${cleanPreviewSnippet(parsed.handoff.state, 90)}`,
    };
  }
  if (parsed.kind === "remember") {
    return {
      kindLabel: "保存的记忆",
      badgeTone: "emerald",
      headline: cleanPreviewSnippet(parsed.text, 120),
      subline: "由 Agent 显式提交保存",
    };
  }
  const userMessage = parsed.messages.find((m) => m.role === "user");
  const assistantMessage = parsed.messages.find((m) => m.role === "assistant");
  const primary = userMessage ?? parsed.messages[0];
  const secondary =
    primary === userMessage && assistantMessage
      ? `助手：${cleanPreviewSnippet(assistantMessage.text, 90)}`
      : `共 ${parsed.messages.length} 条对话消息`;
  return {
    kindLabel: "对话同步",
    badgeTone: "indigo",
    headline: cleanPreviewSnippet(primary.text, 120),
    subline: secondary,
  };
}
