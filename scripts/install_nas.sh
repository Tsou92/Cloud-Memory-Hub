#!/usr/bin/env bash
# Stream over SSH. stdout is reserved for the single completion record.
set -euo pipefail
umask 077
exec 3>&1
exec 1>&2

fail() {
    printf 'NAS 安装未完成：%s\n' "$1"
    exit "${2:-1}"
}

valid_port() {
    [[ $1 =~ ^[1-9][0-9]{0,4}$ ]] && (( $1 >= 1024 && $1 <= 65535 ))
}

[[ $# -ge 5 && $# -le 8 ]] || fail '安装参数不完整。'
release_tag=$1
install_path=$2
compose_project=$3
hub_port=$4
requested_origin=$5
serve_port=${6:-8443}
tailscale_container=${7:-}
release_stage=${8:-}
[[ $release_tag =~ ^v[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}$ ]] || fail '请选择稳定发行版。'
[[ $compose_project =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] || fail 'Compose 项目名称无效。'
valid_port "$hub_port" || fail 'NAS 回环端口必须是 1024 至 65535 的整数。'
[[ $serve_port == 443 ]] || valid_port "$serve_port" || fail 'Tailscale HTTPS 端口必须是 443 或 1024 至 65535 的整数。'
[[ $install_path =~ ^[a-zA-Z0-9_./[:space:]-]+$ && ! $install_path =~ [[:cntrl:]] ]] \
    || fail '安装目录格式无效。'
[[ $install_path != */ && /$install_path/ != */../* && /$install_path/ != */./* ]] \
    || fail '请指定一个新目录，不使用相对跳转。'
[[ $requested_origin == AUTO || $requested_origin =~ ^https://[a-zA-Z0-9.-]+(:[1-9][0-9]{0,4})?$ ]] \
    || fail '入口须是 HTTPS Origin，不包含路径或凭证。'
[[ -z $tailscale_container || $tailscale_container =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]] \
    || fail 'Tailscale 容器名称无效。'
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail '当前发行镜像支持 Linux amd64 NAS。'
for required in curl sha256sum tar flock python3 docker; do
    command -v "$required" >/dev/null || fail "NAS 缺少 $required。"
done

# Use the NAS Docker daemon and the files written by this install only.
unset DOCKER_CONTEXT DOCKER_HOST COMPOSE_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES
unset COMPOSE_REMOVE_ORPHANS CMH_IMAGE CMH_PUBLIC_ORIGIN CMH_PORT CMH_BACKUP_PATH
cmh_docker=(docker --context default)
docker_access=docker
if ! "${cmh_docker[@]}" info >/dev/null 2>&1; then
    cmh_docker=(sudo -n docker --context default)
    docker_access=sudo
    "${cmh_docker[@]}" info >/dev/null 2>&1 || fail 'SSH 用户需要已经获准使用 NAS 的 Docker。'
fi
"${cmh_docker[@]}" compose version >/dev/null 2>&1 || fail '请先启用 Docker Compose v2。'

tailscale_image='(docker\.io/|ghcr\.io/)?tailscale/tailscale([:@][^[:space:]]+)?'
if [[ $requested_origin == AUTO && -z $tailscale_container ]] && ! command -v tailscale >/dev/null; then
    running=$("${cmh_docker[@]}" ps --filter network=host --format '{{.Names}} {{.Image}}') \
        || fail '无法检查 NAS 上的 Tailscale 容器。'
    while read -r candidate image_name; do
        if [[ $candidate =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ && $image_name =~ ^$tailscale_image$ ]]; then
            [[ -z $tailscale_container ]] || fail '发现多个 Tailscale 容器，请指定要使用的容器。'
            tailscale_container=$candidate
        fi
    done <<< "$running"
    [[ -n $tailscale_container ]] || fail 'NAS 未找到可用的 Tailscale。请先登录 Tailscale，或指定已有 HTTPS 入口。'
    printf '已找到 NAS 上的 Tailscale 容器。\n'
fi

if [[ $install_path != /* ]]; then
    install_path=$HOME/$install_path
fi
install_parent=$(cd -P -- "$(dirname -- "$install_path")" 2>/dev/null && pwd) \
    || fail '安装目录的上一级必须已存在且可访问。'
install_dir=${install_parent%/}/$(basename -- "$install_path")
[[ ! $install_dir =~ [[:cntrl:]] ]] || fail '安装目录格式无效。'
if [[ -n $release_stage ]]; then
    [[ $release_stage == /* && ! $release_stage =~ [[:cntrl:]] && -d $release_stage && ! -L $release_stage &&
       $(stat -c '%u:%a' -- "$release_stage") == "$(id -u):700" ]] \
        || fail '传入的发行文件目录必须是此 SSH 用户所有的私有目录。'
fi

# Serialize fresh installs by this SSH account, including different paths that
# target the same Compose project or Tailscale listener.
lock_dir=/tmp/cmh-first-install-$(id -u)
mkdir -m 700 -- "$lock_dir" 2>/dev/null || true
[[ ! -L $lock_dir && $(stat -c '%u:%a' -- "$lock_dir") == "$(id -u):700" ]] \
    || fail '安装锁目录不安全。'
exec 9<"$lock_dir"
flock -n 9 || fail '此 NAS 用户正在执行另一个安装，稍后重试。'

intent="$install_dir/.cmh-install-intent"
intent_value=$(printf '%s\n' 'cmh-first-install-v1' "$release_tag" "$compose_project" "$hub_port" \
    "$requested_origin" "$serve_port" "$tailscale_container")
label=io.cloud-memory-hub.install-id

read_resources() {
    local kind=$1 format=$2
    case "$kind" in
        container) "${cmh_docker[@]}" ps -aq --filter "label=com.docker.compose.project=$compose_project" ;;
        *) "${cmh_docker[@]}" "$kind" ls --format "$format" \
            --filter "label=com.docker.compose.project=$compose_project" ;;
    esac
}

verify_project_resources() {
    local owner=${1:-} kind format objects object found
    for kind in container volume network; do
        format='{{.Name}}'
        objects=$(read_resources "$kind" "$format") || fail '无法读取 Compose 项目资源。'
        # Include conventional names in case an unmanaged resource has no Compose labels.
        if [[ $kind == container ]]; then
            found=$("${cmh_docker[@]}" ps -a --format '{{.Names}}') || fail '无法读取容器名称。'
            if grep -Fxq -- "$compose_project-hub-1" <<< "$found"; then
                objects+=$'\n'"$compose_project-hub-1"
            fi
        else
            found=$("${cmh_docker[@]}" "$kind" ls --format "$format") || fail '无法读取 Compose 资源名称。'
            if [[ $kind == volume ]]; then
                for object in hub_data hub_config hub_governance hub_backups; do
                    if grep -Fxq -- "${compose_project}_$object" <<< "$found"; then
                        objects+=$'\n'"${compose_project}_$object"
                    fi
                done
            elif grep -Fxq -- "${compose_project}_default" <<< "$found"; then
                objects+=$'\n'"${compose_project}_default"
            fi
        fi
        for object in $objects; do
            [[ -n $owner ]] || fail 'Compose 名称已被使用。已有实例请走升级流程，或为新实例选另一个名称。'
            if [[ $kind == container ]]; then
                format="{{index .Config.Labels \"$label\"}}"
            else
                format="{{index .Labels \"$label\"}}"
            fi
            found=$("${cmh_docker[@]}" "$kind" inspect --format "$format" "$object") \
                || fail '无法核对已有资源的安装归属。'
            [[ $found == "$owner" ]] || fail '发现同名且不属于本次安装的资源，已保留现场。'
        done
    done
}

if [[ -e $install_dir || -L $install_dir ]]; then
    [[ ! -L $install_dir && $(stat -c '%u:%a' -- "$install_dir") == "$(id -u):700" &&
       -f $intent && ! -L $intent && -f $install_dir/.cmh-install-id && ! -L $install_dir/.cmh-install-id ]] \
        || fail '目录已存在且不属于此安装器。请选择新目录，已有实例使用升级流程。'
    [[ $(cat -- "$intent") == "$intent_value" ]] || fail '目录内已有不同参数的安装。请使用原命令续跑。'
    install_id=$(cat -- "$install_dir/.cmh-install-id")
    [[ $install_id =~ ^[0-9a-f]{32}$ ]] || fail '安装状态文件无效。'
    verify_project_resources "$install_id"
else
    verify_project_resources
    pending_dir=$(mktemp -d "$install_parent/.cmh-install-intent.XXXXXXXX") || fail '无法创建安装目录。'
    trap 'rm -rf -- "$pending_dir"' EXIT
    chmod 700 -- "$pending_dir" || fail '无法设置安装目录权限。'
    install_id=$(python3 -I -c 'import secrets; print(secrets.token_hex(16))')
    printf '%s\n' "$install_id" > "$pending_dir/.cmh-install-id"
    printf '%s\n' "$intent_value" > "$pending_dir/.cmh-install-intent"
    chmod 600 -- "$pending_dir/.cmh-install-id" "$pending_dir/.cmh-install-intent"
    mv -T -n -- "$pending_dir" "$install_dir" || fail '无法发布安装目录。'
    [[ ! -e $pending_dir ]] || fail '安装目录已被其他进程创建。'
    trap - EXIT
fi

workspace=$(mktemp -d "$install_dir/.download.XXXXXXXX") || fail '无法创建下载目录。'
cleanup() { rm -rf -- "$workspace"; }
trap cleanup EXIT
chmod 700 -- "$workspace" || fail '无法设置下载目录权限。'

# The named Tailscale container must be running on the NAS host network so
# its loopback actually reaches Compose's intentionally private port.
tailscale_cmd=()
if [[ $requested_origin == AUTO ]]; then
    [[ $serve_port != "$hub_port" ]] || fail 'Tailscale HTTPS 端口必须与容器回环端口不同。'
    if [[ -n $tailscale_container ]]; then
        expected_container=$("${cmh_docker[@]}" container inspect --format \
            '{{.Config.Image}} {{.HostConfig.NetworkMode}} {{.State.Running}}' "$tailscale_container" 2>/dev/null) \
            || fail '找不到所选 Tailscale 容器。'
        [[ $expected_container =~ ^$tailscale_image\ host\ true$ ]] \
            || fail '请选择运行中的官方 Tailscale host 网络容器。'
        tailscale_cmd=("${cmh_docker[@]}" exec "$tailscale_container" tailscale)
    else
        command -v tailscale >/dev/null || fail 'NAS 没有可用的 Tailscale。可指定已运行的容器或已有 HTTPS 入口。'
        tailscale_cmd=(tailscale)
    fi
    tailscale_host=$("${tailscale_cmd[@]}" status --json 2>/dev/null | python3 -I -c '
import json, re, sys
status = json.load(sys.stdin)
name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
if status.get("BackendState") != "Running" or not re.fullmatch(r"[a-z0-9.-]+\.ts\.net", name):
    sys.exit(1)
print(name)
' 2>/dev/null) || fail '请先在 NAS 上登录 Tailscale 并启用设备的 MagicDNS。'
    public_origin=https://$tailscale_host
    [[ $serve_port == 443 ]] || public_origin+=:$serve_port
else
    public_origin=$requested_origin
    python3 -I -c '
from urllib.parse import urlsplit
import sys
url = urlsplit(sys.argv[1])
assert url.hostname and (url.port is None or 1 <= url.port <= 65535)
' "$public_origin" 2>/dev/null || fail 'HTTPS 入口的主机或端口无效。'
fi

serve_state() {
    "${tailscale_cmd[@]}" serve status --json 2>/dev/null | python3 -I -c '
import json, sys
port, hostname, target = sys.argv[1:]
config = json.load(sys.stdin)
if config is None:
    config = {}
if not isinstance(config, dict):
    sys.exit(1)

def port_entries(document, key):
    return {k: v for k, v in (document.get(key) or {}).items() if k.rsplit(":", 1)[-1] == port}

def occupied(document):
    return any(port_entries(document, key) for key in ("TCP", "Web", "AllowFunnel")) or any(
        occupied(child) for child in (document.get("Foreground") or {}).values()
    )

# Foreground owners and Funnel are never touched. Only an exact private root
# proxy to this instance is an eligible existing binding.
if port_entries(config, "AllowFunnel") or any(occupied(child) for child in (config.get("Foreground") or {}).values()):
    print("busy")
elif not occupied(config):
    print("free")
elif (port_entries(config, "TCP") == {port: {"HTTPS": True}} and
      port_entries(config, "Web") == {hostname + ":" + port: {"Handlers": {"/": {"Proxy": target}}}}):
    print("matching")
else:
    print("busy")
' "$serve_port" "$tailscale_host" "http://127.0.0.1:$hub_port" 2>/dev/null
}

if [[ $requested_origin == AUTO ]]; then
    current_serve=$(serve_state) || fail '无法安全核对现有 Tailscale Serve 绑定。'
    if [[ $current_serve == busy || ( $current_serve == matching && ! -f $install_dir/.cmh-serve-intent ) ]]; then
        fail '该 Tailscale HTTPS 端口已有绑定，请使用另一个端口。'
    fi
fi

# If no installer-owned Hub is already running, test the loopback bind before
# importing the image. Docker itself remains the final authority on the port.
if [[ -z $("${cmh_docker[@]}" ps -q --filter "label=$label=$install_id" \
    --filter 'label=com.docker.compose.service=hub') ]]; then
    python3 -I -c '
import socket, sys
with socket.socket() as sock:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
' "$hub_port" 2>/dev/null || fail 'NAS 回环端口已被占用，请选择另一个端口。'
fi

release_base=https://github.com/Tsou92/Cloud-Memory-Hub/releases/download/$release_tag
image_file=cloud-memory-hub-$release_tag-linux-amd64.tar.gz
source_file=cloud-memory-hub-$release_tag-source.tar.gz
image_ref=cloud-memory-hub:$release_tag

fetch_release() {
    local file=$1 route deadline=60
    local -a route_options
    [[ $file != "$image_file" ]] || deadline=1200
    if [[ -n $release_stage ]]; then
        local staged_file=$release_stage/$file
        [[ -f $staged_file && ! -L $staged_file &&
           $(stat -c '%u:%a:%h' -- "$staged_file") == "$(id -u):600:1" ]] \
            || fail "传入的 $file 不是此 SSH 用户所有的私有文件。"
        cp -- "$staged_file" "$workspace/$file" || fail "无法读取传入的 $file。"
        return
    fi
    # curl's default retry does not cover an SSL connection reset. Try the
    # same official URL again without switching off certificate verification.
    if [[ $file == "$image_file" ]]; then
        printf '正在下载 NAS 镜像 %s，传输可能需要几分钟。\n' "$file"
    fi
    for route in environment direct environment direct; do
        route_options=(--http1.1)
        [[ $route != direct ]] || route_options+=(--noproxy '*')
        if curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 12 \
            --max-time "$deadline" --speed-limit 4096 --speed-time 30 "${route_options[@]}" \
            --output "$workspace/$file" "$release_base/$file" 2>/dev/null; then
            return
        fi
        sleep 1
    done
    fail "NAS 无法从 GitHub 下载 $file，将尝试由电脑下载并传输。" 42
}

verify_file() {
    local file=$1 expected actual
    expected=$(awk -v file="$file" '$2 == file && $1 ~ /^[a-f0-9]+$/ && length($1) == 64 {print $1}' \
        "$workspace/SHA256SUMS")
    [[ $expected =~ ^[a-f0-9]{64}$ ]] || fail "校验清单缺少唯一的 $file。"
    actual=$(sha256sum < "$workspace/$file") || fail "无法计算 $file 的校验值。"
    [[ ${actual%% *} == "$expected" ]] || fail "$file 校验不匹配。"
}

printf '正在核对官方发行包 %s。\n' "$release_tag"
fetch_release SHA256SUMS
fetch_release release.json
verify_file release.json
receipt=$(python3 -I -c '
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as source:
    metadata = json.load(source)
tag, ref, filename = sys.argv[2:]
assert metadata["schema_version"] == 1 and metadata["tag"] == tag and metadata["version"] == tag[1:]
image = metadata["image"]
commit = metadata["commit"]
assert re.fullmatch(r"[a-f0-9]{40}", commit)
assert image["commit"] == commit and image["tag"] == tag
assert image["image_ref"] == ref and image["platform"] == "linux/amd64"
assert image["artifact"]["filename"] == filename
assert re.fullmatch(r"sha256:[a-f0-9]{64}", image["config_digest"])
assert re.fullmatch(r"sha256:[a-f0-9]{64}", image["image_id"])
print(image["config_digest"], image["image_id"], commit)
' "$workspace/release.json" "$release_tag" "$image_ref" "$image_file" 2>/dev/null) \
    || fail '官方发行元数据无效。'
read -r image_config_digest release_image_id release_commit <<< "$receipt"

fetch_release "$source_file"
verify_file "$source_file"
tar -xOzf "$workspace/$source_file" "cloud-memory-hub-$release_tag/compose.yaml" \
    > "$workspace/compose.yaml" 2>/dev/null || fail '无法读取发行包中的 Compose 配置。'
[[ -s $workspace/compose.yaml ]] || fail '发行包中的 Compose 配置为空。'

inspect_image() {
    "${cmh_docker[@]}" image inspect --format \
        '{{.Id}} {{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_ref" 2>/dev/null
}

verify_image() {
    local inspection actual_id platform revision
    inspection=$(inspect_image) || fail '无法读取已导入镜像。'
    read -r actual_id platform revision <<< "$inspection"
    [[ $platform == linux/amd64 && $revision == "$release_commit" &&
       ( $actual_id == "$image_config_digest" || $actual_id == "$release_image_id" ) ]] \
        || fail 'NAS 上的同名镜像与官方发行不符，请保留原实例并处理冲突。'
}

local_image=$("${cmh_docker[@]}" image ls --no-trunc --quiet "$image_ref") || fail '无法读取 Docker 镜像目录。'
if [[ -z $local_image ]]; then
    fetch_release "$image_file"
    verify_file "$image_file"
    # Check again after downloading before docker load can update a tag.
    local_image=$("${cmh_docker[@]}" image ls --no-trunc --quiet "$image_ref") || fail '无法重新核对 Docker 镜像目录。'
    if [[ -z $local_image ]]; then
        printf '正在导入已校验的 NAS 镜像。\n'
        "${cmh_docker[@]}" image load --input "$workspace/$image_file" >/dev/null 2>&1 \
            || fail 'Docker 未能导入已校验镜像。'
    fi
fi
verify_image

cat > "$workspace/compose.override.yaml" <<YAML
services:
  setup:
    labels: &install_labels
      $label: "$install_id"
  hub:
    labels: *install_labels
volumes:
  hub_data:
    labels: *install_labels
  hub_config:
    labels: *install_labels
  hub_governance:
    labels: *install_labels
  hub_backups:
    labels: *install_labels
networks:
  default:
    labels: *install_labels
YAML
printf 'CMH_IMAGE=%s\nCMH_PORT=%s\nCMH_PUBLIC_ORIGIN=%s\n' "$image_ref" "$hub_port" "$public_origin" > "$workspace/.env"

for file in compose.yaml compose.override.yaml .env; do
    if [[ -e $install_dir/$file || -L $install_dir/$file ]]; then
        [[ -f $install_dir/$file && ! -L $install_dir/$file ]] && \
            cmp -s -- "$workspace/$file" "$install_dir/$file" \
            || fail "已有 $file 已变更，请保留配置并按维护流程处理。"
    else
        mv -- "$workspace/$file" "$install_dir/$file"
    fi
done

compose=("${cmh_docker[@]}" compose --project-directory "$install_dir" --env-file "$install_dir/.env" \
    -f "$install_dir/compose.yaml" -f "$install_dir/compose.override.yaml" -p "$compose_project")
"${compose[@]}" config --quiet >/dev/null 2>&1 || fail 'Docker Compose 不接受当前发行配置。'
verify_project_resources "$install_id"

if [[ ! -f $install_dir/.cmh-initialized ]]; then
    if ! "${compose[@]}" run --rm --no-deps -T --pull never setup </dev/null >/dev/null 2>&1; then
        # Initialization may have succeeded before an SSH disconnect. Treat a
        # complete pre-existing identity as a resumable state only after the
        # actual server starts and responds. Partial state still fails closed.
        "${compose[@]}" run --rm --no-deps -T --pull never \
            --entrypoint /opt/cmh/venv/bin/python setup -c \
            'from cloud_memory_hub.config import HubConfig; HubConfig.from_environment()' \
            </dev/null >/dev/null 2>&1 || fail '初始化未完成。保留卷以供检查，重试同一命令不会清除凭证。'
    fi
fi

printf '正在启动并检查 NAS 服务。\n'
"${compose[@]}" up -d --no-build --no-recreate --no-deps --pull never hub </dev/null >/dev/null 2>&1 \
    || fail '容器无法启动。请检查 NAS 资源和回环端口。'
ready=no
for (( attempt=0; attempt<60; attempt++ )); do
    if curl -fsS --noproxy '*' --connect-timeout 2 --max-time 3 "http://127.0.0.1:$hub_port/healthz" \
        >/dev/null 2>&1; then
        hub_container=$("${compose[@]}" ps -q hub) || fail '无法确认 NAS 容器身份。'
        if [[ -n $hub_container && $("${cmh_docker[@]}" container inspect --format '{{.State.Health.Status}}' \
                "$hub_container" 2>/dev/null || true) == healthy ]]; then
            ready=yes
            break
        fi
    fi
    sleep 2
done
[[ $ready == yes ]] || fail 'NAS 容器还未通过健康检查。可重试同一命令。'
printf '%s\n' "$release_commit" > "$install_dir/.cmh-initialized"

if [[ $requested_origin == AUTO ]]; then
    current_serve=$(serve_state) || fail '无法重新核对 Tailscale Serve 绑定。'
    case "$current_serve" in
        free)
            printf '%s\n' "$public_origin" > "$install_dir/.cmh-serve-intent"
            if ! serve_output=$("${tailscale_cmd[@]}" serve --bg --https="$serve_port" \
                "http://127.0.0.1:$hub_port" </dev/null 2>&1); then
                authorization=$(grep -Eo 'https://login\.tailscale\.com/f/serve\?[a-zA-Z0-9_=&%.-]+' <<< "$serve_output" | head -n 1 || true)
                if [[ -n $authorization ]]; then
                    printf '请在浏览器完成 Tailscale 对此 NAS 的 HTTPS 授权，再重试原命令。\n%s\n' "$authorization"
                fi
                fail 'Tailscale HTTPS Serve 尚未生效。请核对账户授权或 NAS 运行权限。'
            fi
            ;;
        matching)
            [[ -f $install_dir/.cmh-serve-intent && $(cat -- "$install_dir/.cmh-serve-intent") == "$public_origin" ]] \
                || fail '该 Tailscale HTTPS 绑定不属于此次安装。'
            ;;
        *) fail 'Tailscale HTTPS 端口已被其他服务使用。' ;;
    esac
    [[ $(serve_state) == matching ]] || fail 'Tailscale 未返回本次安装的专属私网 HTTPS 绑定。'
fi

printf 'CMH_NAS_READY\t%s\t%s\t%s\t%s\n' "$public_origin" "$install_dir" "$compose_project" "$docker_access" >&3
