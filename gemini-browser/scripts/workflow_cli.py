"""Agent-friendly commands for creating, checking and running named workflows."""

from __future__ import annotations

import json
from pathlib import Path

from browser_session import emit, redact


def add_workflow_commands(parser, common):
    commands = parser.add_subparsers(dest="workflow_command", required=True)
    create = commands.add_parser("create", help="打包固定素材与提示词，创建命名工作流")
    common(create)
    create.add_argument("name")
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path, help="工作流 JSON；素材相对该文件解析")
    source.add_argument("--type", choices=["image", "video"], help="便捷创建单步工作流")
    prompts = create.add_mutually_exclusive_group()
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)
    create.add_argument("--reference", type=Path, action="append", default=[])
    create.add_argument("--var", action="append", default=[], metavar="KEY=DEFAULT", help="声明带默认值的变量")
    create.add_argument("--required", action="append", default=[], metavar="KEY", help="声明运行时必填变量")
    create.add_argument("--description")
    listing = commands.add_parser("list", help="列出当前项目的工作流")
    common(listing)
    for action, description in [
        ("show", "查看工作流定义"), ("validate", "离线校验配置、素材和提示词"),
        ("plan", "替换变量，预览执行计划；不启动浏览器"),
        ("run", "按顺序运行，每步新聊天、保存结果并删除聊天"),
    ]:
        command = commands.add_parser(action, help=description)
        common(command)
        command.add_argument("name")
        if action in {"plan", "run"}:
            command.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
            command.add_argument("--params-file", type=Path, help="UTF-8 JSON 参数，--set 可覆盖")
        if action == "run":
            command.add_argument("--dry-run", action="store_true", help="仅预检与预览，不生成媒体")
    result = commands.add_parser("result", help="本地查看运行记录并复验已保存媒体")
    common(result)
    result.add_argument("run_id")


def _assignments(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("变量参数需要 KEY=VALUE 格式")
        key, text = value.split("=", 1)
        if not key or key in result:
            raise ValueError(f"变量名称为空或重复：{key}")
        result[key] = text
    return result


def _parameters(args):
    parameters = {}
    if args.params_file:
        parameters = json.loads(args.params_file.read_text(encoding="utf-8-sig"))
        if not isinstance(parameters, dict):
            raise ValueError("参数文件必须是 JSON 对象")
    parameters.update(_assignments(args.set))
    return parameters


def _create_spec(args):
    if args.spec:
        if args.prompt is not None or args.prompt_file or args.reference or args.var or args.required:
            raise ValueError("--spec 不能与单步创建的提示词、参考图或变量声明混用")
        path = args.spec.expanduser().resolve()
        spec = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(spec, dict):
            raise ValueError("工作流定义必须是 JSON 对象")
        # The explicit registration name also supports cloning an example bundle.
        spec["name"] = args.name
        if args.description is not None:
            spec["description"] = args.description
        return spec, path.parent
    if args.prompt is None and args.prompt_file is None:
        raise ValueError("单步创建需要 --prompt 或 --prompt-file")
    variables = _assignments(args.var)
    for key in args.required:
        if key in variables:
            raise ValueError(f"变量重复声明：{key}")
        variables[key] = None
    step = {"id": "generate", "type": args.type,
            "references": [str(path.expanduser().resolve()) for path in args.reference]}
    if args.prompt_file:
        step["prompt_file"] = str(args.prompt_file.expanduser().resolve())
    else:
        step["prompt"] = args.prompt
    return {"schema_version": 1, "name": args.name, "description": args.description or "",
            "variables": variables, "steps": [step]}, Path.cwd()


def execute_workflow_command(args):
    runner = None
    try:
        from workflow_config import WorkflowStore
        store = WorkflowStore(args.workflows_dir)
        action = args.workflow_command
        if action == "create":
            spec, source = _create_spec(args)
            created = store.create(args.name, spec, source)
            emit("workflow_created", **created)
        elif action == "list":
            emit("workflow_list", directory=str(Path(args.workflows_dir).expanduser().resolve()), workflows=store.list())
        elif action == "result":
            from workflow_runner import load_workflow_result
            record = load_workflow_result(args.output_dir, args.run_id)
            emit("workflow_result", run=record, local_only=True)
        else:
            definition = store.load(args.name)
            if action == "show":
                emit("workflow_definition", name=definition.name, directory=str(definition.directory),
                     workflow=definition.manifest)
            elif action == "validate":
                emit("workflow_validated", **definition.validate())
            elif action == "plan" or (action == "run" and args.dry_run):
                emit("workflow_plan", dry_run=True, plan=definition.plan(_parameters(args)))
            else:
                from workflow_runner import WorkflowRunner
                profile = args.data_dir / "profiles" / args.profile
                runner = WorkflowRunner(definition, args.output_dir, profile, proxy=args.proxy)
                runner.run(_parameters(args))
        return 0
    except KeyboardInterrupt:
        emit("workflow_interrupted", message="工作流已停止；已提交步骤不会自动重试，请检查步骤记录和清理状态。",
             run_id=runner.last_record["id"] if runner and runner.last_record else None)
        return 130
    except Exception as exc:
        emit("error", error_type=type(exc).__name__, message=redact(str(exc)),
             run_id=runner.last_record["id"] if runner and runner.last_record else None)
        return 1
