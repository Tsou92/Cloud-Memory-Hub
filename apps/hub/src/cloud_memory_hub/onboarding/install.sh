#!/bin/sh
# Only connects an Agent. Does not install the Hub, a model, or a desktop App.
set -eu
umask 077
cmh_origin=__CMH_ORIGIN_SHELL__
cmh_script_sha=__CMH_CONNECT_SHA256__
cmh_client_release=__CMH_CLIENT_RELEASE_TAG_SHELL__
cmh_temp=$(mktemp -d "${TMPDIR:-/tmp}/cmh-connect.XXXXXXXX")
trap 'rm -rf -- "$cmh_temp"' EXIT HUP INT TERM
cmh_fail() { printf '%s\n' "$1" >&2; exit 1; }
cmh_fetch() {
  curl -q --fail --silent --location --max-redirs 3 \
    --proto '=http,https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 180 "$1" -o "$2"
}
cmh_sha() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d ' ' -f 1
  else shasum -a 256 "$1" | cut -d ' ' -f 1; fi
}
__CMH_HUB_SHELL_FETCH__
command -v curl >/dev/null 2>&1 || cmh_fail '系统缺少 curl，暂时无法下载连接组件。'
cmh_fetch_hub "$cmh_origin/hub/connect.py" "$cmh_temp/connect.py"
[ "$(cmh_sha "$cmh_temp/connect.py")" = "$cmh_script_sha" ] || \
  cmh_fail '连接组件校验失败。服务可能正在升级，请刷新连接页面再试。'
cmh_platform="$(uname -s)-$(uname -m)"
cmh_agent=''
cmh_read_agent=0
cmh_has_workspace=0
for cmh_arg in "$@"; do
  if [ "$cmh_read_agent" -eq 1 ]; then cmh_agent=$cmh_arg; cmh_read_agent=0; fi
  case "$cmh_arg" in
    --agent) cmh_read_agent=1 ;;
    --agent=*) cmh_agent=${cmh_arg#*=} ;;
    --workspace|--workspace=*|--managed-workspace) cmh_has_workspace=1 ;;
  esac
done
cmh_auto=0
cmh_minimum_check='import sys; raise SystemExit(sys.version_info < (3, 11))'
cmh_version_check=$cmh_minimum_check
case "$cmh_platform:$cmh_agent:$cmh_has_workspace:$cmh_client_release" in
  Darwin-arm64:claude:1:v[0-9]*|Darwin-arm64:codex:1:v[0-9]*)
    cmh_auto=1
    cmh_version_check='import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'
    ;;
esac
cmh_python=''
cmh_fallback_python=''
for cmh_candidate in python3.12 python3 python; do
  command -v "$cmh_candidate" >/dev/null 2>&1 || continue
  if "$cmh_candidate" -I -c "$cmh_version_check" >/dev/null 2>&1; then
    cmh_python=$cmh_candidate
    break
  fi
  if [ "$cmh_auto" -eq 1 ] && [ -z "$cmh_fallback_python" ] && \
    "$cmh_candidate" -I -c "$cmh_minimum_check" >/dev/null 2>&1; then
    cmh_fallback_python=$cmh_candidate
  fi
done
cmh_connect_with_existing_python() {
  [ -n "$cmh_fallback_python" ] || cmh_fail '运行环境下载或校验失败，请检查网络后重试。'
  printf '%s\n' '自动组件运行环境暂不可用，继续配置 MCP 连接。' >&2
  "$cmh_fallback_python" -I "$cmh_temp/connect.py" "$@"
}
if [ -n "$cmh_python" ]; then
  "$cmh_python" -I "$cmh_temp/connect.py" "$@"
else
  # The verified runtime is private. Hooks need a base Python that survives pairing.
  case "$cmh_platform" in
    Darwin-arm64) cmh_target=aarch64-apple-darwin; cmh_uv_sha=5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62 ;;
    Darwin-x86_64) cmh_target=x86_64-apple-darwin; cmh_uv_sha=b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8 ;;
    Linux-x86_64) cmh_target=x86_64-unknown-linux-gnu; cmh_uv_sha=68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2 ;;
    Linux-aarch64) cmh_target=aarch64-unknown-linux-gnu; cmh_uv_sha=9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31 ;;
    *) cmh_fail '尚未支持自动准备此系统的运行环境。请通过支持 HTTP MCP 的 Agent 添加连接。' ;;
  esac
  printf '%s\n' '正在准备一次性连接环境，无需管理员权限。'
  if ! cmh_fetch "https://github.com/astral-sh/uv/releases/download/0.12.5/uv-$cmh_target.tar.gz" "$cmh_temp/uv.tar.gz" || \
    [ "$(cmh_sha "$cmh_temp/uv.tar.gz")" != "$cmh_uv_sha" ]; then
    cmh_connect_with_existing_python "$@"
    exit $?
  fi
  tar -xzf "$cmh_temp/uv.tar.gz" -C "$cmh_temp" "uv-$cmh_target/uv"
  cmh_python_home="$cmh_temp/python"
  if [ "$cmh_auto" -eq 1 ] && [ -n "${HOME:-}" ]; then
    cmh_private_root="$HOME/Library/Application Support/CloudMemoryHub"
    cmh_safe=1
    cmh_uid=$(id -u)
    for cmh_part in "$HOME" "$HOME/Library" "$HOME/Library/Application Support" \
      "$cmh_private_root" "$cmh_private_root/python"; do
      if [ -L "$cmh_part" ]; then cmh_safe=0; break; fi
      if [ -e "$cmh_part" ]; then
        if [ ! -d "$cmh_part" ]; then cmh_safe=0; break; fi
        if ! cmh_mode=$(stat -f '%u:%Lp' "$cmh_part"); then cmh_safe=0; break; fi
        if [ "${cmh_mode%%:*}" != "$cmh_uid" ]; then cmh_safe=0; break; fi
        case "${cmh_mode#*:}" in *[2367][0-7]|*[2367]) cmh_safe=0; break ;; esac
      fi
    done
    if [ "$cmh_safe" -eq 1 ] && mkdir -p "$cmh_private_root/python" && \
      [ "$(stat -f '%u:%Lp' "$cmh_private_root/python")" = "$cmh_uid:700" ]; then
      cmh_python_home="$cmh_private_root/python"
    fi
  fi
  if [ -n "$cmh_fallback_python" ] && ! UV_CACHE_DIR="$cmh_temp/cache" \
    UV_PYTHON_INSTALL_DIR="$cmh_python_home" "$cmh_temp/uv-$cmh_target/uv" run \
    --no-project --no-config --no-env-file --isolated --managed-python --python 3.12.13 \
    --no-progress python -I -c 'pass' >/dev/null 2>&1; then
    cmh_connect_with_existing_python "$@"
    exit $?
  fi
  UV_CACHE_DIR="$cmh_temp/cache" UV_PYTHON_INSTALL_DIR="$cmh_python_home" \
    "$cmh_temp/uv-$cmh_target/uv" run --no-project --no-config --no-env-file \
    --isolated --managed-python --python 3.12.13 --no-progress \
    python -I "$cmh_temp/connect.py" "$@"
fi
