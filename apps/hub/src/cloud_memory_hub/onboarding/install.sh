#!/bin/sh
# Only connects an Agent. Does not install the Hub, a model, or a desktop App.
set -eu
umask 077
cmh_origin=__CMH_ORIGIN_SHELL__
cmh_script_sha=__CMH_CONNECT_SHA256__
cmh_temp=$(mktemp -d "${TMPDIR:-/tmp}/cmh-connect.XXXXXXXX")
trap 'rm -rf -- "$cmh_temp"' EXIT HUP INT TERM
cmh_fail() { printf '%s\n' "$1" >&2; exit 1; }
cmh_fetch() {
  curl --fail --silent --location --max-redirs 3 \
    --proto '=http,https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 180 "$1" -o "$2" || \
    cmh_fail '下载连接组件失败。请检查网络后重新执行同一条命令。'
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
cmh_python=''
for cmh_candidate in python3.12 python3 python; do
  if command -v "$cmh_candidate" >/dev/null 2>&1 && \
    "$cmh_candidate" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    cmh_python=$cmh_candidate
    break
  fi
done
if [ -n "$cmh_python" ]; then
  "$cmh_python" -I "$cmh_temp/connect.py" "$@"
else
  # Verified uv only supplies a temporary Python; no shell profiles or PATH edits.
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) cmh_target=aarch64-apple-darwin; cmh_uv_sha=5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62 ;;
    Darwin-x86_64) cmh_target=x86_64-apple-darwin; cmh_uv_sha=b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8 ;;
    Linux-x86_64) cmh_target=x86_64-unknown-linux-gnu; cmh_uv_sha=68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2 ;;
    Linux-aarch64) cmh_target=aarch64-unknown-linux-gnu; cmh_uv_sha=9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31 ;;
    *) cmh_fail '尚未支持自动准备此系统的运行环境。请通过支持 HTTP MCP 的 Agent 添加连接。' ;;
  esac
  printf '%s\n' '正在准备一次性连接环境，无需管理员权限。'
  cmh_fetch "https://github.com/astral-sh/uv/releases/download/0.12.5/uv-$cmh_target.tar.gz" "$cmh_temp/uv.tar.gz"
  [ "$(cmh_sha "$cmh_temp/uv.tar.gz")" = "$cmh_uv_sha" ] || cmh_fail '运行环境校验失败，未执行安装。'
  tar -xzf "$cmh_temp/uv.tar.gz" -C "$cmh_temp" "uv-$cmh_target/uv"
  UV_CACHE_DIR="$cmh_temp/cache" UV_PYTHON_INSTALL_DIR="$cmh_temp/python" \
    "$cmh_temp/uv-$cmh_target/uv" run --no-project --no-config --no-env-file \
    --isolated --managed-python --python 3.12.13 --no-progress \
    python -I "$cmh_temp/connect.py" "$@"
fi
