"""Read-only release evidence for the declared OM project contract, not admin policy."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "liuxie066/options-monitor"
WORKFLOW_ID = 273127082
WORKFLOW_PATH = "dynamic/github-code-scanning/codeql"
LANGUAGE_JOBS = {"Analyze (actions)", "Analyze (python)"}


class EvidenceError(Exception):
    """Only fixed, sanitized messages may leave the observer."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubReads:
    def __init__(self, token, *, open_url=None, clock=time.monotonic, budget=900):
        if not token:
            raise EvidenceError("missing workflow token")
        self.token = token
        self.open_url = open_url or build_opener(NoRedirect()).open
        self.clock = clock
        self.deadline = clock() + budget

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise EvidenceError("release evidence deadline exceeded")
        return remaining

    def get(self, path, *, missing=False):
        # No caller-supplied origin, credentials, mutation method or arbitrary URL.
        if not re.fullmatch(r"/(?:actions/(?:workflows/[0-9]+/runs|runs/[0-9]+(?:/attempts/[0-9]+/jobs)?)|git/ref/tags/v[0-9A-Za-z.+-]+|git/tags/[0-9a-f]{40}|releases/tags/v[0-9A-Za-z.+-]+|releases/[0-9]+/assets)(?:\?[a-z0-9_=&]+)?", path):
            raise EvidenceError("invalid evidence endpoint")
        request = Request(f"https://api.github.com/repos/{REPOSITORY}{path}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self.open_url(request, timeout=min(10, self.remaining())) as response:
                data = json.load(response)
            self.remaining()
            if not isinstance(data, (dict, list)):
                raise ValueError()
            return data
        except HTTPError as exc:
            if missing and exc.code == 404 and path.startswith("/git/ref/tags/"):
                self.remaining()
                return None
            raise EvidenceError("GitHub evidence read failed") from None
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GitHub evidence read failed") from None


def _positive(value):
    return type(value) is int and value > 0


def pages(api, path, key):
    rows, total, ids = [], None, set()
    for page in range(1, 11):
        separator = "&" if "?" in path else "?"
        payload = api.get(f"{path}{separator}per_page=100&page={page}")
        if not isinstance(payload, dict) or type(payload.get("total_count")) is not int:
            raise EvidenceError("invalid paginated evidence")
        if total is None:
            total = payload["total_count"]
        if total != payload["total_count"] or not 0 <= total <= 1000:
            raise EvidenceError("changing or truncated evidence")
        batch = payload.get(key)
        if not isinstance(batch, list) or len(batch) > 100:
            raise EvidenceError("invalid paginated evidence")
        for row in batch:
            if not isinstance(row, dict) or not _positive(row.get("id")) or row["id"] in ids:
                raise EvidenceError("duplicate or invalid evidence")
            ids.add(row["id"])
            rows.append(row)
        if len(rows) == total:
            return rows
        if len(rows) > total or len(batch) < 100:
            raise EvidenceError("incomplete paginated evidence")
    raise EvidenceError("truncated evidence")


def trusted_run(run, sha):
    return (isinstance(run, dict) and run.get("workflow_id") == WORKFLOW_ID
            and run.get("path") == WORKFLOW_PATH and run.get("event") == "dynamic"
            and run.get("head_sha") == sha and run.get("head_branch") == "main"
            and isinstance(run.get("repository"), dict)
            and run["repository"].get("full_name") == REPOSITORY)


def validate_run(run):
    if (not isinstance(run, dict)
            or not all(_positive(run.get(k)) for k in ("id", "workflow_id", "run_number", "run_attempt"))
            or not all(isinstance(run.get(k), str) and run[k]
                       for k in ("path", "event", "head_branch", "status"))
            or not isinstance(run.get("head_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
            or "conclusion" not in run
            or (run["conclusion"] is not None and not isinstance(run["conclusion"], str))
            or not isinstance(run.get("repository"), dict)
            or not isinstance(run["repository"].get("full_name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", run["repository"]["full_name"])):
        raise EvidenceError("invalid run identity")


def latest_run(api, sha):
    runs = pages(api, f"/actions/workflows/{WORKFLOW_ID}/runs?head_sha={sha}", "workflow_runs")
    # Malformed new evidence is not an unrelated run: never hide it behind an
    # older success. Only fully shaped, genuinely unrelated sources are ignored.
    for run in runs:
        validate_run(run)
    candidates = [run for run in runs if trusted_run(run, sha)]
    if len({run["run_number"] for run in candidates}) != len(candidates):
        raise EvidenceError("ambiguous latest run")
    return max(candidates, key=lambda r: (r["run_number"], r["id"])) if candidates else None


def wait_codeql(api, sha, *, sleep=time.sleep):
    """One original budget, newest run only, complete current attempt only."""
    while True:
        api.remaining()
        run = latest_run(api, sha)
        if run is not None:
            if run.get("status") == "completed":
                if run.get("conclusion") != "success":
                    raise EvidenceError("latest CodeQL run did not succeed")
                jobs = pages(api, f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
                for job in jobs:
                    if (not _positive(job.get("run_id")) or not _positive(job.get("run_attempt"))
                            or job.get("run_id") != run["id"] or job.get("run_attempt") != run["run_attempt"]
                            or job.get("head_sha") != sha or job.get("head_branch") != "main"
                            or job.get("status") != "completed" or job.get("conclusion") != "success"):
                        raise EvidenceError("CodeQL attempt incomplete; rerun the entire workflow")
                names = [job.get("name") for job in jobs]
                if any(names.count(name) != 1 for name in LANGUAGE_JOBS):
                    raise EvidenceError("CodeQL languages incomplete; rerun the entire workflow")
                current = api.get(f"/actions/runs/{run['id']}")
                validate_run(current)
                latest = latest_run(api, sha)
                if (trusted_run(current, sha) and latest is not None
                        and current.get("id") == run["id"] == latest["id"]
                        and current.get("run_attempt") == run["run_attempt"] == latest["run_attempt"]
                        and current.get("status") == latest.get("status") == "completed"
                        and current.get("conclusion") == latest.get("conclusion") == "success"):
                    return run["id"], run["run_attempt"]
                continue
            if run.get("status") not in {"queued", "in_progress", "waiting", "pending", "requested"}:
                raise EvidenceError("invalid CodeQL run state")
        sleep(min(5, api.remaining()))


def tag_commit(api, tag, *, allow_missing):
    data = api.get(f"/git/ref/tags/{tag}", missing=allow_missing)
    if data is None and allow_missing:
        return None
    if not isinstance(data, dict) or data.get("ref") != f"refs/tags/{tag}":
        raise EvidenceError("invalid tag identity")
    seen = set()
    for _ in range(10):
        obj = data.get("object")
        if not isinstance(obj, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(obj.get("sha", ""))):
            raise EvidenceError("invalid tag identity")
        sha = obj["sha"]
        if obj.get("type") == "commit":
            return sha
        if obj.get("type") != "tag" or sha in seen:
            raise EvidenceError("invalid tag identity")
        seen.add(sha)
        data = api.get(f"/git/tags/{sha}")
        if not isinstance(data, dict) or data.get("sha") != sha:
            raise EvidenceError("invalid tag identity")
    raise EvidenceError("tag chain too deep")


def source_identity(*, repo, sha, head, version, tag):
    if (repo != REPOSITORY or not re.fullmatch(r"[0-9a-f]{40}", sha) or head != sha
            or not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", tag)
            or tag[1:] != version):
        raise EvidenceError("release source identity mismatch")


def check_tag(api, tag, sha, *, allow_missing):
    commit = tag_commit(api, tag, allow_missing=allow_missing)
    if commit is not None and commit != sha:
        raise EvidenceError("release tag moved or differs from checked commit")


def readback(api, tag, sha):
    release = api.get(f"/releases/tags/{tag}")
    if (not isinstance(release, dict) or release.get("tag_name") != tag
            or release.get("draft") is not False or not _positive(release.get("id"))):
        raise EvidenceError("publication outcome unknown or incomplete")
    required = {f"options-monitor-{tag}.tar.gz", "om-agent-spec.json", "VERSION"}
    assets, ids = [], set()
    for page in range(1, 11):
        batch = api.get(f"/releases/{release['id']}/assets?per_page=100&page={page}")
        if not isinstance(batch, list) or len(batch) > 100:
            raise EvidenceError("invalid publication assets")
        for asset in batch:
            if not isinstance(asset, dict) or not _positive(asset.get("id")) or asset["id"] in ids:
                raise EvidenceError("invalid publication assets")
            ids.add(asset["id"])
            assets.append(asset)
        if len(batch) < 100:
            break
    else:
        raise EvidenceError("publication assets truncated")
    for name in required:
        matches = [a for a in assets if a.get("name") == name]
        if len(matches) != 1 or matches[0].get("state") != "uploaded" or not _positive(matches[0].get("size")):
            raise EvidenceError("publication outcome unknown or incomplete")
    check_tag(api, tag, sha, allow_missing=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("identity", "before-publish", "readback"))
    mode = parser.parse_args().mode
    try:
        repo, sha, tag = (os.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_SHA", "RELEASE_TAG"))
        head = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()
        source_identity(repo=repo, sha=sha, head=head, version=Path("VERSION").read_text().strip(), tag=tag)
        api = GitHubReads(os.environ.get("GH_TOKEN", ""))
        allow_missing = os.environ.get("GITHUB_REF_TYPE") != "tag"
        if mode == "readback":
            readback(api, tag, sha)
        else:
            if mode == "before-publish":
                wait_codeql(api, sha)
            check_tag(api, tag, sha, allow_missing=allow_missing)
        print("publication verified" if mode == "readback" else "release evidence verified")
        return 0
    except EvidenceError as exc:
        print("publication outcome unknown or incomplete" if mode == "readback" else str(exc), file=sys.stderr)
    except Exception:
        print("publication outcome unknown or incomplete" if mode == "readback" else "release evidence unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
