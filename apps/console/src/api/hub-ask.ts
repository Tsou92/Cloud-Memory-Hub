import { HubError } from "./hub-api";

export interface AskCitation {
  memory_id: string;
  content: string;
  layer?: string;
}

export interface HubAskAnswer {
  schema_version: 2;
  mode: "empty" | "extractive" | "generated";
  answer: string;
  memories: AskCitation[];
}

export interface AskStreamCallbacks {
  onRecall?(memories: AskCitation[]): void;
  onDelta?(partialAnswer: string): void;
}

function validCitations(value: unknown): value is AskCitation[] {
  return (
    Array.isArray(value) &&
    value.every(
      (item) =>
        item !== null &&
        typeof item === "object" &&
        !Array.isArray(item) &&
        typeof (item as Record<string, unknown>).memory_id === "string" &&
        typeof (item as Record<string, unknown>).content === "string",
    )
  );
}

/** Uses the recall/delta/final SSE contract on /hub/v1/console/ask-stream. */
export async function askHubStream(
  scope: string,
  question: string,
  signal: AbortSignal,
  callbacks?: AskStreamCallbacks,
): Promise<HubAskAnswer> {
  const response = await fetch("/hub/v1/console/ask-stream", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_id: scope, question }),
  });
  if (
    !response.ok ||
    !response.body ||
    !response.headers.get("content-type")?.includes("text/event-stream")
  ) {
    throw new HubError(
      response.status === 404
        ? "网页问答尚未接通，请升级 Hub 后重试。"
        : response.status === 401 || response.status === 403
          ? "登录已失效，请重新登录管理网页。"
          : "问答请求未完成，请检查登录状态和模型设置后重试。",
    );
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let streamed = "";
  let finalAnswer: HubAskAnswer | null = null;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(
        /\r\n/g,
        "\n",
      );
      if (buffer.length > 256_000)
        throw new HubError("问答内容超过显示上限，请缩短问题后重试。");
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (!data) continue;
        const event = JSON.parse(data) as Record<string, unknown>;
        if (event.type === "error") {
          throw new HubError(
            "问答未完成，请检查模型设置或稍后重试。已有记忆仍可在记忆库查看。",
          );
        }
        if (event.type === "recall") {
          if (validCitations(event.memories)) {
            callbacks?.onRecall?.(event.memories);
          }
        } else if (event.type === "delta") {
          if (typeof event.text === "string") {
            streamed += event.text;
            callbacks?.onDelta?.(streamed);
          }
        } else if (event.type === "final") {
          if (
            finalAnswer ||
            event.schema_version !== 2 ||
            typeof event.answer !== "string" ||
            !["empty", "extractive", "generated"].includes(
              String(event.mode),
            ) ||
            !validCitations(event.memories)
          ) {
            throw new HubError("问答返回的数据无法确认，请重试。");
          }
          finalAnswer = {
            schema_version: 2,
            mode: event.mode as HubAskAnswer["mode"],
            answer: event.answer,
            memories: event.memories,
          };
        } else {
          throw new HubError("问答返回的数据无法确认，请重试。");
        }
      }
      if (done) break;
    }
    if (!finalAnswer || buffer.trim())
      throw new HubError("问答连接提前结束，请重试。");
    return finalAnswer;
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
