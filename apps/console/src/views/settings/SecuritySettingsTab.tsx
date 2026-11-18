import { ArrowUpRight, Eye, EyeOff, KeyRound, ShieldCheck, Wand2 } from "lucide-react";
import { type FormEvent, useState } from "react";
import {
  CredentialChangeError,
  changeAdminCredential,
  credentialChangeMessage,
  generateAdminCredential,
} from "../../api/hub-admin-credential";
import { Button } from "../../components/ui";
import { useHub } from "../../context/HubContext";

export function SecuritySettingsTab({
  onCredentialChanged = () => window.location.assign("/hub/login"),
}: {
  onCredentialChanged?(): void;
}) {
  const { bootstrap } = useHub();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [visible, setVisible] = useState(false);
  const [saved, setSaved] = useState(false);
  const [pending, setPending] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  function updateNext(val: string) {
    setNext(val);
    setSaved(false);
    setMessage("");
  }

  function handleGenerate() {
    try {
      const generated = generateAdminCredential();
      updateNext(generated);
      setConfirmation(generated);
      setVisible(true);
      setError("");
      setMessage("已生成 64 位高熵安全凭证，请立即复制保存到密码管理器。");
    } catch {
      setError("当前浏览器环境不支持加密随机数生成，请手动输入新凭证。");
    }
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(next);
      setMessage("已复制新凭证至剪贴板，请妥善保存到密码管理器。");
    } catch {
      setVisible(true);
      setMessage("未能自动写入剪贴板，已展开明文供手动复制。");
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (pending || unknown) return;
    setError("");
    setMessage("");
    if (!current) {
      setError("请输入当前管理员密码。");
      return;
    }
    if (!next) {
      setError("请输入新管理员密码。");
      return;
    }
    if (next !== confirmation) {
      setError("两次输入的新密码不一致。");
      return;
    }
    if (!saved) {
      setError("请先备份新密码并勾选下方确认框。");
      return;
    }
    setPending(true);
    try {
      await changeAdminCredential(current, next);
      setCurrent("");
      setNext("");
      setConfirmation("");
      setVisible(false);
      setSaved(false);
      onCredentialChanged();
    } catch (caught) {
      setError(credentialChangeMessage(caught));
      if (caught instanceof CredentialChangeError && caught.outcomeUnknown) {
        setUnknown(true);
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="cmh-two-col">
      {/* Admin Password Rotation */}
      <section className="cmh-panel cmh-stack" aria-label="修改管理员凭证">
        <div className="cmh-panel__header">
          <div>
            <h2>修改管理员登录密码</h2>
            <p>
              更新成功后当前所有浏览器后台会话立即失效需重新登录；已配对的电脑与 Agent 设备连接不受影响。
            </p>
          </div>
          <KeyRound size={18} className="cmh-muted" />
        </div>

        <form
          className="cmh-stack"
          noValidate
          onSubmit={(e) => void handleSubmit(e)}
        >
          <div className="cmh-field">
            <label htmlFor="cmh-cur-pwd">当前管理员密码</label>
            <input
              id="cmh-cur-pwd"
              type="password"
              autoComplete="current-password"
              className="cmh-input"
              value={current}
              disabled={pending || unknown}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </div>

          <div className="cmh-field">
            <div className="cmh-field__row">
              <label htmlFor="cmh-new-pwd">新管理员密码</label>
              <div className="cmh-inline-actions">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={pending || unknown}
                  onClick={handleGenerate}
                >
                  <Wand2 size={13} /> 生成强密码
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!next || pending}
                  onClick={() => setVisible((v) => !v)}
                >
                  {visible ? <EyeOff size={13} /> : <Eye size={13} />}
                  {visible ? "隐藏" : "显示"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!next || pending}
                  onClick={() => void handleCopy()}
                >
                  复制新密码
                </Button>
              </div>
            </div>
            <input
              id="cmh-new-pwd"
              type={visible ? "text" : "password"}
              autoComplete="new-password"
              className="cmh-input cmh-mono"
              value={next}
              disabled={pending || unknown}
              onChange={(e) => updateNext(e.target.value)}
            />
          </div>

          <div className="cmh-field">
            <label htmlFor="cmh-confirm-pwd">再次输入新密码</label>
            <input
              id="cmh-confirm-pwd"
              type={visible ? "text" : "password"}
              autoComplete="new-password"
              className="cmh-input cmh-mono"
              value={confirmation}
              disabled={pending || unknown}
              onChange={(e) => setConfirmation(e.target.value)}
            />
          </div>

          <label className="cmh-checkbox">
            <input
              type="checkbox"
              checked={saved}
              disabled={pending || unknown}
              onChange={(e) => setSaved(e.target.checked)}
            />
            <span>我已妥善保存新管理员密码</span>
          </label>

          {message ? (
            <p role="status" className="cmh-alert cmh-alert--emerald">
              {message}
            </p>
          ) : null}
          {error ? (
            <p role="alert" className="cmh-alert cmh-alert--danger">
              {error}
            </p>
          ) : null}

          <div className="cmh-inline-actions">
            <Button
              type="submit"
              variant="primary"
              loading={pending}
              disabled={unknown}
            >
              保存密码并重新登录
            </Button>
            {unknown ? (
              <a className="cmh-btn cmh-btn--secondary" href="/hub/login">
                前往登录页验证
              </a>
            ) : null}
          </div>
        </form>
      </section>

      {/* Privacy & Instance Information */}
      <div className="cmh-stack">
        <section className="cmh-panel cmh-stack" aria-label="隐私保护与服务信息">
          <div className="cmh-panel__header">
            <div>
              <h2>隐私过滤与存储边界</h2>
              <p>数据控制权完全留在你部署的运行环境中</p>
            </div>
            <ShieldCheck size={18} className="cmh-muted" />
          </div>

          <ul className="cmh-bullet-list cmh-prose-sm">
            <li>
              <strong>入站与出站双重过滤：</strong>常见私钥、API Token、Cookie 以及当前 Hub 自身的管理员密码与加密密钥均会在接纳阶段拦截，避免落库或送入外部模型。
            </li>
            <li>
              <strong>离线重置密码：</strong>若遗忘后台密码，可在宿主机停止容器后执行{" "}
              <code className="cmh-mono">reset-admin-password --writers-stopped</code>{" "}
              重设，不影响任何已存记忆与设备配对。
            </li>
          </ul>

          <div className="cmh-inline-actions">
            <a
              className="cmh-link-inline"
              href="https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/data-and-privacy.md"
              target="_blank"
              rel="noreferrer"
            >
              《数据会去到哪里》 <ArrowUpRight size={14} />
            </a>
            <a
              className="cmh-link-inline"
              href="https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/development-runtime.md"
              target="_blank"
              rel="noreferrer"
            >
              《运维与密码重置指南》 <ArrowUpRight size={14} />
            </a>
          </div>
        </section>

        <section className="cmh-panel cmh-stack" aria-label="实例与构建版本">
          <h2>服务版本与实例标识</h2>
          <div className="cmh-meta-grid">
            <div>
              <span>产品版本</span>
              <strong className="cmh-mono">
                {bootstrap.version ?? "development"}
              </strong>
            </div>
            <div>
              <span>构建提交</span>
              <strong className="cmh-mono">
                {bootstrap.build_revision
                  ? bootstrap.build_revision.slice(0, 12)
                  : "未绑定"}
              </strong>
            </div>
            <div>
              <span>实例 UUID</span>
              <strong className="cmh-mono">{bootstrap.instance_id}</strong>
            </div>
            <div>
              <span>开源许可</span>
              <strong>Apache-2.0（核心复用 Hy-Memory / MemHub）</strong>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
