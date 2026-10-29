"""Release evidence tests with synthetic API responses, never publication."""
import copy
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_checks", ROOT / ".github/ci/release_checks.py")
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)
SHA = "a" * 40
TAG = "v1.2.3"


def run_data(**changes):
    result = dict(id=10, run_number=5, run_attempt=1, workflow_id=checks.WORKFLOW_ID,
                  path=checks.WORKFLOW_PATH, event="dynamic", head_sha=SHA, head_branch="main",
                  repository={"full_name": checks.REPOSITORY}, status="completed", conclusion="success")
    result.update(changes)
    return result


def job_data(name, **changes):
    result = dict(id=1 if name == "Analyze (actions)" else 2, name=name, run_id=10,
                  run_attempt=1, head_sha=SHA, head_branch="main", status="completed", conclusion="success")
    result.update(changes)
    return result


class FakeAPI:
    def __init__(self, runs=None, jobs=None):
        self.runs = runs if runs is not None else [run_data()]
        self.jobs = jobs if jobs is not None else [job_data(n) for n in sorted(checks.LANGUAGE_JOBS)]
        self.calls = []
        self.left = 3

    def remaining(self):
        self.left -= 0.1
        if self.left <= 0:
            raise checks.EvidenceError("deadline")
        return self.left

    def get(self, path, **kwargs):
        self.calls.append(path)
        if "/workflows/" in path:
            return {"total_count": len(self.runs), "workflow_runs": copy.deepcopy(self.runs)}
        if "/attempts/" in path:
            return {"total_count": len(self.jobs), "jobs": copy.deepcopy(self.jobs)}
        return copy.deepcopy(self.runs[-1])


def test_trusted_current_full_attempt():
    api = FakeAPI()
    assert checks.wait_codeql(api, SHA, sleep=lambda seconds: None) == (10, 1)
    assert any("/attempts/1/jobs" in p for p in api.calls)


@pytest.mark.parametrize("change", [
    {"workflow_id": 1}, {"path": "fake"}, {"event": "push"}, {"head_sha": "b" * 40},
    {"head_branch": "feature"}, {"repository": {"full_name": "other/repository"}},
])
def test_wrong_source_never_counts(change):
    with pytest.raises(checks.EvidenceError):
        checks.wait_codeql(FakeAPI([run_data(**change)]), SHA, sleep=lambda seconds: None)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "neutral", "timed_out"])
def test_latest_bad_run_cannot_fall_back_to_success(conclusion):
    api = FakeAPI([run_data(), run_data(id=11, run_number=6, conclusion=conclusion)])
    with pytest.raises(checks.EvidenceError, match="did not succeed"):
        checks.wait_codeql(api, SHA)
    assert not any("/attempts/" in path for path in api.calls)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "attempt", "sha", "run", "failed", "extra_active"])
def test_incomplete_attempt_rejected(damage):
    jobs = [job_data(n) for n in sorted(checks.LANGUAGE_JOBS)]
    if damage == "missing":
        jobs.pop()
    elif damage == "duplicate":
        jobs.append(job_data(jobs[0]["name"], id=3))
    elif damage == "extra_active":
        jobs.append(job_data("extra", id=3, status="in_progress", conclusion=None))
    else:
        key, value = {"attempt": ("run_attempt", 2), "sha": ("head_sha", "b" * 40),
                      "run": ("run_id", 11), "failed": ("conclusion", "failure")}[damage]
        jobs[0][key] = value
    with pytest.raises(checks.EvidenceError):
        checks.wait_codeql(FakeAPI(jobs=jobs), SHA)


def test_changed_attempt_revalidated_without_cross_attempt_union():
    class Changed(FakeAPI):
        def get(self, path, **kwargs):
            if path == "/actions/runs/10" and self.runs[0]["run_attempt"] == 1:
                self.runs[0]["run_attempt"] = 2
                for job in self.jobs:
                    job["run_attempt"] = 2
            return super().get(path, **kwargs)
    api = Changed()
    assert checks.wait_codeql(api, SHA, sleep=lambda seconds: None) == (10, 2)
    assert any("/attempts/2/jobs" in p for p in api.calls)


@pytest.mark.parametrize("code", [403, 404, 500])
def test_http_actions_errors_are_not_missing_or_success(code):
    def fail(req, **kwargs):
        raise HTTPError(req.full_url, code, "secret", {}, None)
    api = checks.GitHubReads("synthetic", open_url=fail)
    with pytest.raises(checks.EvidenceError, match="^GitHub evidence read failed$"):
        api.get(f"/actions/workflows/{checks.WORKFLOW_ID}/runs?head_sha={SHA}")


def test_real_http_adapter_is_fixed_origin_readonly_and_bounded():
    seen = []
    def open_url(request, **kwargs):
        seen.append((request, kwargs))
        return io.BytesIO(json.dumps({"total_count": 0, "workflow_runs": []}).encode())
    api = checks.GitHubReads("synthetic", open_url=open_url, clock=lambda: 10, budget=3)
    assert checks.latest_run(api, SHA) is None
    request, kwargs = seen[0]
    assert request.get_method() == "GET"
    assert request.full_url.startswith(f"https://api.github.com/repos/{checks.REPOSITORY}/actions/")
    assert kwargs["timeout"] == 3
    with pytest.raises(checks.EvidenceError):
        api.get("https://evil.invalid")


@pytest.mark.parametrize("damage", ["sha", "version", "tag", "repo"])
def test_source_identity_rejects_unchecked_source(damage):
    arguments = dict(repo=checks.REPOSITORY, sha=SHA, head=SHA, version="1.2.3", tag=TAG)
    arguments[{"sha": "head", "version": "version", "tag": "tag", "repo": "repo"}[damage]] = "bad"
    with pytest.raises(checks.EvidenceError):
        checks.source_identity(**arguments)


def test_tag_movement_or_missing_blocks_tag_entrance():
    class API:
        def get(self, path, **kwargs):
            return {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": "b" * 40}}
    with pytest.raises(checks.EvidenceError):
        checks.check_tag(API(), TAG, SHA, allow_missing=False)


def test_publication_readback_requires_all_assets_and_exact_tag():
    class API:
        def __init__(self):
            self.assets = [dict(id=i, name=n, state="uploaded", size=10) for i, n in enumerate(
                [f"options-monitor-{TAG}.tar.gz", "om-agent-spec.json", "VERSION"], 1)]
        def get(self, path, **kwargs):
            if "/assets?" in path:
                return self.assets
            if "/releases/tags/" in path:
                return dict(id=1, tag_name=TAG, draft=False)
            return {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": SHA}}
    api = API()
    checks.readback(api, TAG, SHA)
    api.assets.pop()
    with pytest.raises(checks.EvidenceError):
        checks.readback(api, TAG, SHA)


def workflows():
    return {name: yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)
            for name in ('guardrails.yml', 'release.yml', 'release-from-version.yml', '_release-reusable.yml')}


def test_all_entrances_share_native_gate_and_only_publisher_has_actions_read():
    files = workflows()
    for name in ('release.yml', 'release-from-version.yml'):
        assert files[name]['jobs']['release']['uses'] == './.github/workflows/guardrails.yml'
    guard = files['guardrails.yml']
    assert guard['jobs']['release']['needs'] == 'guardrails'
    assert guard['jobs']['release']['uses'] == './.github/workflows/_release-reusable.yml'
    assert guard['jobs']['guardrails']['needs'] == ['plan', 'checks', 'regression']
    assert guard['jobs']['regression']['strategy']['matrix']['shard'] == list(map(str, range(8)))
    for filename, workflow in files.items():
        assert 'actions' not in workflow.get('permissions', {})
        for job in workflow['jobs'].values():
            if 'runs-on' in job:
                expected = {'contents': 'write', 'actions': 'read'} if filename == '_release-reusable.yml' else {'contents': 'read'}
                assert job['permissions'] == expected
            for step in job.get('steps', []):
                if step.get('uses') == 'actions/checkout@v5':
                    assert step['with']['ref'] == '${{ github.sha }}'
    publisher = files['_release-reusable.yml']['jobs']['release']
    assert publisher['timeout-minutes'] == '25'
    assert publisher['concurrency']['cancel-in-progress'] == 'false'
    steps = publisher['steps']
    action = next(s for s in steps if s.get('id') == 'publish')
    assert action['uses'] == 'softprops/action-gh-release@v3'
    assert action['with']['target_commitish'] == '${{ github.sha }}'
    assert steps[steps.index(action) - 1]['run'].endswith('before-publish')
    assert "steps.publish.outcome == 'failure'" in steps[-1]['if']
    assert 'run_regression_gates' not in str(files)


@pytest.mark.parametrize('field', ['workflow_id', 'path', 'head_sha', 'head_branch', 'repository', 'event'])
def test_newer_malformed_run_cannot_be_hidden_by_older_success(field):
    newer = run_data(id=11, run_number=6)
    del newer[field]
    with pytest.raises(checks.EvidenceError, match='invalid run identity'):
        checks.wait_codeql(FakeAPI([run_data(), newer]), SHA)


@pytest.mark.parametrize('field', ['run_id', 'run_attempt'])
def test_boolean_job_identity_is_not_an_integer(field):
    jobs = [job_data(n) for n in sorted(checks.LANGUAGE_JOBS)]
    jobs[0][field] = True
    with pytest.raises(checks.EvidenceError):
        checks.wait_codeql(FakeAPI(jobs=jobs), SHA)


def test_incomplete_page_cannot_be_treated_as_complete_run_list():
    class Truncated(FakeAPI):
        def get(self, path, **kwargs):
            return {'total_count': 2, 'workflow_runs': [run_data()]}
    with pytest.raises(checks.EvidenceError, match='incomplete'):
        checks.latest_run(Truncated(), SHA)


def test_request_completion_after_deadline_cannot_pass():
    times = iter([0, 0, 2])
    api = checks.GitHubReads('synthetic', clock=lambda: next(times), budget=1,
                            open_url=lambda *a, **k: io.BytesIO(b'{}'))
    with pytest.raises(checks.EvidenceError, match='deadline'):
        api.get('/actions/runs/10')
