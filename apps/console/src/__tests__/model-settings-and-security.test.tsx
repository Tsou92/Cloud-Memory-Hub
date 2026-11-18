import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { SecuritySettingsTab } from "../views/settings/SecuritySettingsTab";
import { HubProvider } from "../context/HubContext";

const INSTANCE = "11111111-2222-4333-8444-555555555555";
const SCOPE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROFILE_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Model settings (sedimentation, catalog, recoverable probe) & Admin credential change", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("manages sedimentation toggle, fetches model candidates, creates and probes a model profile", async () => {
    window.history.replaceState({}, "", "/?view=settings&tab=model");

    let profiles = [
      {
        profile_id: PROFILE_ID,
        revision: 1,
        name: "DeepSeek 默认",
        provider_kind: "openai_compatible",
        base_url: "https://api.deepseek.com/v1",
        model: "deepseek-chat",
        has_api_key: true,
        is_active: false,
      },
    ];
    let sedEnabled = false;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
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
          projects: [{ scope_id: SCOPE, display_name: "默认项目" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url === "/api/v1/model-profiles" && (!init?.method || init.method === "GET")) {
        return jsonResponse(profiles);
      }
      if (url === "/api/v1/sedimentation") {
        if (init?.method === "PUT") {
          const body = JSON.parse(String(init.body)) as { enabled: boolean };
          sedEnabled = body.enabled;
        }
        return jsonResponse({ enabled: sedEnabled });
      }
      if (url.startsWith("/api/v1/model-probe-operations/")) {
        return jsonResponse({
          schema_version: 1,
          operation_id: url.split("/").pop(),
          profile_id: null,
          expected_profile_revision: null,
          state: "not_found",
          result: null,
          code: null,
          stale: null,
        });
      }
      if (url === `/api/v1/model-profiles/${PROFILE_ID}/probe`) {
        const body = JSON.parse(String(init?.body)) as { operation_id: string };
        return jsonResponse({
          schema_version: 1,
          operation_id: body.operation_id,
          profile_id: PROFILE_ID,
          expected_profile_revision: 1,
          state: "completed",
          code: null,
          stale: false,
          result: {
            profile_id: PROFILE_ID,
            profile_revision: 1,
            input_profile_revision: 1,
            capabilities: {
              endpoint_reachable: true,
              model_available: true,
              structured_output: true,
              embedding_available: true,
            },
            stages: [
              { stage: "endpoint", ok: true, code: "ok", latency_ms: 42 },
              { stage: "model", ok: true, code: "ok", latency_ms: 185 },
            ],
          },
        });
      }
      if (url === `/api/v1/model-profiles/${PROFILE_ID}/activate`) {
        profiles = profiles.map((p) => ({ ...p, is_active: true, revision: 2 }));
        return jsonResponse(profiles[0]);
      }
      if (url === "/api/v1/model-catalog") {
        return jsonResponse({
          code: "ok",
          has_more: false,
          models: [
            { id: "deepseek-chat", display_name: "DeepSeek V3" },
            { id: "deepseek-reasoner", display_name: "DeepSeek R1" },
          ],
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText("DeepSeek 默认")).toBeTruthy();

    // Toggle sedimentation switch on
    await user.click(screen.getByRole("switch", { name: "自动提炼长期记忆" }));
    expect(await screen.findByText(/已开启自动记忆整理/)).toBeTruthy();

    // Run connection probe
    await user.click(screen.getByRole("button", { name: /测试连接/ }));
    expect(
      await screen.findByText(/连接测试全部通过/),
    ).toBeTruthy();
    expect(screen.getByText("185 ms")).toBeTruthy();

    // Activate profile
    await user.click(screen.getByRole("button", { name: "设为启用" }));
    expect(await screen.findByText("当前启用")).toBeTruthy();

    // Open Add Modal and fetch catalog
    await user.click(screen.getByRole("button", { name: /添加模型配置/ }));
    await user.click(
      await screen.findByRole("button", { name: /从服务端拉取候选模型/ }),
    );
    expect(await screen.findByText(/已获取 2 个候选模型/)).toBeTruthy();
  });

  it("validates confirmation and submits admin credential rotation", async () => {
    const onChanged = vi.fn();

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
          projects: [{ scope_id: SCOPE, display_name: "默认项目" }],
          next_offset: null,
          global_scope: { scope_id: "global", display_name: "全局偏好" },
        });
      }
      if (url === "/hub/v1/admin/credential") {
        return jsonResponse({
          schema_version: 1,
          state: "credential_changed",
          login_required: true,
        });
      }
      return jsonResponse({}, 404);
    });

    const user = userEvent.setup();
    render(
      <HubProvider>
        <SecuritySettingsTab onCredentialChanged={onChanged} />
      </HubProvider>,
    );

    const curInput = await screen.findByLabelText("当前管理员密码");
    await user.type(curInput, "old-secret");
    await user.click(screen.getByRole("button", { name: /生成强密码/ }));
    await user.click(screen.getByRole("checkbox", { name: /我已妥善保存新管理员密码/ }));
    await user.click(
      screen.getByRole("button", { name: "保存密码并重新登录" }),
    );

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
  });
});
