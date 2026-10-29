#!/usr/bin/env python3
"""用 GitHub Git Data API 推送（本机 git-over-https 到 github.com 被墙）。

## 为什么不用 `git push`

这台机器只有 **api.github.com** 可达：`github.com:443`、`github.com:22`、
`ssh.github.com:443` 全部不通，`git push` 直接超时。而 `gh api` 走
api.github.com 是通的，所以改走 Git Data API。

## 语义：一次本地提交 → 一次远端提交

取本地 HEAD 的内容与提交信息，在远端 main 之上追加一个提交。每次本地提交后
跑一次，远端粒度就与本地一致。

**远端提交 sha 与本地不同**：远端首个提交是 API 建仓的落脚点（GitHub 不允许
往"没有任何 ref"的库里写 blob），父链不同所以 sha 不同。**内容可验证一致**：
脚本比对 tree sha。

## 增量，不是全量

用 `base_tree` = 远端 HEAD 的 tree，只传变化的文件、只建变化条目。
早先的版本每次把 392 个 blob 全传一遍，一次推送要几分钟。

用法：
    python scripts/gh_push.py            # 推 HEAD
    python scripts/gh_push.py --status   # 只看是否已同步
"""

from __future__ import annotations

import base64
import subprocess
import sys

import httpx

REPO = "qsbb/ling"
BRANCH = "main"


API = "https://api.github.com"


def _token() -> str:
    return subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()


_CLIENT: httpx.Client | None = None


def client() -> httpx.Client:
    """复用连接。用 httpx 而不是 `gh api -f`：后者会把 JSON 数组编码成字符串，
    建 tree 时会 422 Invalid tree info。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Accept": "application/vnd.github+json",
            },
            timeout=120.0,
        )
    return _CLIENT


def api_get(path: str, **params: object) -> object:
    r = client().get(path, params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} 失败: {r.status_code} {r.text[:200]}")
    return r.json()


def api_post(path: str, body: dict) -> object:
    r = client().post(path, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"POST {path} 失败: {r.status_code} {r.text[:200]}")
    return r.json()


def api_patch(path: str, body: dict) -> object:
    r = client().patch(path, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH {path} 失败: {r.status_code} {r.text[:200]}")
    return r.json()


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def local_entries(tree: str) -> dict[str, dict]:
    out = {}
    for line in git("ls-tree", "-r", tree).decode().splitlines():
        meta, path = line.split("\t", 1)
        mode, typ, sha = meta.split()
        if typ == "blob":
            out[path] = {"mode": mode, "sha": sha}
    return out


def remote_head() -> str:
    return str(api_get(f"/repos/{REPO}/git/ref/heads/{BRANCH}")["object"]["sha"])


def remote_entries(tree: str) -> dict[str, dict]:
    data = api_get(f"/repos/{REPO}/git/trees/{tree}", recursive=1)
    if data.get("truncated"):
        # 文件多到服务端截断时，退回"全量比较"不安全，直接报错让人知道
        raise RuntimeError("远端 tree 被截断，无法增量比较")
    return {
        e["path"]: {"mode": e["mode"], "sha": e["sha"]}
        for e in data["tree"]
        if e["type"] == "blob"
    }


def main() -> int:
    local_tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    head = remote_head()
    remote_tree = str(
        api_get(f"/repos/{REPO}/commits/{head}")["commit"]["tree"]["sha"]
    )

    if "--status" in sys.argv:
        print(f"本地 tree: {local_tree}\n远端 tree: {remote_tree}")
        print("一致" if local_tree == remote_tree else "不一致（有未推送的改动）")
        return 0

    if local_tree == remote_tree:
        print("远端内容已与本 HEAD 一致，无需推送")
        return 0

    mine = local_entries(local_tree)
    theirs = remote_entries(remote_tree) if remote_tree else {}

    added = {p: e for p, e in mine.items() if theirs.get(p, {}).get("sha") != e["sha"]}
    removed = [p for p in theirs if p not in mine]
    print(f"变更：新增/修改 {len(added)}，删除 {len(removed)}")

    for path, e in added.items():
        data = git("cat-file", "blob", e["sha"])
        got = api_post(
            f"/repos/{REPO}/git/blobs",
            {"encoding": "base64", "content": base64.b64encode(data).decode()},
        )["sha"]
        if got != e["sha"]:
            print(f"blob sha 不一致：{path}", file=sys.stderr)
            return 1

    entries: list[dict] = [
        {"path": p, "mode": e["mode"], "type": "blob", "sha": e["sha"]}
        for p, e in added.items()
    ]
    # 用 base_tree 时，把要删的文件显式置 null
    entries += [{"path": p, "mode": theirs[p]["mode"], "type": "blob", "sha": None} for p in removed]

    new_tree = api_post(
        f"/repos/{REPO}/git/trees", {"base_tree": remote_tree, "tree": entries}
    )["sha"]
    if new_tree != local_tree:
        # 不相等说明增量拼出来的东西和本地不一致，不能硬推
        print(f"新 tree {new_tree} 与本地 {local_tree} 不一致，已中止", file=sys.stderr)
        return 1

    msg = git("log", "-1", "--format=%B").decode()
    an = git("log", "-1", "--format=%an").decode().strip()
    ae = git("log", "-1", "--format=%ae").decode().strip()
    ad = git("log", "-1", "--format=%aI").decode().strip()

    commit = api_post(
        f"/repos/{REPO}/git/commits",
        {
            "message": msg,
            "tree": new_tree,
            "parents": [head],
            "author": {"name": an, "email": ae, "date": ad},
            "committer": {"name": an, "email": ae, "date": ad},
        },
    )["sha"]

    api_patch(f"/repos/{REPO}/git/refs/heads/{BRANCH}", {"sha": commit, "force": True})
    print(f"已推送 → https://github.com/{REPO}/commit/{commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
