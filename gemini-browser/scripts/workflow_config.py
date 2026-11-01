"""Portable, declarative Gemini workflows. No browser or generation is started here."""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from local_state import read_json, verify_image, write_json


MANIFEST_NAME = "workflow.json"
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TEMPLATE = re.compile(r"{{\s*([^{}]+?)\s*}}")
_TOP_KEYS = {"schema_version", "name", "description", "variables", "steps"}
_STEP_KEYS = {"id", "type", "prompt", "prompt_file", "references", "timeout"}
_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)}


class WorkflowError(ValueError):
    """An invalid workflow definition, resource, or parameter."""


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise WorkflowError(f"{label}只能使用 ASCII 字母、数字、下划线和短横线，最多64字符")
    if value.upper() in _RESERVED_NAMES:
        raise WorkflowError(f"{label}不能使用 Windows 保留名称：{value}")
    return value


def _keys(value: Any, allowed: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise WorkflowError(f"{label}必须是对象")
    unknown = [str(key) for key in value if key not in allowed]
    if unknown:
        raise WorkflowError(f"{label}包含未知字段：{', '.join(unknown)}")


def _scalar(value: Any, label: str, *, allow_required=False) -> Any:
    if value is None and allow_required:
        return None
    if not isinstance(value, (str, int, float, bool)) or value is None:
        raise WorkflowError(f"{label}必须是字符串、有限数字或布尔值，不能为 null、列表或对象")
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkflowError(f"{label}必须是有限数字")
    return value


def _timeout(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkflowError(f"{label}必须是有限正数，不能使用布尔值")
    try:
        number = float(value)
    except OverflowError as exc:
        raise WorkflowError(f"{label}必须是有限正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise WorkflowError(f"{label}必须是有限正数")
    return number


def _resource(directory: Path, value: Any, label: str, *, source: bool) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkflowError(f"{label}必须是非空文件路径字符串")
    windows = PureWindowsPath(value)
    absolute = Path(value).is_absolute() or windows.is_absolute() or PurePosixPath(value).is_absolute()
    # Drive-relative Windows paths are not explicit absolute paths.
    if windows.drive and not absolute:
        raise WorkflowError(f"{label}不能使用盘符相对路径")
    if absolute:
        if not source:
            raise WorkflowError(f"{label}必须是工作流目录内的相对路径，禁止绝对路径")
        candidate = Path(value).expanduser().resolve()
    else:
        if value.startswith(("/", "\\")) or ":" in value:
            raise WorkflowError(f"{label}不能使用绝对路径或特殊盘符路径")
        if ".." in PurePosixPath(value.replace("\\", "/")).parts:
            raise WorkflowError(f"{label}禁止使用 ../ 跳出目录")
        candidate = (directory / value.replace("\\", "/")).resolve()
        if not candidate.is_relative_to(directory.resolve()):
            raise WorkflowError(f"{label}经符号链接跳出工作流目录")
    if not candidate.is_file():
        raise WorkflowError(f"{label}文件不存在：{value}")
    return candidate


def _template_variables(prompt: str, declared: dict[str, Any], label: str) -> None:
    for match in _TEMPLATE.finditer(prompt):
        variable = match.group(1).strip()
        if not variable.isidentifier():
            raise WorkflowError(f"{label}的模板变量名无效：{variable}")
        if variable not in declared:
            raise WorkflowError(f"{label}使用了未声明变量：{variable}")
    remainder = _TEMPLATE.sub("", prompt)
    if "{{" in remainder or "}}" in remainder:
        raise WorkflowError(f"{label}的模板标记不完整或嵌套")


def _analyse(manifest: Any, name: str, directory: Path, *, source=False) -> dict[str, Any]:
    _name(name, "工作流名称")
    _keys(manifest, _TOP_KEYS, "工作流")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise WorkflowError("schema_version 必须为整数1")
    if "name" in manifest and manifest["name"] != name:
        raise WorkflowError("清单中的 name 必须与工作流目录名称一致")
    description = manifest.get("description", "")
    if not isinstance(description, str):
        raise WorkflowError("description 必须是字符串")
    variables = manifest.get("variables", {})
    if not isinstance(variables, dict):
        raise WorkflowError("variables 必须是对象")
    for key, value in variables.items():
        if not isinstance(key, str) or not key.isidentifier():
            raise WorkflowError(f"变量名必须是中文、字母或下划线开头的标识符：{key}")
        _scalar(value, f"变量 {key} 的默认值", allow_required=True)
    raw_steps = manifest.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError("steps 必须是非空步骤列表")
    seen: dict[str, str] = {}
    steps = []
    for index, raw in enumerate(raw_steps, 1):
        label = f"步骤{index}"
        _keys(raw, _STEP_KEYS, label)
        step_id = _name(raw.get("id"), f"{label} id")
        label = f"步骤 {step_id}"
        if step_id in seen:
            raise WorkflowError(f"步骤 id 重复：{step_id}")
        media_type = raw.get("type")
        if not isinstance(media_type, str) or media_type not in {"image", "video"}:
            raise WorkflowError(f"{label} type 只能是 image 或 video")
        if ("prompt" in raw) == ("prompt_file" in raw):
            raise WorkflowError(f"{label}必须且只能指定 prompt 或 prompt_file 其中一项")
        if "prompt_file" in raw:
            prompt_path = _resource(directory, raw["prompt_file"], f"{label} prompt_file", source=source)
            try:
                prompt = prompt_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise WorkflowError(f"{label} prompt_file 必须是可读取的 UTF-8 文本") from exc
        else:
            prompt = raw["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError(f"{label}提示词必须是非空字符串")
        _template_variables(prompt, variables, label)
        references = raw.get("references", [])
        if not isinstance(references, list):
            raise WorkflowError(f"{label} references 必须是列表")
        if media_type == "video" and not references:
            raise WorkflowError(f"{label}图生视频必须至少提供一张参考图或前序图片步骤")
        prepared_references = []
        for reference_index, reference in enumerate(references, 1):
            reference_label = f"{label}参考图{reference_index}"
            if isinstance(reference, str):
                path = _resource(directory, reference, reference_label, source=source)
                try:
                    metadata = verify_image(path)
                except (OSError, ValueError) as exc:
                    raise WorkflowError(f"{reference_label}不是有效图片：{exc}") from exc
                prepared_references.append({"source": path, "metadata": metadata})
            else:
                _keys(reference, {"step", "file_index"}, reference_label)
                previous = reference.get("step")
                if not isinstance(previous, str) or previous not in seen:
                    raise WorkflowError(f"{reference_label}只能引用已定义的前序步骤，不能引用自身或后续步骤")
                if seen[previous] != "image":
                    raise WorkflowError(f"{reference_label}只能引用生成图片的步骤，不能引用视频")
                file_index = reference.get("file_index", 0)
                if type(file_index) is not int or file_index < 0:
                    raise WorkflowError(f"{reference_label} file_index 必须是非负整数")
                prepared_references.append({"step": previous, "file_index": file_index})
        steps.append({"id": step_id, "type": media_type, "prompt": prompt,
                      "references": prepared_references,
                      "timeout": _timeout(raw.get("timeout", 600 if media_type == "video" else 300), f"{label} timeout")})
        seen[step_id] = media_type
    return {"schema_version": 1, "name": name, "description": description,
            "variables": dict(variables), "steps": steps}


def _render(prompt: str, parameters: dict[str, Any]) -> str:
    def replace(match):
        value = parameters[match.group(1).strip()]
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    return _TEMPLATE.sub(replace, prompt)


@dataclass
class WorkflowDefinition:
    name: str
    directory: Path
    manifest: dict[str, Any]

    def validate(self) -> dict[str, Any]:
        checked = _analyse(self.manifest, self.name, Path(self.directory).resolve())
        return {"name": self.name, "description": checked["description"],
                "steps": [{"id": step["id"], "type": step["type"], "timeout": step["timeout"],
                           "references": len(step["references"])} for step in checked["steps"]],
                "variables": {key: {"required": value is None, "default": value}
                              for key, value in checked["variables"].items()}}

    def plan(self, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        checked = _analyse(self.manifest, self.name, Path(self.directory).resolve())
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise WorkflowError("工作流参数必须是对象")
        unknown = [str(key) for key in parameters if key not in checked["variables"]]
        if unknown:
            raise WorkflowError(f"传入了未声明参数：{', '.join(unknown)}")
        resolved = {}
        for key, default in checked["variables"].items():
            if key not in parameters and default is None:
                raise WorkflowError(f"缺少必填参数：{key}")
            resolved[key] = _scalar(parameters.get(key, default), f"参数 {key}")
        steps = []
        for step in checked["steps"]:
            references = [dict(reference["metadata"]) if "metadata" in reference else dict(reference)
                          for reference in step["references"]]
            steps.append({**step, "prompt": _render(step["prompt"], resolved), "references": references})
        return {"name": self.name, "directory": str(Path(self.directory).resolve()),
                "parameters": resolved, "steps": steps}


class WorkflowStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def _directory(self, name: str) -> Path:
        _name(name, "工作流名称")
        directory = self.root / name
        if not directory.resolve().is_relative_to(self.root):
            raise WorkflowError("工作流目录经符号链接跳出工作流根目录")
        return directory

    def create(self, name: str, spec: dict, source_dir: Path) -> dict[str, Any]:
        directory = self._directory(name)
        if directory.exists() or directory.is_symlink():
            raise FileExistsError(f"工作流已经存在，不会覆盖：{name}")
        # Read and validate every input before creating the destination directory.
        checked = _analyse(spec, name, Path(source_dir).expanduser().resolve(), source=True)
        manifest = {key: checked[key] for key in ("schema_version", "name", "description", "variables")}
        manifest["steps"] = []
        copies = []
        prompts = []
        for step in checked["steps"]:
            prompt_file = f"prompts/{step['id']}.txt"
            prompts.append((prompt_file, step["prompt"]))
            references = []
            for index, reference in enumerate(step["references"], 1):
                if "source" not in reference:
                    references.append(dict(reference))
                    continue
                destination = f"assets/{step['id']}-{index}{_EXTENSIONS[reference['metadata']['format']]}"
                copies.append((reference["source"], destination, reference["metadata"]["sha256"]))
                references.append(destination)
            manifest["steps"].append({"id": step["id"], "type": step["type"],
                                      "prompt_file": prompt_file, "references": references,
                                      "timeout": step["timeout"]})
        created = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            directory.mkdir()  # Exclusive: a concurrent creator is never overwritten or removed.
            created = True
            for relative, prompt in prompts:
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(prompt, encoding="utf-8")
            for source, relative, digest in copies:
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if verify_image(target)["sha256"] != digest:
                    raise WorkflowError("参考图片在复制期间发生变化，已停止创建工作流")
            definition = WorkflowDefinition(name, directory, manifest)
            definition.validate()
            manifest_path = directory / MANIFEST_NAME
            write_json(manifest_path, manifest)
            return {"name": name, "directory": str(directory), "manifest_path": str(manifest_path),
                    "workflow": manifest}
        except BaseException as exc:
            if created:
                # Windows recursive cleanup is limited to the exact directory
                # created by this invocation, after checking its resolved boundary.
                expected = self.root / name
                resolved = directory.resolve()
                if resolved == expected and resolved.parent == self.root and not directory.is_symlink():
                    try:
                        shutil.rmtree(directory)
                    except OSError as cleanup_error:
                        if hasattr(exc, "add_note"):
                            exc.add_note(f"本次新建目录清理失败：{cleanup_error}")
            raise

    def load(self, name: str) -> WorkflowDefinition:
        directory = self._directory(name)
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file():
            raise WorkflowError(f"工作流清单不存在：{name}/{MANIFEST_NAME}")
        if not manifest_path.resolve().is_relative_to(directory.resolve()):
            raise WorkflowError("工作流清单经符号链接跳出工作流目录")
        try:
            manifest = read_json(manifest_path)
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"工作流清单不是可读取的 UTF-8 JSON：{name}") from exc
        if not isinstance(manifest, dict):
            raise WorkflowError("工作流清单必须是对象")
        definition = WorkflowDefinition(name, directory, manifest)
        definition.validate()
        return definition

    def list(self) -> list[dict[str, str]]:
        if not self.root.exists():
            return []
        entries = []
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not _NAME.fullmatch(directory.name) or not directory.is_dir():
                continue
            if not (directory / MANIFEST_NAME).exists():
                continue
            definition = self.load(directory.name)
            entries.append({"name": definition.name, "description": definition.manifest.get("description", ""),
                            "directory": str(definition.directory)})
        return entries
