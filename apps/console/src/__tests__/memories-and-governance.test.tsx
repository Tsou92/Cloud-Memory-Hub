import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const INSTANCE = "11111111-2222-4333-8444-555555555555";
const SCOPE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const MEM_ID = "mem_synthetic_001";
const RAW_ID = "cmhe:88888888-8888-4888-8888-888888888888";
const DIGEST = "a".repeat(64);

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Memories master-detail editing, source provenance drawer, and Governance modal", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, "", "/?view=memories");
  });

  it("supports inline memory correction, opening source drawer, and executing forget via modal", async () => {
    let storedContent = "初始合成记忆：项目部署在自建 NAS Docker。";

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
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
          projects: [{ scope_id: SCOPE, display_name: "研发项目" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.includes("/hub/v1/console/read/memory-list")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: SCOPE,
          operation: "memory-list",
          content_trust: "untrusted_history",
          items: [
            {
              record_id: MEM_ID,
              kind: "memory",
              content: storedContent,
              content_offset: 0,
              next_content_offset: null,
              layer: "l2_fact",
              agent_id: "claude",
              session_id: null,
              source: null,
              source_raw_ids: [RAW_ID],
              memory_at: 1763431200,
            },
          ],
          returned_count: 1,
          next_offset: null,
          candidate_limit_reached: false,
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
              record_id: MEM_ID,
              kind: "memory",
              content: storedContent,
              content_offset: 0,
              next_content_offset: null,
              layer: "l2_fact",
              agent_id: "claude",
              session_id: null,
              source: null,
              source_raw_ids: [RAW_ID],
              memory_at: 1763431200,
            },
          ],
          returned_count: 1,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      if (url.includes("/hub/v1/console/correct")) {
        const body = JSON.parse(String(init?.body)) as { content: string };
        storedContent = body.content;
        return jsonResponse({
          schema_version: 1,
          scope_id: SCOPE,
          record_id: MEM_ID,
          content: storedContent,
          state: "corrected",
        });
      }
      if (url.includes("/hub/v1/console/read/history-get")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: SCOPE,
          operation: "history-get",
          content_trust: "untrusted_history",
          items: [
            {
              record_id: RAW_ID,
              kind: "history",
              content: "合成原始对话证据内容",
              content_offset: 0,
              next_content_offset: null,
              layer: "l1_raw",
              agent_id: "claude",
              session_id: "sess-xyz",
              source: "mac-mini",
              source_raw_ids: [],
              memory_at: 1763431200,
            },
          ],
          returned_count: 1,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      if (url.includes("/hub/v1/governance/preview")) {
        const req = JSON.parse(String(init?.body)) as { action: string };
        return jsonResponse({
          scope_id: SCOPE,
          action: req.action,
          view_digest: DIGEST,
          consistency: "observational",
          coverage: "explicit_stored_relations",
          content_trust: "untrusted_history",
          execution_available: true,
          total: 2,
          raw_count: 1,
          memory_count: 1,
          unresolved_origin_count: 0,
          next_offset: null,
          items: [
            {
              record_id: MEM_ID,
              kind: "memory",
              layer: "l2_fact",
              status: "active",
              content: storedContent,
              content_truncated: false,
              reasons: ["selected"],
              unresolved_origin: false,
              proposed_effect: "suppress_automatic_use",
            },
            {
              record_id: RAW_ID,
              kind: "raw",
              layer: "l1_raw",
              status: "active",
              content: "合成原始对话证据内容",
              content_truncated: false,
              reasons: ["source"],
              unresolved_origin: false,
              proposed_effect: "retain_for_explicit_read",
            },
          ],
        });
      }
      if (url.includes("/hub/v1/governance/execute")) {
        const req = JSON.parse(String(init?.body)) as {
          operation_id: string;
          action: string;
        };
        return jsonResponse({
          operation_id: req.operation_id,
          scope_id: SCOPE,
          action: req.action,
          state: "completed",
          suppressed_count: 2,
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    // Detail pane loads selected memory automatically
    await screen.findByRole("button", { name: /纠正内容/ });

    // 1. Inline edit & save
    await user.click(screen.getByRole("button", { name: /纠正内容/ }));
    const editor = screen.getByLabelText("编辑记忆正文");
    await user.clear(editor);
    await user.type(editor, "纠正后的记忆：使用独立前端包构建控制台。");
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      expect(screen.getByText("记忆内容已纠正保存。")).toBeTruthy();
    });

    // 2. Open source provenance drawer
    await user.click(screen.getByRole("button", { name: /来源原文 #1/ }));
    expect(
      await screen.findByRole("dialog", { name: "原始记录详情" }),
    ).toBeTruthy();
    expect(screen.getByText("合成原始对话证据内容")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "关闭详情面板" }));

    // 3. Governance modal (forget)
    await user.click(screen.getByRole("button", { name: /忘记 \/ 删除/ }));
    expect(
      await screen.findByRole("dialog", { name: "记忆治理与影响范围核对" }),
    ).toBeTruthy();
    await user.click(
      await screen.findByRole("checkbox", { name: /确认停止自动召回/ }),
    );
    await user.click(screen.getByRole("button", { name: "确认忘记" }));

    await waitFor(() => {
      expect(
        screen.getByText("忘记已完成。相关内容已停止自动使用，原始历史仍保留。"),
      ).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: "查看保留来源" })).toBeTruthy();
  });
});
