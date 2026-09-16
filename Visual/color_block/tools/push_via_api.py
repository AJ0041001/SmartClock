#!/usr/bin/env python3
"""在没有 git 网络通道时，改用 GitHub REST API 把本地提交推上去。

为什么需要这个
--------------
实验室/校园网里 `github.com` 的 **git smart-HTTP 通道常常是断的**
（`https://github.com/<o>/<r>.git/info/refs?service=git-receive-pack`
连接直接超时），但 `api.github.com` 往往是通的。这时 `git push`
只会静默挂死，而 API 还能用。

原理就是把 `git push` 手工做一遍：

    本地文件 ──base64──▶ POST /git/blobs      （每个新文件一个 blob）
             ─────────▶ POST /git/trees      （base_tree = 远端现有的树）
             ─────────▶ POST /git/commits    （复用本地的 author/committer/时间）
             ─────────▶ PATCH /git/refs/heads/<分支>

关键点：**commit 的作者、提交者、时间戳、父提交、tree、Message 全部照抄本地
提交**，所以 GitHub 上生成的 commit SHA 与本地**完全一致**。推送完本地和远端
不会分叉，不需要再 rebase/reset。

安全约定
--------
- token 只从**文件**读（默认 `~/.gh_token`），不接受命令行参数，
  也**从不打印**；出错信息里的 token 会被替换成 `***`。
- 支持 `--dry-run`：只打印计划，不发任何写请求。
- 已上传的 blob SHA 缓存在 `.git/push_cache.json`，重跑时跳过，支持断点续传。

用法::

    # 先看计划（不需要 token、不联网写）
    python3 tools/push_via_api.py --dry-run

    # 正式推送（token 放在文件里，权限 600）
    python3 tools/push_via_api.py

    # 只核对远端内容与本地是否一致
    python3 tools/push_via_api.py --verify-only
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
#: 一次 tree 请求最多放这么多条目（GitHub 允许更多，留点余量更稳）
TREE_BATCH = 400


# ──────────────────────────────────────────────────────────────────────
# 纯逻辑：解析本地提交、拼 tree 条目（可单测，不联网）
# ──────────────────────────────────────────────────────────────────────


def parse_commit_object(raw: str) -> dict:
    """解析 ``git cat-file commit HEAD`` 的输出。

    返回 ``{tree, parents, author: {name,email,iso}, committer: {...},
    message}``，其中时间已转成 GitHub API 要的 ISO 8601 格式。
    """
    lines = raw.split("\n")
    headers: list[str] = []
    index = 0
    while index < len(lines) and lines[index].strip():
        headers.append(lines[index])
        index += 1
    message = "\n".join(lines[index + 1:])      # 跳过 header 与空行

    info: dict = {"parents": []}
    for line in headers:
        key, _, value = line.partition(" ")
        if key == "tree":
            info["tree"] = value
        elif key == "parent":
            info["parents"].append(value)
        elif key in ("author", "committer"):
            info[key] = parse_ident(value)
    info["message"] = message
    return info


def parse_ident(value: str) -> dict:
    """把 ``名字 <邮箱> 1700000000 +0800`` 拆成 API 要的结构。"""
    name_email, _, stamp = value.rpartition("> ")
    name, _, email = name_email.partition(" <")
    email = email.rstrip(">")
    return {
        "name": name,
        "email": email,
        "date": to_iso(stamp.strip()),
    }


def to_iso(stamp: str) -> str:
    """``1700000000 +0800`` → ``2023-11-14T22:13:20+08:00``。"""
    epoch_text, _, offset_text = stamp.partition(" ")
    epoch = int(epoch_text)
    sign = -1 if offset_text.startswith("-") else 1
    hours = int(offset_text[1:3] or 0)
    minutes = int(offset_text[3:5] or 0)
    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return datetime.fromtimestamp(epoch, tz).isoformat()


def build_tree_entries(changes: list[tuple[str, str]],
                       blob_shas: dict[str, str],
                       modes: dict[str, str]) -> list[dict]:
    """把 ``git diff-tree`` 的结果转成 tree API 的 entries。

    ``changes`` 是 ``(状态, 路径)`` 列表；``A``/``M`` 变成 blob 条目，
    ``D`` 变成 ``sha: None`` 的删除条目。

    mode **必须从 git 里读**（``git ls-tree``），不能靠文件系统判断：
    文件系统看不出 ``.sh`` 在 git 里存的是 100755，一旦写成 100644，
    远端 tree 就和本地不一致，commit SHA 也就对不上了。
    """
    entries: list[dict] = []
    for status, path in changes:
        if status.startswith("D"):
            entries.append({"path": path, "mode": "100644",
                            "type": "blob", "sha": None})
            continue
        sha = blob_shas.get(path)
        if sha is None:
            raise KeyError(f"缺少 blob SHA：{path}")
        entries.append({
            "path": path,
            "mode": modes.get(path, "100644"),
            "type": "blob",
            "sha": sha,
        })
    return entries


# ──────────────────────────────────────────────────────────────────────
# git 与 HTTP 的薄封装
# ──────────────────────────────────────────────────────────────────────


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


class GitHub:
    """极简的 GitHub API 客户端（只用标准库）。"""

    def __init__(self, token: str, repo: str, *, dry_run: bool = False) -> None:
        self.token = token
        self.repo = repo
        self.dry_run = dry_run
        self.calls = 0

    def scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def request(self, method: str, path: str, payload: dict | None = None):
        if self.dry_run and method != "GET":
            self.calls += 1
            return {"sha": "dry-run-" + str(self.calls)}
        url = path if path.startswith("http") else API_ROOT + path
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "smartclock-push/1.0")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = self.scrub(exc.read().decode(errors="replace"))
            raise RuntimeError(
                f"HTTP {exc.code} {method} {path}\n{detail[:600]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"网络错误 {method} {path}：{self.scrub(str(exc.reason))}"
            ) from exc
        return json.loads(body) if body else {}

    # ── 具体接口 ──
    def get_ref(self, branch: str) -> dict:
        quoted = urllib.parse.quote(branch, safe="")
        return self.request("GET", f"/repos/{self.repo}/git/ref/heads/{quoted}")

    def create_blob(self, content: bytes) -> str:
        payload = {"content": base64.b64encode(content).decode(),
                   "encoding": "base64"}
        return self.request("POST", f"/repos/{self.repo}/git/blobs",
                            payload)["sha"]

    def create_tree(self, base_tree: str, entries: list[dict]) -> str:
        payload = {"base_tree": base_tree, "tree": entries}
        return self.request("POST", f"/repos/{self.repo}/git/trees",
                            payload)["sha"]

    def create_commit(self, message: str, tree: str, parents: list[str],
                      author: dict, committer: dict) -> str:
        payload = {"message": message, "tree": tree, "parents": parents,
                   "author": author, "committer": committer}
        return self.request("POST", f"/repos/{self.repo}/git/commits",
                            payload)["sha"]

    def update_ref(self, branch: str, sha: str) -> dict:
        quoted = urllib.parse.quote(branch, safe="")
        return self.request("PATCH", f"/repos/{self.repo}/git/refs/heads/{quoted}",
                            {"sha": sha, "force": False})

    def get_commit(self, sha: str) -> dict:
        return self.request("GET", f"/repos/{self.repo}/git/commits/{sha}")


# ──────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────


def load_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def read_modes(commit: str) -> dict[str, str]:
    """``路径 → git mode``（100644 / 100755 / 120000），从 git 里读。"""
    modes: dict[str, str] = {}
    for line in git("ls-tree", "-r", commit).splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3:
            modes[path] = parts[0]
    return modes


def list_changes(commit: str) -> list[tuple[str, str]]:
    """``git diff-tree`` → [(状态, 路径)]。"""
    output = git("diff-tree", "-r", "--no-commit-id", "--name-status", commit)
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        changes.append((status.strip(), path))
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="push_via_api.py",
        description="用 GitHub REST API 推送本地提交（绕过被墙的 git 通道）",
    )
    parser.add_argument("--repo", default="AJ0041001/SmartClock",
                        help="owner/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--commit", default="HEAD", help="要推送的本地提交")
    parser.add_argument("--token-file", default="~/.gh_token")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划，不发送任何写请求")
    parser.add_argument("--verify-only", action="store_true",
                        help="只比对远端 tree 与本地是否一致")
    args = parser.parse_args()

    repo_root = Path(git("rev-parse", "--show-toplevel").strip())
    local_sha = git("rev-parse", args.commit).strip()
    info = parse_commit_object(git("cat-file", "commit", args.commit))
    changes = list_changes(local_sha)
    needs_write = not (args.dry_run or args.verify_only)
    token_path = Path(args.token_file).expanduser()
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if not needs_write and len(token) < 20:
            # 只读操作时，明显不是 token 的内容就别往请求头里塞了
            # （带着无效 Authorization 访问公开接口也会被判 401）
            token = ""
        if needs_write and len(token) < 20:
            raise SystemExit(
                f"❌ {token_path} 里的内容只有 {len(token)} 个字符，"
                f"不像是 GitHub token\n"
                f"   （经典 PAT 是 40 字符；细粒度 PAT 以 github_pat_ 开头、"
                f"90+ 字符）"
            )
    elif needs_write:
        raise SystemExit(
            f"❌ 找不到 token 文件：{token_path}\n"
            f"   请先执行（把 <TOKEN> 换成你的 GitHub token）：\n"
            f"     echo '<TOKEN>' > {token_path} && chmod 600 {token_path}"
        )
    else:
        token = ""      # 只读操作（公开仓库不需要认证）

    client = GitHub(token, args.repo, dry_run=args.dry_run)

    print("── 计划 ──")
    print(f"  仓库    : {args.repo}  分支 {args.branch}")
    print(f"  本地提交: {local_sha[:12]}  tree {info['tree'][:12]}")
    print(f"  改动文件: {len(changes)} 个")
    print()

    ref = client.get_ref(args.branch)
    remote_sha = ref["object"]["sha"]
    remote_commit = client.get_commit(remote_sha)
    print(f"  远端 HEAD: {remote_sha[:12]}  tree {remote_commit['tree']['sha'][:12]}")

    if args.verify_only:
        same = remote_commit["tree"]["sha"] == info["tree"]
        print()
        print("✅ 远端内容与本地提交一致" if same
              else f"❌ 不一致：远端 tree {remote_commit['tree']['sha'][:12]}"
                   f" ≠ 本地 {info['tree'][:12]}")
        return 0 if same else 1

    if remote_sha == local_sha:
        print("\n✅ 远端已经是这个提交，无需推送")
        return 0

    if remote_sha != info["parents"][0]:
        print(f"\n⚠️  远端 HEAD 已变（{remote_sha[:12]}），"
              f"本地提交的父提交是 {info['parents'][0][:12]}")
        print("   为避免覆盖别人的改动，请先 git pull 后再试。")
        return 2

    # ── 上传 blob ──
    cache_path = repo_root / ".git" / "push_cache.json"
    cache = load_cache(cache_path)
    blob_shas: dict[str, str] = {}
    pending = [(status, path) for status, path in changes
               if not status.startswith("D")]
    print(f"\n── 上传 {len(pending)} 个文件 ──")
    for index, (status, path) in enumerate(pending, 1):
        key = f"{local_sha}:{path}"
        if key in cache:
            blob_shas[path] = cache[key]
            continue
        data = (repo_root / path).read_bytes()
        sha = client.create_blob(data)
        cache[key] = sha
        blob_shas[path] = sha
        if index % 50 == 0 or index == len(pending):
            print(f"  {index}/{len(pending)} …")
            if not args.dry_run:
                save_cache(cache_path, cache)
    if not args.dry_run:
        save_cache(cache_path, cache)

    # ── tree（分批发，避免单请求过大）──
    entries = build_tree_entries(changes, blob_shas, read_modes(local_sha))
    print(f"\n── 建立 tree（{len(entries)} 条目，每批 {TREE_BATCH}）──")
    tree_sha = remote_commit["tree"]["sha"]
    for start in range(0, len(entries), TREE_BATCH):
        batch = entries[start:start + TREE_BATCH]
        # 每批以上一批的结果作为 base_tree，逐层叠加
        tree_sha = client.create_tree(tree_sha, batch)["sha"]
    print(f"  新 tree: {tree_sha[:12]}（本地 {info['tree'][:12]}）")

    # ── commit ──
    message = info["message"]
    if not message.endswith("\n"):
        message += "\n"
    new_sha = client.create_commit(
        message=message, tree=tree_sha, parents=[remote_sha],
        author=info["author"], committer=info["committer"],
    )
    print(f"\n── 建立 commit ──\n  新提交: {new_sha[:12]}（本地 {local_sha[:12]}）")

    # ── 更新分支 ──
    if args.dry_run:
        print("\n（--dry-run：到此为止，没有改动远端）")
        return 0
    updated = client.update_ref(args.branch, new_sha)
    print(f"\n✅ 已推送：{args.branch} → {updated['object']['sha'][:12]}"
          f"   （共 {client.calls} 次 API 调用）")
    if updated["object"]["sha"] == local_sha:
        print("   远端 SHA 与本地完全一致，本地无需 rebase/reset")
    else:
        print("   注意：远端 SHA 与本地不同（内容相同），"
              "可执行 git fetch && git reset --hard origin/main 同步")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"\n❌ {error}", file=sys.stderr)
        raise SystemExit(1)
