"""Actions state handoff. Never silently create a baseline after a lost backup."""

import argparse
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx

from .db import connect, enqueue, init_db, settings
from .state import MAX_STATE_BYTES, pack_state, restore_state, verify_state
from .worker import after, worker

STATE_PREFIX = "ddt-state-"


class GitHub:
    def __init__(self):
        self.repository = os.environ["GITHUB_REPOSITORY"]
        self.client = httpx.Client(
            base_url=f"https://api.github.com/repos/{self.repository}/",
            headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            timeout=60, follow_redirects=True,
        )

    def request(self, path, method="GET", **kwargs):
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            # Do not publish response bodies or temporary signed URLs to logs.
            raise RuntimeError(f"GitHub API 操作失败：HTTP {response.status_code}")
        return response

    def pages(self, path, key, **params):
        page = 1
        while True:
            rows = self.request(path, params={**params, "per_page": 100, "page": page}).json()[key]
            yield from rows
            if len(rows) < 100:
                return
            page += 1

    def state_artifacts(self):
        return [a for a in self.pages("actions/artifacts", "artifacts")
                if a["name"].startswith(STATE_PREFIX) and not a["expired"]]

    def run_artifacts(self, run_id):
        return [a for a in self.pages(f"actions/runs/{run_id}/artifacts", "artifacts")
                if a["name"].startswith(STATE_PREFIX) and not a["expired"]]

    def download(self, artifact):
        return self.request(f"actions/artifacts/{artifact['id']}/zip").content

    def previous_run(self, current_id, branch):
        for run in self.pages("actions/workflows/collect.yml/runs", "workflow_runs", branch=branch):
            if run["id"] < current_id and run.get("conclusion") != "skipped":
                return run
        return None


def unpack_bundle(content, target):
    """Read only the two expected members, with bounds; do not extract arbitrary paths."""
    if len(content) > MAX_STATE_BYTES + 1024 * 1024:
        raise ValueError("状态压缩包超过上限")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sorted(archive.namelist()) != ["manifest.json", "tracker.db"]:
            raise ValueError("状态压缩包必须仅包含 tracker.db 和 manifest.json")
        for name, limit in (("tracker.db", MAX_STATE_BYTES), ("manifest.json", 16 * 1024)):
            info = archive.getinfo(name)
            if info.file_size > limit:
                raise ValueError("状态压缩包成员超过上限")
            Path(target, name).write_bytes(archive.read(name))


def choose_artifact(previous, artifacts, operation):
    """A failed workflow can have a valid finalized state; conclusion is not the baseline."""
    candidates = sorted(artifacts, key=lambda a: a["id"], reverse=True)
    if not candidates:
        raise ValueError("没有有效状态备份；请导入备份或明确初始化，禁止自动重建基线")
    chosen = candidates[0]
    if operation != "recover":
        if not previous or previous["status"] != "completed":
            raise ValueError("上一轮未结束，拒绝开始采集")
        if chosen.get("workflow_run", {}).get("id") != previous["id"]:
            raise ValueError("上一轮未保存状态；检查日志后用 recover 恢复，不能静默回退")
    return chosen


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def restore_remote(github, operation, branch, seed_release=""):
    current_id = int(os.environ["GITHUB_RUN_ID"])
    if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
        raise ValueError("不重跑旧任务；请新建 collect/publish 或人工确认后 recover，避免重复采集")
    artifacts = github.state_artifacts()
    if operation == "initialize":
        if artifacts:
            raise ValueError("已经存在状态，拒绝重新初始化；请使用 collect/publish/recover")
        if seed_release:
            release = github.request("releases/tags/" + quote(seed_release, safe="")).json()
            assets = [a for a in release["assets"] if a["name"] == "state.zip"]
            if len(assets) != 1 or assets[0]["size"] > MAX_STATE_BYTES + 1024 * 1024:
                raise ValueError("种子 Release 必须包含一个不超过上限的 state.zip")
            content = github.request(f"releases/assets/{assets[0]['id']}",
                                     headers={"Accept": "application/octet-stream"}).content
            with tempfile.TemporaryDirectory() as temporary:
                unpack_bundle(content, temporary)
                restore_state(temporary)
        else:
            init_db()
        return None
    previous = github.previous_run(current_id, branch)
    chosen = choose_artifact(previous, artifacts, operation)
    with tempfile.TemporaryDirectory() as temporary:
        unpack_bundle(github.download(chosen), temporary)
        manifest = restore_state(temporary, chosen["workflow_run"]["id"])
    if operation == "recover":
        # Recovery may have lost a failed attempt; require a fresh conservative cooldown.
        with connect() as conn:
            row = conn.execute("SELECT cooldown_until FROM runtime WHERE id=1").fetchone()
            until = max(row[0] or "", after(max(settings()["cooldown_minutes"], 60) * 60))
            conn.execute("UPDATE runtime SET cooldown_until=?,last_error=? WHERE id=1",
                         (until, "人工恢复旧状态，本轮不采集；等待冷却后再继续"))
    return manifest


def collect_once():
    try:
        run = enqueue("scheduled" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "manual")
    except ValueError:
        set_output("result", "cooldown")
        set_output("failed", "false")
        return
    worker(once=True)
    with connect() as conn:
        status = conn.execute("SELECT status FROM crawl_runs WHERE id=?", (run["id"],)).fetchone()[0]
    if status in ("queued", "running"):
        raise ValueError("采集未结束，不能发布状态")
    set_output("result", status)
    set_output("failed", "false" if status == "success" else "true")


def verify_upload(github, artifact_id, expected):
    artifact = github.request(f"actions/artifacts/{artifact_id}").json()
    if artifact["expired"] or artifact["workflow_run"]["id"] != int(os.environ["GITHUB_RUN_ID"]):
        raise ValueError("上传状态所属工作流不匹配或已过期")
    with tempfile.TemporaryDirectory() as temporary:
        unpack_bundle(github.download(artifact), temporary)
        actual = verify_state(temporary, os.environ["GITHUB_RUN_ID"])
    if actual != expected:
        raise ValueError("上传状态与本轮备份不一致")


def cleanup(github, artifact_id):
    artifacts = sorted(github.state_artifacts(), key=lambda a: a["id"], reverse=True)
    if not artifacts or artifacts[0]["id"] != artifact_id:
        raise ValueError("出现更新的状态，拒绝清理")
    for artifact in artifacts[3:]:
        github.request(f"actions/artifacts/{artifact['id']}", method="DELETE")


def main():
    parser = argparse.ArgumentParser(description="Actions 数据库状态接力")
    subs = parser.add_subparsers(dest="command", required=True)
    restore = subs.add_parser("restore")
    restore.add_argument("--operation", choices=["initialize", "collect", "publish", "recover"], required=True)
    restore.add_argument("--branch", required=True)
    restore.add_argument("--seed-release", default="")
    subs.add_parser("collect")
    pack = subs.add_parser("pack")
    pack.add_argument("--output", type=Path, required=True)
    verify = subs.add_parser("verify-upload")
    verify.add_argument("--artifact-id", type=int, required=True)
    verify.add_argument("--source", type=Path, required=True)
    clean = subs.add_parser("cleanup")
    clean.add_argument("--artifact-id", type=int, required=True)
    args = parser.parse_args()
    if args.command == "collect":
        collect_once()
        return
    if args.command == "pack":
        pack_state(args.output, {"workflow_run_id": os.environ["GITHUB_RUN_ID"],
                                "workflow_run_attempt": os.environ["GITHUB_RUN_ATTEMPT"]})
        return
    github = GitHub()
    try:
        if args.command == "restore":
            restore_remote(github, args.operation, args.branch, args.seed_release)
            init_db()
            with connect() as conn:
                # Cron owns scheduling in Actions; do not start the resident scheduler.
                conf = settings()
                conf["enabled"] = False
                conn.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(conf),))
                conn.execute("UPDATE runtime SET heartbeat=NULL,next_run_at=NULL WHERE id=1")
        elif args.command == "verify-upload":
            verify_upload(github, args.artifact_id, verify_state(args.source))
        else:
            cleanup(github, args.artifact_id)
    finally:
        github.client.close()


if __name__ == "__main__":
    main()
