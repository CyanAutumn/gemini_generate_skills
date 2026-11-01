"""Run portable image/video workflows through the existing Gemini UI driver."""

from __future__ import annotations

import re
import uuid
from contextlib import suppress
from pathlib import Path

from browser_session import BrowserSession, emit, redact
from local_state import RunStore, read_json, utc_now, verify_image, verify_saved_files, write_json


def workflow_record_path(output_dir, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("无效的工作流运行 ID")
    return Path(output_dir).expanduser().resolve() / "workflow-runs" / run_id / "record.json"


def load_workflow_result(output_dir, run_id, verify=True):
    path = workflow_record_path(output_dir, run_id)
    record = read_json(path)
    if not isinstance(record, dict) or record.get("id") != run_id:
        raise ValueError("工作流运行记录与 ID 不匹配")
    if verify:
        for step in record.get("steps", []):
            verify_saved_files(step.get("files", []), step["type"])
    return record


class WorkflowRunner:
    def __init__(self, definition, output_dir, profile_dir, proxy=None,
                 session_factory=None, driver_factory=None):
        self.definition = definition
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.proxy = proxy
        self.session_factory = session_factory or BrowserSession
        self.driver_factory = driver_factory
        self.last_record = None

    def plan(self, parameters=None):
        return self.definition.plan(parameters)

    def _save(self):
        self.last_record["updated_at"] = utc_now()
        write_json(workflow_record_path(self.output_dir, self.last_record["id"]), self.last_record)

    @staticmethod
    def _references(specifications, completed):
        resolved = []
        for reference in specifications:
            if "step" in reference:
                previous = completed[reference["step"]]
                index = reference.get("file_index", 0)
                if index >= len(previous):
                    raise ValueError(f"前序步骤 {reference['step']} 没有第 {index} 个输出")
                reference = previous[index]
            checked = verify_image(reference["path"])
            if reference.get("sha256") and reference["sha256"] != checked["sha256"]:
                raise ValueError("参考图在工作流预检后发生变化，已停止执行")
            resolved.append(checked)
        return resolved

    @staticmethod
    def _copy_task_state(entry, task):
        entry.update(task_status=task["status"], files=task.get("files", []),
                     chat_cleanup=task.get("chat_cleanup"))

    def run(self, parameters=None):
        # Resolve every prompt, parameter and fixed reference before opening a
        # browser. A bad later step must not consume a successful earlier step.
        self.last_record = None
        plan = self.plan(parameters)
        now = utc_now()
        self.last_record = {
            "id": uuid.uuid4().hex, "workflow_name": self.definition.name,
            "workflow_directory": str(self.definition.directory),
            "workflow": self.definition.manifest, "parameters": plan["parameters"],
            "profile": str(self.profile_dir), "status": "running", "steps": [], "files": [],
            "created_at": now, "updated_at": now,
        }
        self._save()
        path = workflow_record_path(self.output_dir, self.last_record["id"])
        emit("workflow_started", run_id=self.last_record["id"], name=self.definition.name,
             record=str(path), step_count=len(plan["steps"]))
        completed = {}
        active_entry = active_task = None
        try:
            with self.session_factory(self.profile_dir, proxy=self.proxy) as session:
                session.navigate()
                session.ensure_login(900)
                if self.driver_factory is None:
                    from gemini_web import GeminiImages
                    driver_factory = GeminiImages
                else:
                    driver_factory = self.driver_factory
                for step in plan["steps"]:
                    active_entry = active_task = None
                    references = self._references(step["references"], completed)
                    step_directory = path.parent / "steps" / step["id"]
                    store = RunStore(step_directory)
                    active_task = store.create(step["prompt"], self.profile_dir)
                    active_task.update(media_type=step["type"], references=references,
                                       workflow_run_id=self.last_record["id"], workflow_step=step["id"])
                    store.save(active_task)
                    active_entry = {
                        "id": step["id"], "type": step["type"], "status": "running",
                        "task_run_id": active_task["id"],
                        "task_record": str(store.path_for(active_task["id"])),
                        "output_dir": str(step_directory), "files": [],
                    }
                    self.last_record["steps"].append(active_entry)
                    self._save()
                    emit("workflow_step_started", run_id=self.last_record["id"],
                         step_id=step["id"], task_run_id=active_task["id"])
                    driver_factory(session, store).generate(active_task, step["timeout"])
                    self._copy_task_state(active_entry, active_task)
                    self._save()
                    if active_task["status"] != "succeeded":
                        raise RuntimeError(f"步骤 {step['id']} 没有成功保存结果，已停止后续步骤")
                    if active_task.get("chat_cleanup", {}).get("status") != "deleted":
                        raise RuntimeError(f"步骤 {step['id']} 的聊天尚未确认删除，已停止后续步骤")
                    if not active_task.get("files"):
                        raise RuntimeError(f"步骤 {step['id']} 没有输出文件")
                    files = verify_saved_files(active_task["files"], step["type"])
                    active_entry.update(status="succeeded", files=files)
                    completed[step["id"]] = files
                    self.last_record["files"] = files
                    self._save()
                    emit("workflow_step_succeeded", run_id=self.last_record["id"],
                         step_id=step["id"], files=files)
                    active_entry = active_task = None
            # A successful workflow also requires normal session shutdown.
            self.last_record["status"] = "succeeded"
            self._save()
            emit("workflow_succeeded", run_id=self.last_record["id"], files=self.last_record["files"],
                 record=str(path))
            return self.last_record
        except BaseException as exc:
            if active_entry is not None:
                active_entry["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                if active_task is not None:
                    self._copy_task_state(active_entry, active_task)
                active_entry["error"] = redact(str(exc))
            self.last_record.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                                    error=redact(str(exc)))
            with suppress(Exception):
                self._save()
            emit("workflow_failed", run_id=self.last_record["id"], status=self.last_record["status"],
                 message=redact(str(exc)), record=str(path))
            raise
