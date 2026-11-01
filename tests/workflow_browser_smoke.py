"""Run a portable image-to-video workflow against loopback-only browser fixtures.

Uses a temporary workflow registry, browser profile and output directory. No
Google page or real account is opened; only existing local browser binaries run.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "gemini-browser" / "scripts"))

from cloakbrowser import binary_info, launch_persistent_context

import gemini_web
from browser_smoke import FixtureSession, LocalFixture
from gemini_web import GeminiImages
from local_state import read_json, verify_image, verify_media, write_json
from workflow_config import WorkflowStore
from workflow_runner import WorkflowRunner, load_workflow_result


def run_check():
    binary = Path(os.environ.get("CLOAKBROWSER_BINARY_PATH") or binary_info()["binary_path"])
    if not binary.is_file():
        raise RuntimeError("Install the local CloakBrowser binary before this offline check")
    with patch.dict(os.environ, {"CLOAKBROWSER_AUTO_UPDATE": "false", "CLOAKBROWSER_BINARY_PATH": str(binary)}), \
         tempfile.TemporaryDirectory(prefix="gemini-workflow-smoke-") as temporary, LocalFixture() as fixture:
        root = Path(temporary).resolve()
        source = root / "source.png"
        Image.new("RGB", (80, 60), "orange").save(source)
        source_hash = verify_image(source)["sha256"]
        registry = WorkflowStore(root / "registry")
        registry.create("fixed-smoke", {
            "schema_version": 1, "name": "fixed-smoke", "variables": {"subject": "cat"},
            "steps": [
                {"id": "still", "type": "image", "prompt": "Draw {{subject}} from the reference", "references": ["source.png"], "timeout": 30},
                {"id": "motion", "type": "video", "prompt": "Animate {{subject}} from the generated image", "references": [{"step": "still"}], "timeout": 30},
            ],
        }, root)
        definition = registry.load("fixed-smoke")
        sessions = []
        rejected_requests = []

        def restrict_request(route):
            if fixture.is_local(route.request.url):
                route.continue_()
            else:
                rejected_requests.append(route.request.url)
                route.abort()

        class LocalBrowserSession(FixtureSession):
            def __init__(self, profile_dir, proxy=None):
                self.profile_dir = Path(profile_dir).resolve()
                if not self.profile_dir.is_relative_to(root) or proxy is not None:
                    raise ValueError("Local workflow smoke refuses a non-temporary profile or a proxy")
                self.context = None

            def __enter__(self):
                self.context = launch_persistent_context(
                    self.profile_dir, headless=True, accept_downloads=True,
                    args=["--disable-background-networking", "--disable-component-update", "--no-proxy-server",
                          "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
                    service_workers="block",
                )
                self.context.route("**/*", restrict_request)
                self.context.set_default_timeout(5000)
                FixtureSession.__init__(self, self.context, fixture)
                sessions.append(self)
                return self

            def __exit__(self, *_):
                self.context.close()

            def navigate(self, url=None):
                return super().navigate(url or fixture.app_url)

        with patch.object(gemini_web, "GEMINI_URL", fixture.app_url), patch.object(
            GeminiImages, "_is_chat_url", staticmethod(fixture.is_chat)
        ):
            runner = WorkflowRunner(definition, root / "outputs", root / "profile",
                                    session_factory=LocalBrowserSession, driver_factory=GeminiImages)
            result = runner.run({"subject": "orange cat"})

        assert result["status"] == "succeeded", result
        assert len(sessions) == 1, len(sessions)
        assert len(result["steps"]) == 2
        first, second = result["steps"]
        assert first["task_record"] != second["task_record"]
        first_task, second_task = read_json(first["task_record"]), read_json(second["task_record"])
        first_file = verify_media(first["files"][0]["path"], "image")
        final_file = verify_media(second["files"][0]["path"], "video")
        assert final_file["format"] == "MP4" and final_file["decoded_frames"] == 12, final_file
        assert second_task["references"][0]["sha256"] == first_file["sha256"]
        assert second_task["references"][0]["path"] == first_file["path"]
        assert fixture.submissions == fixture.downloads == fixture.deletions
        assert len(fixture.submissions) == 2 and len(set(fixture.submissions)) == 2
        assert fixture.new_chat_clicks == 2
        image_submission = fixture.submission_details[fixture.submissions[0]]
        video_submission = fixture.submission_details[fixture.submissions[1]]
        assert image_submission["mode"] == "images"
        assert image_submission["references"] == [source_hash], image_submission
        assert video_submission["mode"] == "videos"
        assert video_submission["references"] == [first_file["sha256"]], video_submission
        assert [receipt["id"] for receipt in fixture.reference_receipts] == [source_hash, first_file["sha256"]]
        assert set(fixture.chats) == {"unrelated-keep"}, fixture.chats
        assert set(fixture.missing_chat_checks) == set(fixture.deletions)
        assert first_task["chat_cleanup"]["status"] == second_task["chat_cleanup"]["status"] == "deleted"
        # A local result lookup runs after the browser is closed and revalidates
        # both preserved output files without reopening or continuing any chat.
        assert load_workflow_result(root / "outputs", result["id"]) == result
        report = {
            "test": "workflow_browser_smoke", "target": "loopback_fixture_only", "google_accessed": False,
            "status": "passed", "steps": ["image", "video"], "browser_sessions": len(sessions),
            "submissions": len(fixture.submissions), "downloads": len(fixture.downloads), "deletions": len(fixture.deletions),
            "new_chat_clicks": fixture.new_chat_clicks, "reference_uploads": len(fixture.reference_receipts),
            "video_reference_matches_previous_image_sha256": True, "separate_step_records": True,
            "remaining_task_chats": 0, "unrelated_chat_preserved": True, "local_result_verified": True,
            "video": {key: final_file[key] for key in ("format", "width", "height", "duration_seconds", "decoded_frames")},
            "blocked_nonlocal_requests": len(rejected_requests), "cached_browser_only": True,
            "temporary_fixture_files_removed": True,
        }
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    report_path = PROJECT / ".runtime" / "workflow-browser-smoke-report.json"
    try:
        report = run_check()
    except BaseException as exc:
        write_json(report_path, {"test": "workflow_browser_smoke", "target": "loopback_fixture_only",
                                 "status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        raise
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
