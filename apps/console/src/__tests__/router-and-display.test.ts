import { describe, expect, it } from "vitest";
import {
  cleanPreviewSnippet,
  historyDisplay,
  summarizeHistoryItem,
} from "../api/history-display";
import { parseRoute, pushRoute } from "../router";

const VALID_ENVELOPE = {
  protocol_version: 1,
  event_id: "11111111-1111-4111-8111-111111111111",
  instance_id: "22222222-2222-4222-8222-222222222222",
  device_id: "33333333-3333-4333-8333-333333333333",
  source_id: "44444444-4444-4444-8444-444444444444",
  scope_id: "55555555-5555-4555-8555-555555555555",
  conversation_id: "66666666-6666-4666-8666-666666666666",
  record_id: "77777777-7777-4777-8777-777777777777",
  revision: 1,
  occurred_at: "2026-11-18T02:00:00Z",
  privacy_policy: "cmh-credentials-v1",
};

describe("console URL query router", () => {
  it("parses canonical views, legacy aliases and settings tabs", () => {
    expect(parseRoute("")).toEqual({ view: "overview", tab: "model" });
    expect(parseRoute("?view=memories")).toEqual({
      view: "memories",
      tab: "model",
    });
    expect(parseRoute("?view=library")).toEqual({
      view: "memories",
      tab: "model",
    });
    expect(parseRoute("?view=connections")).toEqual({
      view: "devices",
      tab: "model",
    });
    expect(parseRoute("?view=settings&tab=backups")).toEqual({
      view: "settings",
      tab: "backups",
    });
    expect(parseRoute("?view=unknown&tab=invalid")).toEqual({
      view: "overview",
      tab: "model",
    });
  });

  it("pushes query params without changing pathname so CloudBoundary auth redirect works", () => {
    window.history.replaceState({}, "", "/hub");
    const next = pushRoute({ view: "settings", tab: "usage" });
    expect(next).toEqual({ view: "settings", tab: "usage" });
    expect(window.location.pathname).toBe("/hub");
    expect(window.location.search).toBe("?view=settings&tab=usage");

    pushRoute({ view: "overview" });
    expect(window.location.pathname).toBe("/hub");
    expect(window.location.search).toBe("");
  });
});

describe("history envelope projection and preview noise cleaning", () => {
  it("extracts readable summaries from XML task notifications without altering full display", () => {
    const rawMsg =
      "<task-notification><task-id>abc</task-id><summary>Codex 历史导入适配测试全部通过</summary></task-notification>";
    expect(cleanPreviewSnippet(rawMsg)).toBe("Codex 历史导入适配测试全部通过");

    const rawId = "cmhe:88888888-8888-4888-8888-888888888888";
    const content = JSON.stringify({
      ...VALID_ENVELOPE,
      kind: "conversation_turn",
      content: {
        messages: [
          { role: "user", text: rawMsg },
          { role: "assistant", text: "好的，准备合并分支。" },
        ],
      },
    });

    const summary = summarizeHistoryItem({
      raw_id: rawId,
      content,
      truncated: false,
    });
    expect(summary.kindLabel).toBe("对话同步");
    expect(summary.headline).toBe("Codex 历史导入适配测试全部通过");
    expect(summary.subline).toContain("准备合并分支");

    // Full display preserves the exact original text untouched
    const full = historyDisplay({ raw_id: rawId, content, truncated: false });
    expect(full?.kind).toBe("conversation");
    if (full?.kind === "conversation") {
      expect(full.messages[0].text).toBe(rawMsg);
    }
  });

  it("refuses truncated or non-cmhe records in structured projection", () => {
    const rawId = "cmhe:88888888-8888-4888-8888-888888888888";
    const content = JSON.stringify({
      ...VALID_ENVELOPE,
      kind: "operation",
      content: {
        text: "合成偏好：使用中文回复",
        metadata: { agent_access_version: 1, agent_access_kind: "remember" },
      },
    });
    expect(
      historyDisplay({ raw_id: rawId, content, truncated: true }),
    ).toBeNull();
    expect(
      historyDisplay({ raw_id: "plain-id", content, truncated: false }),
    ).toBeNull();
    expect(
      historyDisplay({ raw_id: rawId, content, truncated: false }),
    ).toEqual({
      kind: "remember",
      text: "合成偏好：使用中文回复",
    });
  });
});
