import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const INSTANCE = "11111111-2222-4333-8444-555555555555";
const SCOPE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Streaming Ask workbench and History search / import modal", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("streams SSE answers, displays citations, and opens citation detail in a drawer", async () => {
    window.history.replaceState({}, "", "/?view=ask");

    const sseBody = [
      `data: ${JSON.stringify({ type: "recall", memories: [{ memory_id: "cite-1", content: "引用事实：使用 Tailscale HTTPS 访问。" }] })}\n\n`,
      `data: ${JSON.stringify({ type: "delta", text: "目前决定通过 Tailscale HTTPS " })}\n\n`,
      `data: ${JSON.stringify({ type: "delta", text: "访问自建 NAS。" })}\n\n`,
      `data: ${JSON.stringify({
        type: "final",
        schema_version: 2,
        mode: "generated",
        answer: "目前决定通过 Tailscale HTTPS 访问自建 NAS。",
        memories: [{ memory_id: "cite-1", content: "引用事实：使用 Tailscale HTTPS 访问。" }],
      })}\n\n`,
    ].join("");

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/hub/v1/console/bootstrap")) {
        return jsonResponse({
          schema_version: 1,
          mode: "hub",
          product: "Cloud Memory Hub",
          instance_id: INSTANCE,
          version: "0.1.10",
          build_revision: null,
          features: ["scoped_console_reads", "scoped_console_ask"],
          projects: [{ scope_id: SCOPE, display_name: "网络架构项目" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.includes("/hub/v1/console/ask-stream")) {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(sseBody));
            controller.close();
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (url.includes("/hub/v1/console/read/memory-get")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: SCOPE,
          operation: "memory-get",
          content_trust: "untrusted_history",
          items: [
            {
              record_id: "cite-1",
              kind: "memory",
              content: "引用事实：使用 Tailscale HTTPS 访问。",
              content_offset: 0,
              next_content_offset: null,
              layer: "l2_fact",
              agent_id: "codex",
              session_id: null,
              source: null,
              source_raw_ids: [],
              memory_at: 1763431200,
            },
          ],
          returned_count: 1,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    const box = await screen.findByLabelText("你的问题");
    await user.type(box, "怎么访问 NAS？");
    await user.click(screen.getByRole("button", { name: "提问" }));

    await waitFor(() => {
      expect(
        screen.getByText("目前决定通过 Tailscale HTTPS 访问自建 NAS。"),
      ).toBeTruthy();
    });
    expect(screen.getByText("模型综合回答")).toBeTruthy();

    await user.click(
      screen.getByRole("button", { name: /引用事实：使用 Tailscale HTTPS 访问/ }),
    );
    expect(
      await screen.findByRole("dialog", { name: "引用记忆详情" }),
    ).toBeTruthy();
  });

  it("searches history originals and opens the client import guide modal", async () => {
    window.history.replaceState({}, "", "/?view=history");

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/hub/v1/console/bootstrap")) {
        return jsonResponse({
          schema_version: 1,
          mode: "hub",
          product: "Cloud Memory Hub",
          instance_id: INSTANCE,
          version: "0.1.10",
          build_revision: null,
          features: ["scoped_console_reads", "scoped_console_ask"],
          projects: [{ scope_id: SCOPE, display_name: "网络架构项目" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.includes("/hub/v1/console/read/history-list")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: SCOPE,
          operation: "history-list",
          content_trust: "untrusted_history",
          items: [],
          returned_count: 0,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      if (url.includes("/hub/v1/console/read/history-search")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: SCOPE,
          operation: "history-search",
          content_trust: "untrusted_history",
          items: [
            {
              record_id: "raw-hit-1",
              kind: "history",
              content: "关键词命中的历史原文记录",
              content_offset: 0,
              next_content_offset: null,
              layer: "l1_raw",
              agent_id: "codex",
              session_id: "s1",
              source: "mbp",
              source_raw_ids: [],
              memory_at: 1763431200,
            },
          ],
          returned_count: 1,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    const searchInput = await screen.findByLabelText("搜索历史原文");
    await user.type(searchInput, "关键词命中");
    await user.click(screen.getByRole("button", { name: "检索原文" }));

    expect(await screen.findByText("关键词命中的历史原文记录")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /导入本机已有会话/ }));
    const dialog = await screen.findByRole("dialog", {
      name: "导入本机已有历史会话",
    });
    expect(dialog.textContent).toContain("claude-code");
    expect(dialog.textContent).toContain("codex");
    expect(dialog.textContent).toContain("t3-code");
  });
});
