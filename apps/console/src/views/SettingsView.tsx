import { Coins, Cpu, HardDrive, ShieldCheck } from "lucide-react";
import type { SettingsTab } from "../router";
import { BackupsSettingsTab } from "./settings/BackupsSettingsTab";
import { ModelSettingsTab } from "./settings/ModelSettingsTab";
import { SecuritySettingsTab } from "./settings/SecuritySettingsTab";
import { UsageSettingsTab } from "./settings/UsageSettingsTab";

const TABS: {
  id: SettingsTab;
  label: string;
  Icon: typeof Cpu;
}[] = [
  { id: "model", label: "记忆整理模型", Icon: Cpu },
  { id: "usage", label: "用量与费用估算", Icon: Coins },
  { id: "backups", label: "备份与恢复", Icon: HardDrive },
  { id: "security", label: "账户与安全", Icon: ShieldCheck },
];

export function SettingsView({
  tab,
  onSelectTab,
}: {
  tab: SettingsTab;
  onSelectTab(next: SettingsTab): void;
}) {
  return (
    <div className="cmh-page">
      <header className="cmh-page-header">
        <div>
          <h1>系统设置</h1>
          <p>管理 Hub 全局生效的记忆提炼模型、调用开销统计、NAS 受控备份策略与管理员凭证。</p>
        </div>
      </header>

      <nav className="cmh-tabs cmh-tabs--bar" aria-label="系统设置分组">
        {TABS.map(({ id, label, Icon }) => {
          const active = tab === id;
          return (
            <button
              key={id}
              type="button"
              aria-current={active ? "page" : undefined}
              className={active ? "is-active" : ""}
              onClick={() => onSelectTab(id)}
            >
              <Icon size={15} />
              <span>{label}</span>
            </button>
          );
        })}
      </nav>

      <div className="cmh-settings-stage">
        {tab === "model" ? <ModelSettingsTab /> : null}
        {tab === "usage" ? <UsageSettingsTab /> : null}
        {tab === "backups" ? <BackupsSettingsTab /> : null}
        {tab === "security" ? <SecuritySettingsTab /> : null}
      </div>
    </div>
  );
}
