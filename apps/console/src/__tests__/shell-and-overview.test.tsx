import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const INSTANCE = "11111111-2222-4333-8444-555555555555";
const PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Shell switcher, project creation fence, and Overview drawer", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("auto-selects a single project, renders overview KPIs, and opens the original drawer on activity click", async () => {
    const rawId = "cmhe:99999999-9999-4999-8999-999999999999";
    const envelope = JSON.stringify({
      protocol_version: 1,
      event_id: "11111111-1111-4111-8111-111111111111",
      instance_id: INSTANCE,
      device_id: "33333333-3333-4333-8333-333333333333",
      source_id: "44444444-4444-4444-8444-444444444444",
      scope_id: PROJECT_A,
      conversation_id: "66666666-6666-4666-8666-666666666666",
      record_id: "77777777-7777-4777-8777-777777777777",
      revision: 1,
      kind: "conversation_turn",
      occurred_at: "2026-11-18T01:00:00Z",
      privacy_policy: "cmh-credentials-v1",
      content: {
        messages: [{ role: "user", text: "合成验收：重构后台总览交互" }],
      },
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/hub/v1/console/bootstrap")) {
        return jsonResponse({
          schema_version: 1,
          mode: "hub",
          product: "Cloud Memory Hub",
          instance_id: INSTANCE,
          version: "0.1.10",
          build_revision: "a".repeat(12),
          features: ["scoped_console_reads", "scoped_console_ask"],
          projects: [{ scope_id: PROJECT_A, display_name: "合成项目 Alpha" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.includes("/hub/v1/console/dashboard")) {
        return jsonResponse({
          schema_version: 1,
          instance_id: INSTANCE,
          scope_id: PROJECT_A,
          generated_at: "2026-11-18T02:00:00Z",
          statistics: {
            device_connections: 2,
            agent_connections: 3,
            shared_records: 18,
            handoffs: 4,
          },
          record_types: [
            { kind: "conversation", label: "会话记录", count: 12 },
            { kind: "remember", label: "保存的记忆", count: 2 },
            { kind: "handoff", label: "任务交接", count: 4 },
            { kind: "other", label: "其他记录", count: 0 },
          ],
          daily_activity: [
            { date: "2026-11-12", count: 1 },
            { date: "2026-11-13", count: 2 },
            { date: "2026-11-14", count: 0 },
            { date: "2026-11-15", count: 3 },
            { date: "2026-11-16", count: 4 },
            { date: "2026-11-17", count: 5 },
            { date: "2026-11-18", count: 3 },
          ],
          activity_timezone: "UTC",
          activity_state: "ready",
          missing_timestamps: 0,
        });
      }
      if (url.includes("/hub/v1/console/summary")) {
        return jsonResponse({
          schema_version: 1,
          instance_id: INSTANCE,
          scope: { scope_id: PROJECT_A, display_name: "合成项目 Alpha" },
          sources: [],
          sources_total: 0,
          next_offset: null,
          visible_memory_total: null,
          visible_history_total: null,
        });
      }
      if (url.includes("/hub/v1/devices")) {
        return jsonResponse({ devices: [] });
      }
      if (url.includes("/hub/v1/console/read/history-list")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: PROJECT_A,
          operation: "history-list",
          content_trust: "untrusted_history",
          items: [
            {
              record_id: rawId,
              kind: "history",
              content: envelope,
              content_offset: 0,
              next_content_offset: null,
              layer: "l1_raw",
              agent_id: "claude",
              session_id: "sess-1",
              source: "home-mac",
              source_raw_ids: [],
              memory_at: 1763431200,
            },
          ],
          returned_count: 1,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      if (url.includes("/hub/v1/backups/status")) {
        return jsonResponse({
          schedule: { schema_version: 1, enabled: false },
          execution: { schema_version: 1, state: "completed" },
          next_due_at: null,
          pending_request: null,
          supervisor_running: "not_checked",
        });
      }
      if (url.includes("/api/v1/model-profiles")) {
        return jsonResponse([]);
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "合成项目 Alpha" }),
      ).toBeTruthy();
    });
    expect(await screen.findByText("18")).toBeTruthy();

    // Click recent activity item -> opens slide-over OriginalDrawer without navigating away
    await user.click(await screen.findByText("合成验收：重构后台总览交互"));
    expect(
      await screen.findByRole("dialog", { name: "原始记录详情" }),
    ).toBeTruthy();
    expect(screen.getByText("原始信封")).toBeTruthy();
  });

  it("creates a new project via the modal and remembers selection", async () => {
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
          projects: [],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.endsWith("/hub/v1/projects") && init?.method === "POST") {
        return jsonResponse(
          { project_id: PROJECT_B, display_name: "新建合成空间" },
          201,
        );
      }
      if (url.includes("/hub/v1/console/dashboard")) {
        return jsonResponse({
          schema_version: 1,
          instance_id: INSTANCE,
          scope_id: PROJECT_B,
          generated_at: "2026-11-18T02:00:00Z",
          statistics: {
            device_connections: 0,
            agent_connections: 0,
            shared_records: 0,
            handoffs: 0,
          },
          record_types: [
            { kind: "conversation", label: "会话记录", count: 0 },
            { kind: "remember", label: "保存的记忆", count: 0 },
            { kind: "handoff", label: "任务交接", count: 0 },
            { kind: "other", label: "其他记录", count: 0 },
          ],
          daily_activity: [],
          activity_timezone: "UTC",
          activity_state: "unavailable",
          missing_timestamps: 1,
        });
      }
      if (url.includes("/hub/v1/console/summary")) {
        return jsonResponse({
          schema_version: 1,
          instance_id: INSTANCE,
          scope: { scope_id: PROJECT_B, display_name: "新建合成空间" },
          sources: [],
          sources_total: 0,
          next_offset: null,
          visible_memory_total: null,
          visible_history_total: null,
        });
      }
      if (url.includes("/hub/v1/devices")) return jsonResponse({ devices: [] });
      if (url.includes("/hub/v1/console/read/history-list")) {
        return jsonResponse({
          schema_version: 1,
          authority: "administrator",
          instance_id: INSTANCE,
          scope_id: PROJECT_B,
          operation: "history-list",
          content_trust: "untrusted_history",
          items: [],
          returned_count: 0,
          next_offset: null,
          candidate_limit_reached: false,
        });
      }
      if (url.includes("/hub/v1/backups/status")) {
        return jsonResponse({
          schedule: { schema_version: 1, enabled: false },
          execution: { schema_version: 1, state: "not_run" },
          next_due_at: null,
          pending_request: null,
          supervisor_running: "not_checked",
        });
      }
      if (url.includes("/api/v1/model-profiles")) return jsonResponse([]);
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /创建第一个项目/ }));
    const input = await screen.findByLabelText("项目名称");
    await user.type(input, "新建合成空间");
    await user.click(screen.getByRole("button", { name: "创建并进入项目" }));

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "新建合成空间" }),
      ).toBeTruthy();
    });
    expect(localStorage.getItem(`cmh_hub_scope:${INSTANCE}`)).toBe(PROJECT_B);
  });
});
