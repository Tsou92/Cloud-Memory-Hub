"""Trusted static assets for the first-use administrator form."""

import base64
import hashlib

SETUP_FORM = """<p>初始密码是 <code>admin</code>。请先设置新的管理员密码，再进入后台。</p>
<p id="setup-message" role="alert" hidden></p>
<form id="setup-form" method="post" action="/hub/setup">
<label for="initial-password">初始密码</label>
<input id="initial-password" type="password" autocomplete="current-password" required>
<label for="new-password">新密码</label>
<input id="new-password" type="password" autocomplete="new-password" required>
<label for="confirm-password">再次输入新密码</label>
<input id="confirm-password" type="password" autocomplete="new-password" required>
<button id="generate-password" type="button" hidden>生成并显示强密码</button>
<button id="finish-setup" type="submit" disabled>保存新密码并进入后台</button>
</form>
<p><a href="/hub/login">返回登录</a></p>
<noscript><p>启用 JavaScript 后，重新打开安装器提供的初始化链接。</p></noscript>"""

SETUP_SCRIPT = """(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  const proofs = params.getAll('proof');
  const proof = proofs.length === 1 ? proofs[0] : '';
  if (location.hash || location.search) history.replaceState(null, '', location.pathname);
  const form = document.getElementById('setup-form');
  const message = document.getElementById('setup-message');
  const initial = document.getElementById('initial-password');
  const next = document.getElementById('new-password');
  const confirmation = document.getElementById('confirm-password');
  const submit = document.getElementById('finish-setup');
  const generate = document.getElementById('generate-password');
  const show = (text) => {
    message.textContent = text;
    message.hidden = !text;
  };
  if (!/^[0-9a-f]{64}$/.test(proof)) {
    form.hidden = true;
    show('请从安装器提供的初始化链接打开此页。');
    return;
  }
  submit.disabled = false;
  if (window.crypto && window.crypto.getRandomValues) {
    generate.hidden = false;
    generate.addEventListener('click', () => {
      const bytes = crypto.getRandomValues(new Uint8Array(24));
      const generated = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
      next.type = confirmation.type = 'text';
      next.value = confirmation.value = generated;
      next.select();
      show('请先保存生成的新密码，再点保存。');
    });
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    if (next.value !== confirmation.value) {
      show('两次输入的新密码不同，请重新确认。');
      confirmation.focus();
      return;
    }
    submit.disabled = true;
    show('');
    try {
      const response = await fetch('/hub/setup', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          password: initial.value, proof, new_credential: next.value,
          confirm_credential: confirmation.value,
        }),
      });
      if (response.ok) {
        form.reset();
        location.replace('/');
        return;
      }
      const {detail} = await response.json();
      const errors = {
        invalid_initial_credential: '初始密码或初始化链接无效，请核对。',
        initial_setup_not_pending: '此初始化链接已经失效，请返回登录页。',
        invalid_new_credential: '请设置不同于初始密码的新密码，并确认两次输入一致。',
        login_rate_limited: '尝试次数较多，请稍后再试。',
      };
      show(Object.hasOwn(errors, detail) ? errors[detail] : '暂时无法保存，请稍后再试。');
    } catch (_) {
      show('暂时无法连接后台。若新密码已经保存，请返回登录页。');
    } finally {
      submit.disabled = false;
    }
  });
})();"""

SETUP_SCRIPT_HASH = base64.b64encode(hashlib.sha256(SETUP_SCRIPT.encode()).digest()).decode("ascii")
