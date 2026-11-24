#!/usr/bin/env python3
"""Run the unmodified upstream SWEeper-Bench evaluation worker on local Docker.

The upstream runner (https://github.com/zlab-princeton/sweeper-bench) executes
each case inside a Modal VM: an ubuntu:24.04 image with Docker and the repo's
src/runtime/ directory, booted with runtime/install.sh, then driven by
runtime/worker.py reading /task.json. This script builds the same VM image and
runs it as a privileged Docker-in-Docker container instead of a Modal sandbox.
Nothing in src/runtime/ is changed; only the Modal transport is replaced.

Used for the pilot's original-side `baseline` (unpatched base commit) and
`reference` (upstream fix) phases.

Example:
    python scripts/run_upstream_local.py \
        --runner ~/sweeper-bench --dataset-file test.jsonl \
        --cases sweeper-001 --phases baseline reference \
        --reference-patch-dir patches/ --out runs/pilot
Credentials come from the environment, as in the upstream CLI: GHCR_TOKEN,
GHCR_USER, and either OPENAI_API_KEY (--auth api) or CODEX_AUTH_FILE pointing
at a Codex auth.json (--auth subscription).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

VM_DOCKERFILE = """\
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
    docker.io docker-buildx ca-certificates python3 python3-yaml git curl iptables iproute2 \\
    && rm -rf /var/lib/apt/lists/*
COPY runtime/ /runtime/
"""


def sh(args, **kw):
    print("+", " ".join(str(a) for a in args), flush=True)
    return subprocess.run(args, **kw)


def build_vm_image(runner: Path, tag: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        (ctx / "Dockerfile").write_text(VM_DOCKERFILE)
        sh(["cp", "-a", str(runner / "src" / "runtime"), str(ctx / "runtime")], check=True)
        sh(["docker", "build", "-q", "-t", tag, str(ctx)], check=True)


def eval_payload(row: dict, args) -> dict:
    """Same shape as Backend._exec_case() in upstream src/modal_vm.py."""
    return {
        "stage": "evaluation",
        "auth": args.auth,
        "row": {  # upstream src/data.py:eval_row()
            "id": row["case_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "reference_commit": row.get("reference_commit") or "",
            "workflows": row["workflows"],
        },
        "prediction": {
            "harness": "codex",
            "provider": "openai",
            "model": "gpt-6-astra",
            "effort": "xhigh",
            "level": "scoped",
            "time_budget": "unlimited",
            "prompt_templates": "browser",
        },
        "evaluation": {
            "harness": args.eval_harness,
            "provider": "openai",
            "model": args.eval_model,
            "effort": args.eval_effort,
            "phases": args.phases,
            "categories": "target,preservation",
            "record": "0",
        },
    }


def vm_env(args) -> dict[str, str]:
    """Same variables as Backend.create() + host_credentials() upstream."""
    env = {
        "RUNTIME": "/runtime",
        "GHCR_REGISTRY": "ghcr.io",
        "GHCR_OWNER": "evigbyen",
        "GHCR_TAG": "alpha",
        "GHCR_USER": os.environ.get("GHCR_USER", "evigbyen"),
        "GHCR_TOKEN": os.environ["GHCR_TOKEN"],
        "PRED_EGRESS": "1",
        "EVAL_DROP_CACHES": "1",
        "EVAL_SKIP_FAILED_TASKS": "1",
    }
    if args.auth == "subscription":
        env["CODEX_AUTH_JSON"] = Path(os.environ["CODEX_AUTH_FILE"]).expanduser().read_text()
    else:
        env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
    return env


def run_case(row: dict, args, vm_tag: str) -> dict:
    cid = row["case_id"]
    dest = Path(args.out) / cid
    dest.mkdir(parents=True, exist_ok=True)
    name = f"sweeper-upstream-vm-{cid}-{os.getpid()}"
    env = vm_env(args)
    envfile = dest / ".vm.env"
    envfile.touch(mode=0o600)
    envfile.write_text("".join(f"{k}={v}\n" for k, v in env.items() if "\n" not in v))
    multiline = {k: v for k, v in env.items() if "\n" in v}
    started = time.time()
    try:
        sh(["docker", "rm", "-f", name], capture_output=True)
        sh(
            [
                "docker", "run", "-d", "--privileged", "--name", name,
                "--cpus", str(args.cpus), "--memory", f"{args.memory_mb}m",
                "--env-file", str(envfile),
                "-v", f"sweeper-upstream-docker-{cid}:/var/lib/docker",
                vm_tag, "sleep", "infinity",
            ],
            check=True,
        )
        envfile.unlink()
        # Multi-line values (the Codex auth JSON) cannot go through --env-file.
        for key, value in multiline.items():
            sh(["docker", "exec", "-i", name, "bash", "-c", f"cat > /run/{key}"], input=value.encode(), check=True)
        prefix = " ".join(f'{k}="$(cat /run/{k})"' for k in multiline)
        with (dest / "boot.log").open("wb") as log:
            sh(["docker", "exec", name, "bash", "-c", f"{prefix} bash /runtime/install.sh"], stdout=log, stderr=subprocess.STDOUT, check=True)
        sh(["docker", "exec", name, "bash", "-lc", "rm -rf /incoming /artifacts.tgz /task.json && mkdir -p /incoming"], check=True)
        sh(["docker", "exec", "-i", name, "bash", "-c", "cat > /task.json"], input=json.dumps(eval_payload(row, args), ensure_ascii=False).encode(), check=True)
        if "reference" in args.phases:
            patch = Path(args.reference_patch_dir) / f"{cid}.patch"
            sh(["docker", "exec", "-i", name, "bash", "-c", "cat > /incoming/reference.patch"], input=patch.read_bytes(), check=True)
        with (dest / "worker.stdout.log").open("wb") as out, (dest / "worker.stderr.log").open("wb") as err:
            proc = sh(
                ["timeout", str(args.timeout), "docker", "exec", name, "bash", "-c", f"{prefix} python3 /runtime/worker.py"],
                stdout=out, stderr=err,
            )
        art = dest / "artifacts.tgz"
        got = sh(["docker", "cp", f"{name}:/artifacts.tgz", str(art)])
        if got.returncode == 0:
            with tarfile.open(art) as tar:
                tar.extractall(dest / "artifacts", filter="data")
        status = {"case": cid, "worker_exit": proc.returncode, "seconds": round(time.time() - started)}
    finally:
        envfile.unlink(missing_ok=True)
        if not args.keep_vm:
            sh(["docker", "rm", "-f", name], capture_output=True)
    for phase in args.phases:
        result = dest / "artifacts" / "logs" / f"{phase}-result.json"
        cells = {}
        if result.is_file():
            data = json.loads(result.read_text())
            for cat in ("target", "preservation"):
                cell = data.get(cat) or {}
                cells[cat] = {k: cell.get(k) for k in ("pass", "fail", "uncertain")}
        status[phase] = cells
    (dest / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status), flush=True)
    return status


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runner", type=Path, required=True, help="checkout of zlab-princeton/sweeper-bench")
    p.add_argument("--dataset-file", type=Path, required=True)
    p.add_argument("--cases", nargs="+", required=True)
    p.add_argument("--phases", nargs="+", default=["baseline", "reference"])
    p.add_argument("--reference-patch-dir", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--auth", choices=["api", "subscription"], default="api")
    p.add_argument("--eval-harness", default="codex")
    p.add_argument("--eval-model", default="gpt-5.6-luna")
    p.add_argument("--eval-effort", default="xhigh")
    p.add_argument("--cpus", type=int, default=8)
    p.add_argument("--memory-mb", type=int, default=16384)
    p.add_argument("--timeout", type=int, default=10800)
    p.add_argument("--keep-vm", action="store_true")
    args = p.parse_args()

    rows = {json.loads(l)["case_id"]: json.loads(l) for l in args.dataset_file.read_text().splitlines() if l.strip()}
    commit = subprocess.run(["git", "-C", str(args.runner), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"upstream runner commit {commit}", flush=True)
    vm_tag = f"sweeper-upstream-vm:{commit[:12] or 'local'}"
    build_vm_image(args.runner, vm_tag)
    results = [run_case(rows[c], args, vm_tag) for c in args.cases]
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "summary.json").write_text(json.dumps({"runner_commit": commit, "results": results}, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
