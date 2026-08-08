#!/usr/bin/env sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)"
cd "$project_root"

python3 <<'PY'
import os
import json
import subprocess
import tempfile
from pathlib import Path

workflow_path = Path(".github/workflows/release-images.yml")
if not workflow_path.is_file():
    raise SystemExit(f"missing workflow: {workflow_path}")

import yaml

workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
if not isinstance(workflow, dict):
    raise SystemExit("workflow must be a mapping")

on_config = workflow.get(True, workflow.get("on"))
if not isinstance(on_config, dict):
    raise SystemExit("workflow must define an on mapping")

push = on_config.get("push")
if not isinstance(push, dict):
    raise SystemExit("push trigger must be a mapping")
if push.get("branches") != ["main"]:
    raise SystemExit("push branches must be ['main']")
if push.get("tags") != ["v*"]:
    raise SystemExit("push tags must be ['v*']")
if "pull_request" not in on_config:
    raise SystemExit("pull_request trigger is required")
if "workflow_dispatch" not in on_config:
    raise SystemExit("workflow_dispatch trigger is required")

permissions = workflow.get("permissions")
if permissions != {"contents": "read"}:
    raise SystemExit("top-level permissions must be contents: read only")

concurrency = workflow.get("concurrency")
if not isinstance(concurrency, dict):
    raise SystemExit("workflow must define concurrency")
if concurrency.get("cancel-in-progress") is not False:
    raise SystemExit("workflow concurrency must keep cancel-in-progress false")
group = concurrency.get("group")
if not isinstance(group, str):
    raise SystemExit("workflow concurrency group must be text")
for fragment in ("github.repository", "github.ref"):
    if fragment not in group:
        raise SystemExit(f"workflow concurrency group must include {fragment}")

jobs = workflow.get("jobs")
if not isinstance(jobs, dict):
    raise SystemExit("jobs must be a mapping")


def find_run_steps(steps, pattern):
    return [
        step["run"]
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and pattern in step["run"]
    ]


def find_uses_step(steps, action):
    for step in steps:
        if isinstance(step, dict) and step.get("uses") == action:
            return step
    return None

test_job = jobs.get("test")
preflight_job = jobs.get("preflight")
publish_job = jobs.get("publish")
promote_job = jobs.get("promote-stable")
if not all(isinstance(job, dict) for job in (test_job, preflight_job, publish_job, promote_job)):
    raise SystemExit("test, preflight, publish, and promote-stable jobs are required")

if preflight_job.get("needs") != ["test"]:
    raise SystemExit("preflight must need test")
if publish_job.get("needs") != ["preflight"]:
    raise SystemExit("publish must need the single preflight job")

preflight_steps = preflight_job.get("steps")
if not isinstance(preflight_steps, list):
    raise SystemExit("preflight job must define steps")

metadata_steps = [
    step
    for step in preflight_steps
    if isinstance(step, dict)
    and isinstance(step.get("run"), str)
    and "git show -s --format=%cI" in step["run"]
]
if len(metadata_steps) != 1:
    raise SystemExit("preflight must resolve release metadata exactly once")


def run_release_metadata(metadata_shell: str, release_tag: str) -> tuple[subprocess.CompletedProcess[str], str]:
    rendered_shell = (
        metadata_shell
        .replace("${{ github.ref_name }}", "v1.2.3")
        .replace("${{ github.sha }}", "abc123")
    )
    with tempfile.TemporaryDirectory(prefix="release-metadata-") as tempdir:
        temp_root = Path(tempdir)
        bin_dir = temp_root / "bin"
        bin_dir.mkdir()
        git_path = bin_dir / "git"
        git_path.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1 $2 $3\" = \"show -s --format=%cI\" ]; then\n"
            "  printf '%s\\n' '2026-08-08T08:09:10+08:00'\n"
            "  exit 0\n"
            "fi\n"
            "exit 97\n",
            encoding="utf-8",
        )
        git_path.chmod(0o755)
        output_path = temp_root / "github-output"
        script_path = temp_root / "metadata.sh"
        script_path.write_text("#!/usr/bin/env sh\nset -eu\n" + rendered_shell + "\n", encoding="utf-8")
        script_path.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
        environment["GITHUB_OUTPUT"] = str(output_path)
        environment["GITHUB_REF_NAME"] = release_tag
        environment["GITHUB_SHA"] = "abc123"
        result = subprocess.run(
            ["sh", str(script_path)],
            cwd=temp_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return result, output


def verify_release_metadata_utc(metadata_shell: str) -> None:
    result, output = run_release_metadata(metadata_shell, "v1.2.3")
    if result.returncode != 0:
        raise SystemExit(f"release metadata must run successfully: {result.stderr.strip()}")
    if output != "release_tag=v1.2.3\nrelease_created=2026-08-08T00:09:10Z\n":
        raise SystemExit("release metadata must normalize commit time to UTC RFC3339")


def verify_release_metadata_rejects_leading_zero(metadata_shell: str) -> None:
    result, output = run_release_metadata(metadata_shell, "v01.2.3")
    if result.returncode == 0 or result.stderr.strip() != "release tags must use canonical vX.Y.Z format" or output:
        raise SystemExit("release metadata must reject canonical SemVer identifiers with leading zeroes")


verify_release_metadata_utc(metadata_steps[0]["run"])
verify_release_metadata_rejects_leading_zero(metadata_steps[0]["run"])

test_permissions = test_job.get("permissions")
if isinstance(test_permissions, dict) and "packages" in test_permissions:
    raise SystemExit("test job must not receive packages write permissions")

expected_publish_permissions = {"contents": "read", "packages": "write"}
if preflight_job.get("permissions") != expected_publish_permissions:
    raise SystemExit("preflight job must define job-level contents: read and packages: write")
if publish_job.get("permissions") != expected_publish_permissions:
    raise SystemExit("publish job must define job-level contents: read and packages: write")
if promote_job.get("permissions") != expected_publish_permissions:
    raise SystemExit("promote-stable job must define job-level contents: read and packages: write")

test_steps = test_job.get("steps")
if not isinstance(test_steps, list):
    raise SystemExit("test job must define steps")

for action in (
    "actions/checkout@v4",
    "actions/setup-python@v5",
    "actions/setup-node@v4",
):
    if find_uses_step(test_steps, action) is None:
        raise SystemExit(f"missing test action: {action}")

test_commands = [
    step.get("run")
    for step in test_steps
    if isinstance(step, dict) and isinstance(step.get("run"), str)
]
required_test_commands = [
    'pip install ".[dev]"',
    "pytest --cov --cov-report=term-missing",
    "npm ci",
    "npm run test:js",
    "npx playwright install --with-deps chromium",
    "npm run test:e2e",
    "SHUNDA_REQUIRE_DOCKER_COMPOSE=1 bash scripts/system-update-compose.test.sh",
    "bash scripts/release-images-contract.test.sh",
]
for command in required_test_commands:
    if command not in test_commands:
        raise SystemExit(f"missing test command: {command}")

publish_if = publish_job.get("if")
if not isinstance(publish_if, str):
    raise SystemExit("publish job must define an if expression")
for fragment in (
    "github.event_name == 'push'",
    "github.ref_type == 'tag'",
    "github.event.deleted != true",
    "startsWith(github.ref, 'refs/tags/v')",
):
    if fragment not in publish_if:
        raise SystemExit(f"publish gate must include {fragment}")

strategy = publish_job.get("strategy")
if not isinstance(strategy, dict):
    raise SystemExit("publish job must define strategy")
matrix = strategy.get("matrix")
if not isinstance(matrix, dict):
    raise SystemExit("publish matrix must be a mapping")
if matrix.get("target") != ["web", "updater"]:
    raise SystemExit("publish matrix target must be [web, updater]")

publish_steps = publish_job.get("steps")
if not isinstance(publish_steps, list):
    raise SystemExit("publish job must define steps")


for action in (
    "actions/checkout@v4",
    "docker/setup-buildx-action@v3",
    "docker/login-action@v3",
    "docker/metadata-action@v5",
    "docker/build-push-action@v6",
):
    if find_uses_step(publish_steps, action) is None:
        raise SystemExit(f"missing publish action: {action}")

if find_run_steps(publish_steps, "docker buildx imagetools inspect"):
    raise SystemExit("publish matrix must not repeat immutable tag preflight")

workflow_text = workflow_path.read_text(encoding="utf-8")
if "github.event.head_commit.timestamp" in workflow_text:
    raise SystemExit("workflow must not depend on github.event.head_commit.timestamp")

metadata_run = metadata_steps[0]["run"]
for fragment in ('git show -s --format=%cI "${GITHUB_SHA}"', "GITHUB_OUTPUT", "release_created"):
    if fragment not in metadata_run:
        raise SystemExit(f"release metadata step must include {fragment}")

preflight_matches = [
    step
    for step in preflight_steps
    if isinstance(step, dict)
    and isinstance(step.get("run"), str)
    and "docker buildx imagetools inspect" in step["run"]
]
if len(preflight_matches) != 1:
    raise SystemExit("preflight job must use one fail-closed preflight step for both immutable tags")
preflight_run = preflight_matches[0]["run"]
for repository in (
    "ghcr.io/s450586793/shunda-finance-web",
    "ghcr.io/s450586793/shunda-finance-updater",
):
    if repository not in preflight_run:
        raise SystemExit(f"missing immutable preflight for {repository}")
for fragment in (
    "manifest unknown",
    "name unknown",
    "unable to verify immutable tag availability",
    "immutable tag already exists",
):
    if fragment not in preflight_run:
        raise SystemExit(f"preflight step must include fail-closed handling for {fragment}")
for fragment in (
    "image_lc",
    '${image_lc}: not found',
):
    if fragment not in preflight_run:
        raise SystemExit(f"preflight step must anchor registry not-found handling with {fragment}")
for forbidden in (
    "manifest unknown|name unknown|not found",
    "grep -eq 'manifest unknown|name unknown|not found'",
    "grep -eq \"manifest unknown|name unknown|not found\"",
    "grep -Eq 'manifest unknown|name unknown|not found'",
    "grep -Eq \"manifest unknown|name unknown|not found\"",
    "executable file not found",
):
    if forbidden in preflight_run:
        raise SystemExit(f"preflight step must not allow generic not-found handling: {forbidden}")
for forbidden in ('cat "$', 'printf \'%s\' "$inspect_output"', 'echo "$inspect_output"'):
    if forbidden in preflight_run:
        raise SystemExit("preflight step must not print raw registry errors")


def verify_preflight_classifier(preflight_shell: str) -> None:
    release_tag = "v1.2.3"
    web_image = f"ghcr.io/s450586793/shunda-finance-web:{release_tag}"
    updater_image = f"ghcr.io/s450586793/shunda-finance-updater:{release_tag}"

    def run_preflight_case(fixtures: dict[str, list[dict[str, object]]]) -> subprocess.CompletedProcess[str]:
        rendered_shell = preflight_shell.replace("${{ steps.release.outputs.release_tag }}", release_tag)
        with tempfile.TemporaryDirectory(prefix="release-preflight-") as tempdir:
            temp_root = Path(tempdir)
            bin_dir = temp_root / "bin"
            bin_dir.mkdir()
            fixture_path = temp_root / "fixtures.json"
            fixture_path.write_text(json.dumps({"calls": fixtures}), encoding="utf-8")
            docker_path = bin_dir / "docker"
            docker_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "from __future__ import annotations",
                        "import json",
                        "import os",
                        "import sys",
                        "from pathlib import Path",
                        "",
                        "fixture_path = Path(os.environ['FAKE_DOCKER_FIXTURES'])",
                        "payload = json.loads(fixture_path.read_text(encoding='utf-8'))",
                        "argv = sys.argv[1:]",
                        "if argv[:3] != ['buildx', 'imagetools', 'inspect'] or len(argv) != 4:",
                        "    sys.stderr.write('unexpected docker invocation')",
                        "    raise SystemExit(97)",
                        "image = argv[3]",
                        "calls = payload['calls']",
                        "entries = calls.get(image)",
                        "if not entries:",
                        "    sys.stderr.write(f'unexpected image: {image}')",
                        "    raise SystemExit(98)",
                        "entry = entries.pop(0)",
                        "fixture_path.write_text(json.dumps(payload), encoding='utf-8')",
                        "sys.stdout.write(str(entry.get('stdout', '')))",
                        "sys.stderr.write(str(entry.get('stderr', '')))",
                        "raise SystemExit(int(entry['exit_code']))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            docker_path.chmod(0o755)
            script_path = temp_root / "preflight.sh"
            script_path.write_text(
                "#!/usr/bin/env sh\nset -eu\n" + rendered_shell + "\n",
                encoding="utf-8",
            )
            script_path.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["FAKE_DOCKER_FIXTURES"] = str(fixture_path)
            return subprocess.run(
                ["sh", str(script_path)],
                cwd=temp_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise SystemExit(message)

    def assert_success(name: str, fixtures: dict[str, list[dict[str, object]]]) -> None:
        result = run_preflight_case(fixtures)
        require(result.returncode == 0, f"{name} should pass but failed: {result.stderr.strip()}")
        require(result.stdout == "", f"{name} should not emit stdout")
        require(result.stderr == "", f"{name} should not emit raw registry errors")

    def assert_failure(
        name: str,
        fixtures: dict[str, list[dict[str, object]]],
        expected_message: str,
        forbidden_fragments: tuple[str, ...],
    ) -> None:
        result = run_preflight_case(fixtures)
        require(result.returncode != 0, f"{name} should fail closed")
        stderr = result.stderr.strip()
        require(stderr == expected_message, f"{name} must emit only safe fixed error, got: {stderr!r}")
        require(result.stdout == "", f"{name} should not emit stdout")
        for fragment in forbidden_fragments:
            require(fragment not in stderr, f"{name} leaked raw registry detail: {fragment}")

    assert_success(
        "anchored not-found references",
        {
            web_image: [{"exit_code": 1, "stderr": f"{web_image}: not found\n"}],
            updater_image: [{"exit_code": 1, "stderr": f"{updater_image}: not found\n"}],
        },
    )
    assert_success(
        "manifest unknown",
        {
            web_image: [{"exit_code": 1, "stderr": "manifest unknown: manifest unknown\n"}],
            updater_image: [{"exit_code": 1, "stderr": "manifest unknown: manifest unknown\n"}],
        },
    )
    assert_success(
        "name unknown",
        {
            web_image: [{"exit_code": 1, "stderr": "name unknown: name unknown\n"}],
            updater_image: [{"exit_code": 1, "stderr": "name unknown: name unknown\n"}],
        },
    )

    unsafe_cases = {
        "generic not-found": "not found\n",
        "executable file not found": "error: executable file not found in $PATH\n",
        "credential helper missing": "error getting credentials - err: exec: \"docker-credential-ghcr\": executable file not found in $PATH\n",
        "dns failure": "dial tcp: lookup ghcr.io on 127.0.0.53:53: no such host\n",
        "proxy failure": "proxyconnect tcp: dial tcp 127.0.0.1:8080: connect: connection refused\n",
        "authentication failure": "unauthorized: authentication required\n",
        "unknown error": "unexpected remote failure\n",
    }
    for case_name, raw_error in unsafe_cases.items():
        assert_failure(
            case_name,
            {
                web_image: [{"exit_code": 1, "stderr": raw_error}],
                updater_image: [{"exit_code": 1, "stderr": f"{updater_image}: not found\n"}],
            },
            "unable to verify immutable tag availability for web",
            (raw_error.strip(), "not found", "unauthorized", "lookup ghcr.io", "docker-credential-ghcr"),
        )

    for case_name, raw_error in (
        ("manifest unknown with transport failure", "manifest unknown: manifest unknown\ndial tcp: lookup ghcr.io: no such host\n"),
        ("name unknown with authentication failure", "name unknown: name unknown\nunauthorized: authentication required\n"),
    ):
        assert_failure(
            case_name,
            {
                web_image: [{"exit_code": 1, "stderr": raw_error}],
                updater_image: [{"exit_code": 1, "stderr": f"{updater_image}: not found\n"}],
            },
            "unable to verify immutable tag availability for web",
            ("manifest unknown", "name unknown", "dial tcp", "unauthorized"),
        )

    exists_error = "immutable tag already exists for web"
    assert_failure(
        "existing immutable tag",
        {
            web_image: [{"exit_code": 0, "stdout": "name: existing-tag\n"}],
            updater_image: [{"exit_code": 1, "stderr": f"{updater_image}: not found\n"}],
        },
        exists_error,
        ("name: existing-tag",),
    )


verify_preflight_classifier(preflight_run)

metadata_step = find_uses_step(publish_steps, "docker/metadata-action@v5")
if metadata_step is None:
    raise SystemExit("metadata step is required")
metadata_with = metadata_step.get("with")
if not isinstance(metadata_with, dict):
    raise SystemExit("metadata step must define inputs")
images = metadata_with.get("images")
if images not in (
    "${{ matrix.repository }}",
    "ghcr.io/s450586793/shunda-finance-${{ matrix.target }}",
):
    raise SystemExit("metadata images must target the matrix repository")
tags = metadata_with.get("tags")
if not isinstance(tags, str):
    raise SystemExit("metadata tags must be declared as text")
if "type=raw,value=${{ needs.preflight.outputs.release_tag }}" not in tags:
    raise SystemExit("metadata tags must include immutable release tag")
for forbidden in ("latest", "stable"):
    if forbidden in tags:
        raise SystemExit(f"publish metadata must not emit mutable tag {forbidden}")

build_step = find_uses_step(publish_steps, "docker/build-push-action@v6")
if build_step is None:
    raise SystemExit("build step is required")
build_with = build_step.get("with")
if not isinstance(build_with, dict):
    raise SystemExit("build step must define inputs")
if build_with.get("target") != "${{ matrix.target }}":
    raise SystemExit("build target must follow the matrix target")
if build_with.get("push") is not True:
    raise SystemExit("build step must push images")
build_args = build_with.get("build-args")
labels = build_with.get("labels")
if not isinstance(build_args, str) or not isinstance(labels, str):
    raise SystemExit("build args and labels must be text blocks")
for value in (
    "SHUNDA_RELEASE_VERSION=${{ needs.preflight.outputs.release_tag }}",
    "SHUNDA_RELEASE_REVISION=${{ github.sha }}",
    "SHUNDA_RELEASE_CREATED=${{ needs.preflight.outputs.release_created }}",
):
    if value not in build_args:
        raise SystemExit(f"missing build arg: {value}")
for value in (
    "org.opencontainers.image.version=${{ needs.preflight.outputs.release_tag }}",
    "org.opencontainers.image.revision=${{ github.sha }}",
    "org.opencontainers.image.created=${{ needs.preflight.outputs.release_created }}",
):
    if value not in labels:
        raise SystemExit(f"missing OCI label: {value}")

if promote_job.get("needs") != ["publish"]:
    raise SystemExit("promote-stable must need the full publish matrix")
promote_if = promote_job.get("if")
if not isinstance(promote_if, str):
    raise SystemExit("promote-stable must define an if expression")
for fragment in (
    "github.event_name == 'push'",
    "github.ref_type == 'tag'",
    "github.event.deleted != true",
    "startsWith(github.ref, 'refs/tags/v')",
):
    if fragment not in promote_if:
        raise SystemExit(f"promote-stable gate must include {fragment}")

promote_steps = promote_job.get("steps")
if not isinstance(promote_steps, list):
    raise SystemExit("promote-stable must define steps")

for action in (
    "docker/setup-buildx-action@v3",
    "docker/login-action@v3",
):
    if find_uses_step(promote_steps, action) is None:
        raise SystemExit(f"missing promote action: {action}")

promote_runs = find_run_steps(promote_steps, "docker buildx imagetools create")
if len(promote_runs) != 1:
    raise SystemExit("promote-stable must create exactly one mutable tag")
promote_run = promote_runs[0]
if "ghcr.io/s450586793/shunda-finance-web:stable" not in promote_run:
    raise SystemExit("promote-stable must only publish the web stable tag")
if "shunda-finance-updater" in promote_run or ":latest" in promote_run:
    raise SystemExit("promote-stable must not mutate updater or latest tags")
if "GITHUB_REF_NAME" not in promote_run and "steps.release.outputs.release_tag" not in promote_run:
    if "needs.preflight.outputs.release_tag" not in promote_run:
        raise SystemExit("promote-stable must target the current immutable release tag")

promote_concurrency = promote_job.get("concurrency")
if not isinstance(promote_concurrency, dict):
    raise SystemExit("promote-stable must serialize stable promotion")
promote_group = promote_concurrency.get("group")
if not isinstance(promote_group, str) or "github.repository" not in promote_group:
    raise SystemExit("promote-stable concurrency group must be repository scoped")
if any(fragment in promote_group for fragment in ("github.ref", "github.ref_name", "release_tag")):
    raise SystemExit("promote-stable concurrency group must not include release tag or ref")
if promote_concurrency.get("cancel-in-progress") is not False:
    raise SystemExit("promote-stable concurrency must not cancel queued releases")


def verify_promotion_guard(promote_shell: str) -> None:
    candidate_tag = "v1.2.4"
    stable_image = "ghcr.io/s450586793/shunda-finance-web:stable"
    rendered_shell = (
        promote_shell
        .replace("${{ needs.preflight.outputs.release_tag }}", candidate_tag)
        .replace("${{ github.ref_name }}", candidate_tag)
    )

    def run_case(entries: list[dict[str, object]], release_tag: str = candidate_tag) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory(prefix="release-promote-") as tempdir:
            temp_root = Path(tempdir)
            bin_dir = temp_root / "bin"
            bin_dir.mkdir()
            fixture_path = temp_root / "fixtures.json"
            fixture_path.write_text(json.dumps({"entries": entries, "calls": []}), encoding="utf-8")
            docker_path = bin_dir / "docker"
            docker_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "import os",
                        "import sys",
                        "from pathlib import Path",
                        "path = Path(os.environ['FAKE_DOCKER_FIXTURES'])",
                        "payload = json.loads(path.read_text(encoding='utf-8'))",
                        "argv = sys.argv[1:]",
                        "payload['calls'].append(argv)",
                        "if argv[:3] == ['buildx', 'imagetools', 'inspect']:",
                        "    if not payload['entries']:",
                        "        sys.stderr.write('unexpected inspect')",
                        "        raise SystemExit(97)",
                        "    entry = payload['entries'].pop(0)",
                        "    path.write_text(json.dumps(payload), encoding='utf-8')",
                        "    sys.stdout.write(str(entry.get('stdout', '')))",
                        "    sys.stderr.write(str(entry.get('stderr', '')))",
                        "    raise SystemExit(int(entry['exit_code']))",
                        "if argv[:3] == ['buildx', 'imagetools', 'create']:",
                        "    path.write_text(json.dumps(payload), encoding='utf-8')",
                        "    raise SystemExit(0)",
                        "sys.stderr.write('unexpected docker invocation')",
                        "raise SystemExit(98)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            docker_path.chmod(0o755)
            script_path = temp_root / "promote.sh"
            script_path.write_text("#!/usr/bin/env sh\nset -eu\n" + rendered_shell + "\n", encoding="utf-8")
            script_path.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["FAKE_DOCKER_FIXTURES"] = str(fixture_path)
            environment["GITHUB_REF_NAME"] = release_tag
            result = subprocess.run(
                ["sh", str(script_path)],
                cwd=temp_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = json.loads(fixture_path.read_text(encoding="utf-8"))["calls"]
            return result, calls

    def assert_outcome(
        name: str,
        entries: list[dict[str, object]],
        success: bool,
        message: str | None = None,
        release_tag: str = candidate_tag,
    ) -> None:
        result, calls = run_case(entries, release_tag)
        candidate_image = f"ghcr.io/s450586793/shunda-finance-web:{release_tag}"
        if success:
            if result.returncode != 0:
                raise SystemExit(f"{name} should pass but failed: {result.stderr.strip()}")
            creates = [call for call in calls if call[:3] == ["buildx", "imagetools", "create"]]
            if len(creates) != 1 or candidate_image not in creates[0] or stable_image not in creates[0]:
                raise SystemExit(f"{name} must create only stable from immutable web candidate")
            if any("updater" in argument or ":latest" in argument for argument in creates[0]):
                raise SystemExit(f"{name} must not create updater or latest aliases")
            return
        if result.returncode == 0:
            raise SystemExit(f"{name} should fail closed")
        if result.stderr.strip() != message:
            raise SystemExit(f"{name} must emit only {message!r}, got {result.stderr.strip()!r}")
        if any(call[:3] == ["buildx", "imagetools", "create"] for call in calls):
            raise SystemExit(f"{name} must not create stable")

    absent = {"exit_code": 1, "stderr": "manifest unknown: manifest unknown\n"}
    assert_outcome("absent stable", [absent, absent], True)
    assert_outcome(
        "strict version increase",
        [{"exit_code": 0, "stdout": "v1.2.3\n"}, {"exit_code": 0, "stdout": "v1.2.3\n"}],
        True,
    )
    assert_outcome("older candidate", [{"exit_code": 0, "stdout": "v1.2.5\n"}], False, "candidate release is not newer than current stable")
    assert_outcome("equal candidate", [{"exit_code": 0, "stdout": "v1.2.4\n"}], False, "candidate release is not newer than current stable")
    assert_outcome("candidate leading zero", [absent], False, "candidate release version is invalid", "v01.2.4")
    for name, output in (("noncanonical stable", "1.2.3\n"), ("missing stable version", "\n"), ("ambiguous stable version", "v1.2.3\nv1.2.4\n")):
        assert_outcome(name, [{"exit_code": 0, "stdout": output}], False, "current stable OCI version is invalid")
    assert_outcome("stable leading zero", [{"exit_code": 0, "stdout": "v01.2.3\n"}], False, "current stable OCI version is invalid")
    large_candidate = "v" + "1" + "0" * 39 + ".2.3"
    large_stable = "v" + "9" * 39 + ".2.3"
    assert_outcome(
        "arbitrary-size numeric identifier",
        [{"exit_code": 0, "stdout": f"{large_stable}\n"}, {"exit_code": 0, "stdout": f"{large_stable}\n"}],
        True,
        release_tag=large_candidate,
    )
    assert_outcome("registry failure", [{"exit_code": 1, "stderr": "unauthorized: authentication required\n"}], False, "unable to verify current stable version")
    mixed_absence_error = "manifest unknown: manifest unknown\nunauthorized: authentication required\n"
    assert_outcome(
        "stable absence marker with authentication failure",
        [{"exit_code": 1, "stderr": mixed_absence_error}, {"exit_code": 1, "stderr": mixed_absence_error}],
        False,
        "unable to verify current stable version",
    )
    assert_outcome(
        "queued winner recheck",
        [{"exit_code": 0, "stdout": "v1.2.3\n"}, {"exit_code": 0, "stdout": "v1.2.4\n"}],
        False,
        "candidate release is not newer than current stable",
    )


verify_promotion_guard(promote_run)

readme_path = Path("README.md")
if not readme_path.is_file():
    raise SystemExit("README.md is required")
readme = readme_path.read_text(encoding="utf-8")
required_readme_snippets = (
    "ghcr.io/s450586793/shunda-finance-web",
    "ghcr.io/s450586793/shunda-finance-updater",
    "vX.Y.Z",
    "stable",
    "DSM",
    ".env",
)
for snippet in required_readme_snippets:
    if snippet not in readme:
        raise SystemExit(f"README.md must document {snippet}")
for forbidden in (
    "long-production-password",
    "uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu",
    "/volume4/docker/docker/shunda-finance",
):
    if forbidden in readme:
        raise SystemExit(f"README.md must not expose {forbidden}")

pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
if "PyYAML~=6.0" not in pyproject:
    raise SystemExit("pyproject.toml must add PyYAML~=6.0 to dev dependencies")
PY
