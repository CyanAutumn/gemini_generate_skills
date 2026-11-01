"""Gemini web image and video generation through a visible local CloakBrowser."""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

from browser_session import BrowserSession, LoginRequired, emit, redact
from playwright.sync_api import Error as BrowserError
from local_state import RunStore, default_data_dir, utc_now, verify_image, verify_saved_files, write_json


def common(parser, *, root=False):
    defaults = {
        "profile": "default", "data_dir": default_data_dir(),
        "output_dir": Path(__file__).resolve().parents[2] / "outputs",
    }
    parser.add_argument("--profile", default=defaults["profile"] if root else argparse.SUPPRESS,
                        help="专用浏览器配置名称（默认 default）")
    parser.add_argument("--data-dir", type=Path, default=defaults["data_dir"] if root else argparse.SUPPRESS,
                        help="私有登录态目录，默认位于 LOCALAPPDATA")
    parser.add_argument("--output-dir", type=Path, default=defaults["output_dir"] if root else argparse.SUPPRESS,
                        help="图片、视频及任务记录输出目录")
    parser.add_argument("--proxy", default=os.environ.get("GEMINI_BROWSER_PROXY") if root else argparse.SUPPRESS,
                        help="仅本浏览器使用的代理地址，也可设置 GEMINI_BROWSER_PROXY")
    parser.add_argument("--workflows-dir", type=Path,
                        default=Path.cwd() / "workflows" if root else argparse.SUPPRESS,
                        help="工作流库，默认当前工作目录的 workflows")


def parser_for_cli():
    parser = argparse.ArgumentParser(description="使用 CloakBrowser 操作 Gemini 网页；不调用 Gemini API。")
    common(parser, root=True)
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="打开浏览器，等待人工登录并保存会话")
    common(login)
    login.add_argument("--timeout", type=float, default=900, help="等待人工登录秒数")
    status = commands.add_parser("status", help="检查持久化登录态（不提交生成）")
    common(status)
    status.add_argument("--inspect", action="store_true", help="在私有数据目录保存页面控件和截图")
    for media_type, help_text, default_timeout, require_reference in [
        ("image", "提交一次文生图或图生图任务并下载校验", 300, False),
        ("video", "使用参考图片提交一次视频任务并下载校验", 600, True),
    ]:
        generation = commands.add_parser(media_type, help=help_text)
        common(generation)
        prompts = generation.add_mutually_exclusive_group(required=True)
        prompts.add_argument("--prompt", help="完整生成提示词")
        prompts.add_argument("--prompt-file", type=Path, help="UTF-8 提示词文件")
        generation.add_argument("--reference", type=Path, action="append", default=[],
                                required=require_reference, help="参考图片路径，可重复指定")
        generation.add_argument("--timeout", type=float, default=default_timeout,
                                help="生成等待秒数；登录等待另计")
    result = commands.add_parser("result", help="仅校验已有任务的本地图片或视频，不打开浏览器")
    common(result)
    result.add_argument("run_id")
    cleanup = commands.add_parser("cleanup", help="仅删除指定任务聊天，不生成图片")
    common(cleanup)
    cleanup.add_argument("run_id")
    workflow = commands.add_parser("workflow", help="创建、预览和执行固定参考图工作流")
    common(workflow)
    from workflow_cli import add_workflow_commands
    add_workflow_commands(workflow, common)
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.profile):
        parser.error("--profile 只能包含字母、数字、下划线、短横线，最多 64 个字符")
    if hasattr(args, "timeout") and (not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("--timeout 必须是有限正数")
    args.data_dir = args.data_dir.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    args.workflows_dir = args.workflows_dir.expanduser().resolve()
    if args.command == "workflow":
        from workflow_cli import execute_workflow_command
        return execute_workflow_command(args)
    profile = args.data_dir / "profiles" / args.profile
    record = None
    store = RunStore(args.output_dir)
    try:
        if args.command in {"image", "video"}:
            prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8-sig")
            prompt = prompt.strip()
            if not prompt:
                parser.error("提示词不能为空")
            references = [verify_image(path.expanduser().resolve()) for path in args.reference]
            record = store.create(prompt, profile)
            record["references"] = references
            record["media_type"] = args.command
            store.save(record)
            emit("run_created", run_id=record["id"], record=str(store.path_for(record["id"])))
        elif args.command in {"result", "cleanup"}:
            record = store.load(args.run_id)
            if args.command == "result":
                if not record.get("files"):
                    raise ValueError("该任务没有已保存的本地图片或视频；result 不会打开网页或重新生成。")
                media_type = record.get("media_type", "image")
                files = verify_saved_files(record["files"], media_type)
                emit("result", run_id=record["id"], files=files,
                     media_type=media_type, status=record["status"],
                     chat_cleanup=record.get("chat_cleanup"), local_only=True)
                return 0
            cleanup_state = record.get("chat_cleanup", {}).get("status")
            if cleanup_state in {"deleted", "not_created"}:
                emit("chat_cleanup", run_id=record["id"], status=cleanup_state, already_cleaned=True)
                return 0
            if Path(record["profile"]).resolve() != profile:
                raise ValueError("当前 profile 与原任务不一致，请使用原来的 --profile 和 --data-dir。")
            if not record.get("chat_url"):
                raise ValueError("该任务没有可定位的聊天链接，不能安全清理；不会重新提交生成。")
        with BrowserSession(profile, proxy=args.proxy) as session:
            try:
                session.navigate()
            except BrowserError:
                if args.command != "login":
                    raise
                emit("waiting_for_connection", message="连接 Gemini 失败。浏览器保持打开，请检查网络或在窗口内刷新页面；连接成功后可继续登录。")
            if args.command == "login":
                session.ensure_login(args.timeout)
                write_json(args.data_dir / "profiles" / args.profile / "session-check.json",
                           {"authenticated": True, "last_confirmed_at": utc_now()})
                emit("login_saved", profile_dir=str(profile), message="登录验证通过，正在正常关闭浏览器保存会话。")
            elif args.command == "status":
                # Allow the app shell and account controls to finish rendering.
                for _ in range(12):
                    if session.auth_state() != "loading":
                        break
                    session.page.wait_for_timeout(1000)
                info = session.snapshot()
                diagnostics = session.diagnostics(args.data_dir / "diagnostics") if args.inspect else {}
                emit("status", authenticated=info["auth_state"] == "authenticated",
                     auth_state=info["auth_state"], **diagnostics)
                if args.inspect:
                    emit("controls", controls=info["controls"])
                return 0 if info["auth_state"] == "authenticated" else 2
            else:
                if args.command in {"image", "video"} and record["status"] == "prepared":
                    record["status"] = "waiting_for_login"
                    store.save(record)
                session.ensure_login(900)
                from gemini_web import GeminiImages
                driver = GeminiImages(session, store)
                if args.command in {"image", "video"}:
                    driver.generate(record, args.timeout)
                else:
                    driver.cleanup(record)
                    if record.get("chat_cleanup", {}).get("status") not in {"deleted", "not_created"}:
                        raise RuntimeError("尚未确认任务聊天已删除；请检查记录或重新运行 cleanup。")
        return 0
    except KeyboardInterrupt:
        emit("interrupted", message="已停止；请用 result 检查本地图片或视频，必要时用 cleanup 清理原任务聊天。不要重新提交不明状态的任务。",
             run_id=record["id"] if record else None)
        if record and args.command == "cleanup":
            cleanup = record.setdefault("chat_cleanup", {})
            if cleanup.get("status") not in {"deleted", "not_created"}:
                cleanup.update(status="failed", error="清理被中断", updated_at=utc_now())
                store.save(record)
        return 130
    except Exception as exc:
        emit("error", error_type=type(exc).__name__, message=redact(str(exc))[:2000],
             run_id=record["id"] if record else None)
        if record and args.command == "cleanup":
            cleanup = record.setdefault("chat_cleanup", {})
            if cleanup.get("status") not in {"deleted", "not_created"}:
                cleanup.update(status="failed", error=redact(str(exc))[:1000], updated_at=utc_now())
                store.save(record)
        elif record and args.command in {"image", "video"} and record["status"] not in {"succeeded", "submitted", "submitting", "generating", "downloading", "download_failed", "timed_out", "blocked"}:
            record["status"] = "waiting_for_login" if isinstance(exc, LoginRequired) else "failed_before_submit"
            record["error"] = redact(str(exc))[:1000]
            store.save(record)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
