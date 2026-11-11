#!/bin/sh
# Fetch the fixed official release, then hand off from the caller's directory.
set -eu
umask 077
unset TAR_OPTIONS GZIP

cmh_tag=v0.1.6
cmh_prefix="cloud-memory-hub-$cmh_tag"
cmh_archive="$cmh_prefix-source.tar.gz"
cmh_release="https://github.com/Tsou92/Cloud-Memory-Hub/releases/download/$cmh_tag"

cmh_fail() { printf '%s\n' "$1" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || cmh_fail '安装需要 curl。'
command -v tar >/dev/null 2>&1 || cmh_fail '安装需要 tar。'
if command -v sha256sum >/dev/null 2>&1; then
    cmh_sha() { sha256sum "$1"; }
elif command -v shasum >/dev/null 2>&1; then
    cmh_sha() { shasum -a 256 "$1"; }
else
    cmh_fail '安装需要 sha256sum 或 shasum 校验发行包。'
fi

cmh_stage=$(mktemp -d "${TMPDIR:-/tmp}/cmh-release.XXXXXXXX")
trap 'rm -rf -- "$cmh_stage"' 0
trap 'exit 1' HUP INT TERM

cmh_fetch() {
    cmh_file=$1
    cmh_limit=$2
    cmh_deadline=$(($(date +%s) + $3))
    cmh_status=28
    printf '正在下载 %s 的官方发行附件 %s…\n' "$cmh_tag" "$cmh_file" >&2
    # Retry TLS resets on the same official URL, sharing one deadline per asset.
    cmh_attempt=1
    for cmh_route in environment direct environment direct; do
        cmh_remaining=$((cmh_deadline - $(date +%s)))
        [ "$cmh_remaining" -gt 0 ] || break
        set --
        if [ "$cmh_route" = direct ]; then
            set -- --noproxy '*'
        fi
        if curl -q -fsL --http1.1 --proto '=https' --proto-redir '=https' --max-redirs 3 \
            --connect-timeout 12 --max-time "$cmh_remaining" --max-filesize "$cmh_limit" \
            --speed-limit 4096 --speed-time 30 \
            "$@" "$cmh_release/$cmh_file" -o "$cmh_stage/$cmh_file"; then
            cmh_size=$(wc -c < "$cmh_stage/$cmh_file")
            [ "$cmh_size" -gt 0 ] && [ "$cmh_size" -le "$cmh_limit" ] || \
                cmh_fail '发行附件大小不符合预期。'
            return
        else
            cmh_status=$?
        fi
        [ "$cmh_attempt" -lt 4 ] || break
        [ "$cmh_deadline" -gt "$(date +%s)" ] || break
        cmh_attempt=$((cmh_attempt + 1))
        printf '%s 下载中断（curl %s），准备第 %s/4 次尝试。\n' \
            "$cmh_file" "$cmh_status" "$cmh_attempt" >&2
        sleep 1
    done
    cmh_fail "无法下载 $cmh_tag 的官方发行附件 ${cmh_file}（curl ${cmh_status}），请检查网络或发行状态。"
}

cmh_fetch SHA256SUMS 65536 60
cmh_expected=$(LC_ALL=C awk -v name="$cmh_archive" '
    $2 == name {
        count++
        if (length($1) == 64 && $1 ~ /^[0-9a-f]+$/ && substr($0, 65) == "  " name) digest = $1
    }
    END { if (count != 1 || digest == "") exit 1; print digest }
' "$cmh_stage/SHA256SUMS") || cmh_fail '官方校验清单没有唯一的源码包校验值。'
cmh_fetch "$cmh_archive" 134217728 180
printf '正在校验发行源码包…\n' >&2
cmh_actual=$(cmh_sha "$cmh_stage/$cmh_archive") || cmh_fail '无法计算源码包校验值。'
cmh_actual=${cmh_actual%% *}
[ "$cmh_actual" = "$cmh_expected" ] || cmh_fail '源码包校验失败，安装已停止。'

# Only materialize the installer's known files. tar writes member contents to
# stdout, so archive paths, links and metadata do not touch the filesystem.
for cmh_file in \
    scripts/install-hub \
    scripts/install_hub.py \
    scripts/install_local_docker.py \
    scripts/install_nas.sh \
    scripts/install_release_fetch.py \
    scripts/install_first_device.py \
    scripts/admin_entry.py \
    apps/hub/src/cloud_memory_hub/onboarding/connect.py
do
    cmh_member="$cmh_prefix/$cmh_file"
    cmh_entry=$(tar -tvzf "$cmh_stage/$cmh_archive" "$cmh_member") || \
        cmh_fail '发行源码包缺少安装所需文件。'
    case "$cmh_entry" in
        -*) ;;
        *) cmh_fail '发行源码包中的安装文件不是普通文件。' ;;
    esac
    [ "$(printf '%s\n' "$cmh_entry" | wc -l)" -eq 1 ] || cmh_fail '发行源码包含重复安装文件。'
    cmh_destination="$cmh_stage/$cmh_member"
    mkdir -p "${cmh_destination%/*}"
    tar -xOzf "$cmh_stage/$cmh_archive" "$cmh_member" > "$cmh_destination" || \
        cmh_fail '无法从发行源码包读取安装文件。'
    [ -s "$cmh_destination" ] || cmh_fail '发行源码包中的安装文件为空。'
done

# Keep cwd and argument boundaries, and pass the verified tag last so the
# installation cannot select a different release from the one we unpacked.
printf '发行源码包校验通过，正在启动安装器…\n' >&2
sh "$cmh_stage/$cmh_prefix/scripts/install-hub" "$@" --tag "$cmh_tag"
