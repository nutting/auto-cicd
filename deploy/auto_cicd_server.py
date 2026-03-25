#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional


BASE_DIR = Path("/home/miao.jie/auto-cicd")
ARTIFACTS_DIR = BASE_DIR / "artifacts"
LOG_DIR = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / "state.json"
CONFIG_FILE = BASE_DIR / "config.json"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_cmd_text(cmd: List[str]) -> str:
    items = []
    for part in cmd:
        if "@" in part and "://" in part:
            left, right = part.split("@", 1)
            scheme, rest = left.split("://", 1)
            if ":" in rest:
                user = rest.split(":", 1)[0]
                part = "{}://{}:***@{}".format(scheme, user, right)
        items.append(part)
    return " ".join(items)


def safe_branch_name(branch_name: str) -> str:
    text = (branch_name or "").strip()
    if not text:
        return "default"
    return text.replace("/", "-").replace("\\", "-").replace(" ", "-")


def run_command(cmd: List[str], cwd: Optional[Path] = None) -> str:
    logging.info("Running command: %s (cwd=%s)", safe_cmd_text(cmd), cwd or os.getcwd())
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        check=True,
    )
    return result.stdout


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ProjectRunner(object):
    def __init__(self, server_config: Dict[str, Any], project_config: Dict[str, Any], state: Dict[str, Any]) -> None:
        self.server_config = server_config
        self.config = project_config
        self.name = project_config["name"]
        self.repo_dir = Path(project_config["repo_dir"])
        build_subdir = project_config.get("build_subdir", "")
        self.build_dir = self.repo_dir / build_subdir if build_subdir else self.repo_dir
        self.lock = threading.Lock()
        self.state = state.setdefault(
            self.name,
            {"last_seen_commit": "", "builds": [], "current_build": None},
        )

    def ensure_repo(self) -> None:
        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".git").exists():
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            run_command(["git", "init"], cwd=self.repo_dir)
        remotes = run_command(["git", "remote"], cwd=self.repo_dir)
        if "origin" not in remotes.split():
            run_command(["git", "remote", "add", "origin", self.config["repo_url_with_auth"]], cwd=self.repo_dir)
        self.sync_repo(self.default_branch())

    def default_branch(self) -> str:
        configured = self.config.get("branch", "")
        if configured:
            return configured
        output = run_command(["git", "ls-remote", "--symref", self.config["repo_url_with_auth"], "HEAD"])
        for line in output.splitlines():
            if line.startswith("ref: ") and "\tHEAD" in line:
                ref = line.split("\t", 1)[0].split(" ", 1)[1]
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/"):]
        heads = self.fetch_remote_heads()
        if heads:
            return sorted(heads.keys())[0]
        raise RuntimeError("No remote branches found for {}".format(self.name))

    def sync_repo(self, branch_name: str) -> str:
        run_command(["git", "fetch", "origin"], cwd=self.repo_dir)
        run_command(
            ["git", "checkout", "-B", branch_name, "origin/{}".format(branch_name)],
            cwd=self.repo_dir,
        )
        run_command(["git", "reset", "--hard", "origin/{}".format(branch_name)], cwd=self.repo_dir)
        return run_command(["git", "rev-parse", "HEAD"], cwd=self.repo_dir).strip()

    def fetch_remote_heads(self) -> Dict[str, str]:
        output = run_command(["git", "ls-remote", "--heads", self.config["repo_url_with_auth"]])
        heads = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
                continue
            heads[parts[1][len("refs/heads/"):]] = parts[0]
        return heads

    def fetch_remote_head(self, branch_name: Optional[str] = None) -> str:
        active_branch = branch_name or self.default_branch()
        output = run_command(["git", "ls-remote", self.config["repo_url_with_auth"], active_branch])
        return output.split()[0]

    def apply_text_replacements(self) -> None:
        replacements = self.config.get("text_replacements", [])
        for item in replacements:
            relative_path = item.get("path", "")
            search_text = item.get("search", "")
            replace_text = item.get("replace", "")
            if not relative_path or search_text == "":
                continue
            file_path = self.build_dir / relative_path
            if not file_path.exists():
                raise FileNotFoundError("Replacement target not found: {}".format(file_path))
            content = file_path.read_text(encoding="utf-8")
            if search_text not in content:
                raise ValueError("Replacement text not found in {}".format(file_path))
            file_path.write_text(content.replace(search_text, replace_text, 1), encoding="utf-8")

    def recent_builds(self) -> List[Dict[str, Any]]:
        return list(reversed(self.state.get("builds", [])[-4:]))

    def latest_build(self) -> Optional[Dict[str, Any]]:
        builds = self.state.get("builds", [])
        return builds[-1] if builds else None

    def current_build(self) -> Optional[str]:
        return self.state.get("current_build")

    def fetch_commit_subject(self, commit_id: str) -> str:
        if not commit_id:
            return ""
        return run_command(["git", "log", "-1", "--pretty=%s", commit_id], cwd=self.repo_dir).strip()

    def fetch_commit_meta(self, commit_id: str) -> Dict[str, str]:
        if not commit_id:
            return {"commit_message": "", "commit_author": "", "commit_time": ""}
        return {
            "commit_message": run_command(["git", "log", "-1", "--pretty=%s", "HEAD"], cwd=self.repo_dir).strip(),
            "commit_author": run_command(["git", "log", "-1", "--pretty=%an", "HEAD"], cwd=self.repo_dir).strip(),
            "commit_time": run_command(["git", "log", "-1", "--pretty=%ci", "HEAD"], cwd=self.repo_dir).strip(),
        }

    def poll_once(self) -> Optional[Dict[str, str]]:
        active_branch = self.default_branch()
        branch_commits = self.state.setdefault("branch_commits", {})
        remote_commit = self.fetch_remote_head(active_branch)
        last_seen = branch_commits.get(active_branch, self.state.get("last_seen_commit", ""))
        if remote_commit and remote_commit != last_seen:
            logging.info("Detected new commit for %s/%s: %s -> %s", self.name, active_branch, last_seen, remote_commit)
            return {"branch": active_branch, "commit": remote_commit}
        return None

    def should_handle_webhook(self, payload: Dict[str, Any]) -> bool:
        return bool(self.extract_branch_from_webhook(payload))

    def extract_branch_from_webhook(self, payload: Dict[str, Any]) -> str:
        ref = payload.get("ref", "")
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):]
        if payload.get("branch"):
            return str(payload.get("branch"))
        head_commit = payload.get("head_commit") or {}
        if head_commit.get("ref") and str(head_commit["ref"]).startswith("refs/heads/"):
            return str(head_commit["ref"])[len("refs/heads/"):]
        return ""

    def extract_commit_from_webhook(self, payload: Dict[str, Any]) -> str:
        after = payload.get("after", "")
        if after and after != "0000000000000000000000000000000000000000":
            return after
        head_commit = payload.get("head_commit") or {}
        return head_commit.get("id", "")

    def build(self, expected_commit: Optional[str], triggered_by: str, branch_name: Optional[str] = None) -> Dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Project {} already has a running build".format(self.name))

        active_branch = branch_name or self.default_branch()
        build_time = datetime.now().strftime("%Y%m%d%H%M%S")
        log_file = LOG_DIR / "{}-build-{}.log".format(self.name, build_time)
        record = {
            "project": self.name,
            "build_id": build_time,
            "branch": active_branch,
            "commit": expected_commit or "",
            "commit_message": "",
            "commit_author": "",
            "commit_time": "",
            "status": "running",
            "started_at": now_text(),
            "finished_at": "",
            "artifact_name": None,
            "artifact_url": None,
            "log_file": str(log_file),
            "message": "Triggered by {}".format(triggered_by),
        }
        self.state["current_build"] = build_time
        self.state.setdefault("builds", []).append(record)

        try:
            with log_file.open("w", encoding="utf-8") as logf:
                remote_commit = self.sync_repo(active_branch)
                self.apply_text_replacements()
                commit_meta = self.fetch_commit_meta(remote_commit)
                self.update_build(build_time, commit=remote_commit, **commit_meta)
                if expected_commit and remote_commit != expected_commit:
                    logf.write("Expected commit: {}\n".format(expected_commit))
                    logf.write("Actual commit after sync: {}\n".format(remote_commit))
                    logf.flush()

                for cmd in self.build_commands():
                    process = subprocess.Popen(
                        cmd,
                        cwd=str(self.build_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                    )
                    assert process.stdout is not None
                    for line in process.stdout:
                        logf.write(line)
                    if process.wait() != 0:
                        raise RuntimeError("Command failed: {}".format(" ".join(cmd)))

                artifact_path = self.collect_artifact(build_time, active_branch)
                artifact_name = artifact_path.name
                artifact_relative = artifact_path.relative_to(ARTIFACTS_DIR)
                artifact_url = "{}/artifacts/{}".format(
                    self.server_config["public_base_url"],
                    "/".join([urllib.parse.quote(part) for part in artifact_relative.parts]),
                )
                self.update_build(
                    build_time,
                    status="success",
                    finished_at=now_text(),
                    commit=remote_commit,
                    **commit_meta,
                    artifact_name=artifact_name,
                    artifact_url=artifact_url,
                    message="Build succeeded, triggered by {}".format(triggered_by),
                )
                self.state["last_seen_commit"] = remote_commit
                self.state.setdefault("branch_commits", {})[active_branch] = remote_commit
                return self.latest_build() or {}
        except Exception as exc:
            logging.exception("Build failed for %s", self.name)
            self.update_build(build_time, status="failed", finished_at=now_text(), message=str(exc))
            raise
        finally:
            self.state["current_build"] = None
            self.lock.release()

    def build_commands(self) -> List[List[str]]:
        settings_xml = self.config.get("settings_xml", "")
        repo_local = self.config["maven_repo_local"]
        profiles = self.config.get("maven_profiles", [])
        commands = []
        pre_modules = self.config.get("pre_build_modules", [])
        base_cmd = ["mvn", "-o"]
        if settings_xml:
            base_cmd.extend(["-s", settings_xml])
        base_cmd.append("-Dmaven.repo.local={}".format(repo_local))
        if profiles:
            base_cmd.append("-P{}".format(",".join(profiles)))
        for module in pre_modules:
            commands.append(base_cmd + ["clean", "install", "-pl", module, "-am", "-DskipTests"])

        package_goal = self.config.get("package_goal", "module")
        if package_goal == "root":
            commands.append(base_cmd + ["clean", "package", "-DskipTests"])
        else:
            commands.append(
                base_cmd + ["clean", "package", "-pl", self.config["target_module"], "-am", "-DskipTests"]
            )
        return commands

    def collect_artifact(self, build_time: str, branch_name: str) -> Path:
        pattern = self.config.get("artifact_glob", "*.tar.gz")
        artifact_module = self.config.get("artifact_module", self.config.get("target_module", ""))
        target_dir = self.build_dir / artifact_module / "target" if artifact_module else self.build_dir / "target"
        candidates = sorted(target_dir.glob(pattern), key=lambda item: item.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError("No artifact matched {} in {}".format(pattern, target_dir))
        source = candidates[-1]
        project_artifact_dir = ARTIFACTS_DIR / self.name
        date_dir = build_time[:8]
        build_dir = project_artifact_dir / date_dir / build_time
        build_dir.mkdir(parents=True, exist_ok=True)
        copied_tar = build_dir / source.name
        shutil.copy2(str(source), str(copied_tar))
        zip_path = build_dir / "{}-{}.zip".format(build_time, safe_branch_name(branch_name))
        arcname = "{}-{}/{}".format(build_time, safe_branch_name(branch_name), source.name)
        with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.write(str(copied_tar), arcname=arcname)
        return zip_path

    def update_build(self, build_id: str, **changes: Any) -> None:
        for build in self.state.get("builds", []):
            if build["build_id"] == build_id:
                build.update(changes)
                return

    def send_notification(self, build: Optional[Dict[str, Any]]) -> None:
        notify = self.config.get("notify", {})
        webhook = notify.get("webhook", "")
        secret = notify.get("secret", "")
        title = notify.get("title", "自动打包结果")
        extra_lines = notify.get("extra_lines", [])
        mention = notify.get("at_mobiles", [])
        if not webhook or not secret or not build:
            return

        timestamp = str(int(time.time() * 1000))
        sign_str = "{}\n{}".format(timestamp, secret).encode("utf-8")
        sign = urllib.parse.quote_plus(
            base64.b64encode(hmac.new(secret.encode("utf-8"), sign_str, hashlib.sha256).digest())
        )
        request_url = "{}&timestamp={}&sign={}".format(webhook, timestamp, sign)
        lines = [
            title,
            "项目: {}".format(self.name),
            "分支: {}".format(build["branch"]),
            "提交: {}".format(build["commit"]),
            "备注: {}".format(build.get("commit_message") or "无"),
            "提交人: {}".format(build.get("commit_author") or "无"),
            "提交时间: {}".format(build.get("commit_time") or "无"),
            "状态: {}".format(build["status"]),
            "开始: {}".format(build["started_at"]),
            "结束: {}".format(build["finished_at"]),
            "说明: {}".format(build["message"]),
            "产物: {}".format(build.get("artifact_url") or "无"),
            "日志: {}/logs/{}".format(self.server_config["public_base_url"], Path(build["log_file"]).name),
        ]
        for line in extra_lines:
            lines.append(str(line))
        payload = {
            "msgtype": "text",
            "text": {"content": "\n".join(lines)},
            "at": {"atMobiles": mention, "isAtAll": False},
        }
        req = urllib.request.Request(
            request_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            logging.info("DingTalk response for %s: %s", self.name, response.read().decode("utf-8"))


class AutoCICDServer(object):
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.state = self.load_state()
        self.projects = {}
        for project_config in config.get("projects", []):
            project = ProjectRunner(config, project_config, self.state.setdefault("projects", {}))
            self.projects[project.name] = project

    def load_state(self) -> Dict[str, Any]:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"projects": {}}

    def save_state(self) -> None:
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_repos(self) -> None:
        for project in self.projects.values():
            project.ensure_repo()
        self.save_state()

    def poll_projects_once(self) -> None:
        for name, project in self.projects.items():
            try:
                if project.current_build():
                    continue
                change = project.poll_once()
                if change:
                    self.trigger_build_async(name, change["commit"], "poller", change["branch"])
            except Exception:
                logging.error("Polling failed for %s\n%s", name, traceback.format_exc())

    def handle_build(
        self,
        project_name: str,
        expected_commit: Optional[str],
        triggered_by: str,
        branch_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        project = self.projects[project_name]
        build = project.build(expected_commit=expected_commit, triggered_by=triggered_by, branch_name=branch_name)
        self.save_state()
        project.send_notification(build)
        return build

    def trigger_build_async(
        self,
        project_name: str,
        expected_commit: Optional[str],
        triggered_by: str,
        branch_name: Optional[str] = None,
    ) -> None:
        def worker() -> None:
            try:
                self.handle_build(project_name, expected_commit, triggered_by, branch_name=branch_name)
            except Exception:
                self.save_state()
                project = self.projects[project_name]
                project.send_notification(project.latest_build())
                logging.error("Async build failed\n%s", traceback.format_exc())

        threading.Thread(target=worker, daemon=True).start()

    def list_builds(self) -> Dict[str, Any]:
        data = {}
        for name, project in self.projects.items():
            data[name] = {
                "current_build": project.current_build(),
                "latest_build": project.latest_build(),
                "builds": project.recent_builds(),
            }
        return data


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "AutoCICD/2.0"

    @property
    def app(self) -> AutoCICDServer:
        return self.server.app  # type: ignore[attr-defined]

    def do_HEAD(self) -> None:
        if self.path.startswith("/artifacts/") or self.path.startswith("/logs/"):
            self._serve_file_head()
            return
        self.send_response(HTTPStatus.OK)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._serve_index()
            return
        if self.path == "/api/builds":
            self._json_response(self.app.list_builds())
            return
        if self.path.startswith("/artifacts/"):
            self._serve_file_data()
            return
        if self.path.startswith("/logs/"):
            self._serve_file_data()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/build/"):
            project_name = path.split("/")[-1]
            if project_name not in self.app.projects:
                self.send_error(HTTPStatus.NOT_FOUND, "Project not found")
                return
            self.app.trigger_build_async(project_name, None, "manual-api")
            self._json_response({"message": "Build started", "project": project_name})
            return
        if path.startswith("/webhook/"):
            project_name = path.split("/")[-1]
            if project_name not in self.app.projects:
                self.send_error(HTTPStatus.NOT_FOUND, "Project not found")
                return
            payload = self._read_json_body()
            project = self.app.projects[project_name]
            if not project.should_handle_webhook(payload):
                self._json_response({"message": "Ignored by branch filter", "project": project_name})
                return
            commit = project.extract_commit_from_webhook(payload)
            branch_name = project.extract_branch_from_webhook(payload)
            self.app.trigger_build_async(project_name, commit, "webhook", branch_name=branch_name)
            self._json_response(
                {"message": "Webhook accepted", "project": project_name, "commit": commit, "branch": branch_name}
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _serve_index(self) -> None:
        cards = []
        for name, project in self.app.projects.items():
            latest = project.latest_build()
            rows = []
            for build in project.recent_builds():
                artifact = build.get("artifact_url")
                artifact_html = '<a href="{}">下载</a>'.format(artifact) if artifact else "-"
                log_name = Path(build["log_file"]).name
                rows.append(
                    "<tr>"
                    "<td>{}</td><td>{}<br><span class=\"sub\">分支: {}</span><br><span class=\"sub\">{}</span><br><span class=\"sub\">{}</span><br><span class=\"sub\">{}</span></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><a href=\"/logs/{}\">日志</a></td>"
                    "</tr>".format(
                        build["build_id"],
                        build["status"],
                        build.get("branch") or "-",
                        build.get("commit_message") or "-",
                        build.get("commit_author") or "-",
                        build.get("commit_time") or "-",
                        build["commit"][:12],
                        build["started_at"],
                        build["finished_at"],
                        artifact_html,
                        log_name,
                    )
                )
            cards.append(
                """
                <section class="card">
                  <div class="card-head">
                    <div>
                      <h2>{name}</h2>
                      <p>触发分支: 任意分支</p>
                      <p>Webhook: <code>{base}/webhook/{name}</code></p>
                      <p>当前构建: {current}</p>
                      <p>最近状态: {status}</p>
                    </div>
                    <button onclick="fetch('/api/build/{name}', {{method:'POST'}}).then(() => location.reload())">手动触发</button>
                  </div>
                  <table>
                    <thead>
                      <tr><th>构建号</th><th>状态 / 提交信息</th><th>提交</th><th>开始时间</th><th>结束时间</th><th>产物</th><th>日志</th></tr>
                    </thead>
                    <tbody>
                      {rows}
                    </tbody>
                  </table>
                </section>
                """.format(
                    name=name,
                    base=self.app.config["public_base_url"],
                    current=project.current_build() or "空闲",
                    status=latest["status"] if latest else "暂无构建",
                    rows="".join(rows) if rows else '<tr><td colspan="7">暂无构建记录</td></tr>',
                )
            )
        html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Auto CICD</title>
  <style>
    body { font-family: sans-serif; padding: 24px; background: #eef3f7; color: #1f2937; }
    .hero { background: #ffffff; border-radius: 16px; padding: 24px; margin-bottom: 20px; box-shadow: 0 8px 28px rgba(15, 23, 42, 0.08); }
    .card { background: #ffffff; border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 8px 28px rgba(15, 23, 42, 0.08); }
    .card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }
    button { background: #0f62fe; color: #fff; border: 0; padding: 10px 14px; border-radius: 8px; cursor: pointer; }
    code { background: #eef2ff; padding: 2px 6px; border-radius: 6px; }
    .sub { color: #6b7280; font-size: 12px; }
  </style>
</head>
<body>
  <div class="hero">
    <h1>自动打包系统</h1>
    <p>支持多项目、Webhook 触发、手动触发、产物下载和钉钉通知。</p>
  </div>
  __CARDS__
</body>
</html>""".replace("__CARDS__", "".join(cards))
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _resolve_file(self) -> Optional[Path]:
        if self.path.startswith("/artifacts/"):
            rel = self.path[len("/artifacts/"):]
            safe = Path(urllib.parse.unquote(rel))
            parts = [part for part in safe.parts if part not in ("", ".", "..")]
            if len(parts) < 2:
                return None
            return ARTIFACTS_DIR.joinpath(*parts)
        if self.path.startswith("/logs/"):
            name = Path(urllib.parse.unquote(self.path[len("/logs/"):])).name
            return LOG_DIR / name
        return None

    def _serve_file_head(self) -> None:
        file_path = self._resolve_file()
        if not file_path or not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()

    def _serve_file_data(self) -> None:
        file_path = self._resolve_file()
        if not file_path or not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(str(LOG_DIR / "server.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    app = AutoCICDServer(config)
    app.ensure_repos()

    poll_interval = int(config.get("poll_interval_seconds", 60))

    def poll_loop() -> None:
        while True:
            app.poll_projects_once()
            time.sleep(poll_interval)

    if poll_interval > 0:
        threading.Thread(target=poll_loop, daemon=True).start()

    port = int(config.get("listen_port", 8088))
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    server.app = app  # type: ignore[attr-defined]
    logging.info("Server started at port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
