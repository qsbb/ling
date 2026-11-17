#!/usr/bin/env bash
# Fast-forward the source checkout, then run the regular source-mode installer.

set -Eeuo pipefail

fail() {
  printf 'ling upgrade: %s\n' "$*" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || fail "需要先安装 Git"
command -v bash >/dev/null 2>&1 || fail "需要 Bash"

install_root="${LING_INSTALL_DIR:-}"
if [ -z "$install_root" ]; then
  current_root=""
  if current_root=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null) \
    && [ -f "$current_root/deploy/install.sh" ]; then
    install_root="$current_root"
  else
    [ -n "${HOME:-}" ] || fail "HOME 未设置，请指定 LING_INSTALL_DIR"
    install_root="$HOME/ling"
  fi
fi

case "$install_root" in
  /*) ;;
  *) install_root="$PWD/$install_root" ;;
esac

git -C "$install_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fail "安装目录不是 Git 检出：$install_root"
[ -f "$install_root/deploy/install.sh" ] \
  || fail "LING_INSTALL_DIR 不是 ling 源码安装目录"

if [ -f "$install_root/deploy/.env" ] \
  && grep -Eq '^[[:space:]]*LING_APP_IMAGE=' "$install_root/deploy/.env"; then
  fail "检测到镜像模式配置；请使用镜像模式发布流程，不能用源码安装器升级"
fi

if [ -n "$(git -C "$install_root" status --porcelain --untracked-files=normal)" ]; then
  fail "安装目录有未提交或未跟踪文件，请先处理后再升级"
fi

branch=$(git -C "$install_root" branch --show-current)
[ -n "$branch" ] || fail "安装目录处于 detached HEAD，请先切回跟踪分支"

remote="${LING_INSTALL_REMOTE:-}"
ref="${LING_INSTALL_REF:-}"
if [ -z "$ref" ]; then
  upstream=$(git -C "$install_root" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
  if [ -n "$upstream" ] && [[ "$upstream" == */* ]]; then
    remote="${remote:-${upstream%%/*}}"
    ref="${upstream#*/}"
  else
    ref="$branch"
  fi
fi
remote="${remote:-origin}"

git check-ref-format --branch "$ref" >/dev/null 2>&1 \
  || fail "LING_INSTALL_REF 不是有效分支名"
git -C "$install_root" remote get-url "$remote" >/dev/null 2>&1 \
  || fail "安装目录没有名为 $remote 的 Git 远端"

printf '获取 ling 更新 %s/%s\n' "$remote" "$ref"
git -C "$install_root" fetch --quiet --no-tags "$remote" "$ref" \
  || fail "获取源码失败，请检查网络与 Git 凭据"
git -C "$install_root" merge --ff-only --quiet FETCH_HEAD \
  || fail "本地提交与远端不构成快进关系，请先人工检查分支"

cd "$install_root"
printf '开始构建并部署 ling\n'
exec bash deploy/install.sh "$@"
