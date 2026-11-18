import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

const INSTANCE = "11111111-2222-4333-8444-555555555555";
const SCOPE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const DEV_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const SRC_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const PAIRING_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const BACKUP_A = "cmh-backup-20261118T010000000000Z-" + "a".repeat(32);
const BACKUP_B = "cmh-backup-20261117T010000000000Z-" + "b".repeat(32);

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Devices management, one-link wizard, and Settings tabs", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("displays connected devices directly and generates a 10-minute pairing link in a modal", async () => {
    window.history.replaceState({}, "", "/?view=devices");

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
          projects: [{ scope_id: SCOPE, display_name: "多端共享空间" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.endsWith("/hub/v1/devices")) {
        return jsonResponse({
          devices: [
            {
              device_id: DEV_ID,
              display_name: "办公室 Mac mini",
              revision: 1,
              status: "active",
              created_at: "2026-11-18T00:00:00Z",
              grants: [
                {
                  scope_id: SCOPE,
                  permissions: ["events:write", "memory:read", "history:read"],
                },
              ],
            },
          ],
        });
      }
      if (url.endsWith("/hub/v1/sources")) {
        return jsonResponse({
          sources: [
            {
              source_id: SRC_ID,
              device_id: DEV_ID,
              scope_id: SCOPE,
              adapter_id: "cloud-memory-hub.agent",
              display_name: "Claude Code",
              revision: 1,
              status: "active",
            },
          ],
        });
      }
      if (url.includes("/hub/v1/console/summary")) {
        return jsonResponse({
          schema_version: 1,
          instance_id: INSTANCE,
          scope: { scope_id: SCOPE, display_name: "多端共享空间" },
          sources: [
            {
              source_id: SRC_ID,
              device_id: DEV_ID,
              scope_id: SCOPE,
              display_name: "Claude Code",
              adapter_id: "cloud-memory-hub.agent",
              status: "active",
              device_display_name: "办公室 Mac mini",
              device_status: "active",
              last_accepted_at: null,
            },
          ],
          sources_total: 1,
          next_offset: null,
          visible_memory_total: null,
          visible_history_total: null,
        });
      }
      if (url.endsWith("/hub/v1/pairings") && init?.method === "POST") {
        return jsonResponse({
          pairing_id: PAIRING_ID,
          invitation_secret: "A".repeat(43),
          expires_at: "2026-11-18T03:00:00Z",
          origin: "https://hub.example.test",
        });
      }
      if (url.endsWith(`/hub/v1/pairings/${PAIRING_ID}`)) {
        return jsonResponse({
          pairing_id: PAIRING_ID,
          scope_id: SCOPE,
          state: "pending",
          expires_at: "2026-11-18T03:00:00Z",
          device_id: null,
          source_id: null,
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    // Connected device is shown on main surface (not buried inside <details>)
    expect(
      await screen.findByRole("heading", { level: 3, name: "办公室 Mac mini" }),
    ).toBeTruthy();
    expect(await screen.findByText("最近上传：尚无记录")).toBeTruthy();

    // Open Connect New Agent modal
    await user.click(screen.getByRole("button", { name: /连接新 Agent/ }));
    const devInput = await screen.findByLabelText(/为这台电脑起一个易识别的设备名/);
    await user.type(devInput, "家里 MacBook Pro");
    await user.click(
      screen.getByRole("button", { name: /生成 10 分钟连接链接/ }),
    );

    await waitFor(() => {
      expect(screen.getByText(/等待目标电脑领取配对/)).toBeTruthy();
    });
    expect(screen.getByText(/https:\/\/hub\.example\.test\/hub\/connect/)).toBeTruthy();
  });

  it("renders usage cost estimate and binds selected backup ID into export/restore commands", async () => {
    window.history.replaceState({}, "", "/?view=settings&tab=usage");

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
          projects: [{ scope_id: SCOPE, display_name: "多端共享空间" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url.includes("/hub/v1/console/usage")) {
        return jsonResponse({
          request_count: 42,
          succeeded: 41,
          failed: 1,
          in_flight: 0,
          prompt_tokens: 12000,
          completion_tokens: 3000,
          total_tokens: 15000,
          cost: {
            available: true,
            currency: "USD",
            total: 0.0835,
            priced_requests: 40,
            unpriced_models: ["custom-local-llm"],
          },
        });
      }
      if (url.includes("/hub/v1/backups/status")) {
        return jsonResponse({
          schedule: {
            schema_version: 1,
            enabled: true,
            interval_minutes: 1440,
            keep: 7,
            configured_at: "2026-11-18T00:00:00Z",
          },
          execution: {
            schema_version: 1,
            state: "completed",
            last_success_at: "2026-11-18T01:00:00Z",
            last_backup_id: BACKUP_A,
          },
          next_due_at: "2026-11-19T01:00:00Z",
          pending_request: null,
          supervisor_running: "not_checked",
        });
      }
      if (url.includes("/hub/v1/backups/versions")) {
        return jsonResponse({
          versions: [
            {
              backup_id: BACKUP_A,
              state: "unverified",
              created_at: "2026-11-18T01:00:00Z",
              revision: "v0.1.10",
              file_count: 9,
              total_bytes: 5242880,
            },
            {
              backup_id: BACKUP_B,
              state: "unverified",
              created_at: "2026-11-17T01:00:00Z",
              revision: "v0.1.9",
              file_count: 8,
              total_bytes: 4194304,
            },
          ],
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText("USD 0.0835")).toBeTruthy();
    expect(screen.getByText(/custom-local-llm/)).toBeTruthy();

    // Switch to Backups tab -> URL updates & backup ID auto-populates command
    await user.click(screen.getByRole("button", { name: "备份与恢复" }));
    expect(window.location.search).toBe("?view=settings&tab=backups");

    await waitFor(() => {
      expect(screen.getAllByText(new RegExp(BACKUP_A)).length).toBeGreaterThan(1);
    });

    // Click "填入命令" on the second version row -> command updates to BACKUP_B
    await user.click(screen.getByRole("button", { name: "填入命令" }));
    expect(
      screen.getByText(new RegExp(`restore --writers-stopped --version ${BACKUP_B}`)),
    ).toBeTruthy();
  });
});
