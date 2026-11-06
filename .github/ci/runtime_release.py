"""Render runtime bundle status and update only an existing Release body."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import release_checks as checks
from src.application.runtime_bundle import asset_name, release_platforms

BASE = Path(__file__).resolve().parents[2]
START = "<!-- om-runtime-bundles:start -->"
END = "<!-- om-runtime-bundles:end -->"


def status_block(tag, sha, state, run_url):
    platforms = release_platforms(BASE)
    lines = [START, "### Runtime bundles", "", f"Status: **{state}**", "",
             f"Tag: `{tag}` · Commit: `{sha}`", "",
             "Release platforms: " + ", ".join(f"`{p}`" for p in platforms) + "."]
    if state == "building":
        lines += ["", "运行包构建中；核心产物已发布，运行包尚未完成资格校验与补挂。",
                  "缺少本 tag 合格运行包时安装会拒绝，请等待 ready 后重试；不会自动回退源码或使用其他版本包。"]
    elif state == "failed":
        lines += ["", "运行包构建、资格校验或补挂失败（含取消），本次未确认运行包已就绪。",
                  "已发布的核心产物仍可下载。请查看 Actions 失败步骤；在原 run 选择 Re-run all jobs，仍补挂到同一个 Release。",
                  "请重跑整次 run，以便重新标记 building；仅 Re-run failed jobs 不会重置上次的 failed 状态。"]
    elif state == "ready":
        lines += ["", "运行包已通过完整资格校验，以下资产的远端大小与 SHA-256 已回读核对："]
        for platform in platforms:
            name = asset_name(tag, platform)
            url = f"https://github.com/{checks.REPOSITORY}/releases/download/{tag}/{name}"
            lines += [f"- `{platform}`: [运行包]({url}) · [sha256]({url}.sha256) · "
                      f"[manifest]({url}.manifest.json) · [qualification.json]({url}.qualification.json)"]
    else:
        raise checks.EvidenceError("invalid runtime bundle status")
    lines += ["", f"[构建与补挂日志]({run_url})", END]
    return "\n".join(lines)


def render_status(body, block):
    if START not in body and END not in body:
        return body.rstrip() + "\n\n" + block + "\n"
    if body.count(START) != 1 or body.count(END) != 1 or body.index(START) >= body.index(END):
        raise checks.EvidenceError("invalid runtime status markers")
    start, end = body.index(START), body.index(END) + len(END)
    return body[:start] + block + body[end:]


def update_status(api, tag, sha, state, run_url, *, bundle_dir=None, run=subprocess.run):
    if state == "ready":
        if bundle_dir is None:
            raise checks.EvidenceError("ready requires verified runtime artifacts")
        release = checks.readback_runtime(api, tag, sha, bundle_dir=bundle_dir)
    else:
        release = checks.existing_release(api, tag, sha)
    body = render_status(release.get("body") or "", status_block(tag, sha, state, run_url))
    run(["gh", "api", "--method", "PATCH", f"repos/{checks.REPOSITORY}/releases/{release['id']}",
         "--input", "-"], input=json.dumps({"body": body}), text=True, check=True,
        stdout=subprocess.DEVNULL, timeout=60)
    if checks.existing_release(api, tag, sha).get("body") != body:
        raise checks.EvidenceError("runtime status readback mismatch")
    print(f"[RUNTIME_RELEASE_STATUS] tag={tag} state={state}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare-notes", "update"))
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--state", choices=("ready", "failed"))
    parser.add_argument("--bundle-dir", type=Path)
    args = parser.parse_args()
    try:
        tag, sha = (os.environ.get(k, "") for k in ("RELEASE_TAG", "GITHUB_SHA"))
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=10).strip()
        checks.source_identity(repo=os.environ.get("GITHUB_REPOSITORY", ""), sha=sha, head=head,
                               version=Path("VERSION").read_text().strip(), tag=tag)
        run_url = f"https://github.com/{checks.REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        if args.mode == "prepare-notes":
            if args.notes is None:
                parser.error("prepare-notes requires --notes")
            body = render_status(args.notes.read_text(), status_block(tag, sha, "building", run_url))
            args.notes.write_text(body)
        else:
            if args.state is None:
                parser.error("update requires --state")
            update_status(checks.GitHubReads(os.environ.get("GH_TOKEN", "")), tag, sha,
                          args.state, run_url, bundle_dir=args.bundle_dir)
        return 0
    except (checks.EvidenceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[RUNTIME_RELEASE_ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
