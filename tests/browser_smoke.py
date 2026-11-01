"""Manual loopback-only check: python tests/browser_smoke.py.

Real CloakBrowser performs two independent image tasks, each with an explicit
new chat, native download, and confirmed deletion. No Google account is used.
The temporary browser profile and generated fixture files are removed on exit.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "gemini-browser" / "scripts"))

from cloakbrowser import binary_info, launch_persistent_context

import gemini_web
from gemini_web import GeminiImages
from local_state import RunStore, verify_image, write_json


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Local Gemini fixture</title>
<link rel="icon" href="data:,"><style>
body { font:16px sans-serif; margin:32px; }
textarea { display:block; width:600px; height:100px; }
button, a { margin:8px; padding:8px; }
user-query, model-response { display:block; margin-top:20px; }
.image-card { display:inline-block; padding:12px; border:1px solid #888; }
.image-card img { display:block; width:384px; height:256px; }
.download { display:none; }
.image-card:hover .download { display:inline-block; }
[role=dialog] { border:1px solid #888; padding:12px; }
[hidden] { display:none !important; }
</style></head><body>
<h1>Local Gemini fixture (not a Google service)</h1>
__NOTICE__
<a href="/app" aria-label="发起新对话" id="new-chat">发起新对话</a>
<nav id="history">__HISTORY__</nav>
<button aria-label="Tools" id="tools">Tools</button>
<div id="tools-menu" hidden><button role="menuitem" id="images">Create images</button></div>
<textarea aria-label="Message Gemini"></textarea>
<button aria-label="Send message" id="send">Send message</button>
<button aria-label="打开对话操作菜单。" id="chat-actions" hidden>Chat actions</button>
<div id="chat-menu" role="menu" hidden>
  <button role="menuitem" id="delete-chat">删除</button>
</div>
<div role="dialog" aria-label="删除对话？" id="delete-dialog" hidden>
  <p>删除这个对话？</p><button id="confirm-delete">删除</button>
</div>
<div id="responses"></div>
<script>
let mode = 'text';
let current = __CURRENT_CHAT__;
let originalPrompt = __PROMPT__;
if (__RESET_PATH__) history.replaceState(null, '', '/app');
document.querySelector('#new-chat').onclick = event => {
  event.preventDefault();
  fetch('/new-chat', {method:'POST'});
  current = null; originalPrompt = ''; mode = 'text';
  history.replaceState(null, '', '/app');
  document.querySelector('#responses').innerHTML = '';
  document.querySelector('textarea').value = '';
  document.querySelector('#chat-actions').hidden = true;
};
document.querySelector('#tools').onclick = () => document.querySelector('#tools-menu').hidden = false;
document.querySelector('#images').onclick = () => {
  mode = 'images'; document.querySelector('#tools-menu').hidden = true;
};
function showImage() {
  document.querySelector('#responses').innerHTML = `
    <user-query></user-query>
    <model-response><div class="image-card"><div>
      <img src="/image.png?id=${current}" alt="Local generated fixture">
    </div><button class="download" aria-label="Download full size"
      onclick="window.location.href='/download.png?id=${current}'">Download full size</button>
    </div></model-response>`;
  document.querySelector('user-query').textContent = originalPrompt;
  document.querySelector('#chat-actions').hidden = false;
}
document.querySelector('#send').onclick = async () => {
  if (current !== null) throw new Error('Fixture refuses submission in an existing chat');
  const response = await fetch('/submit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({prompt:document.querySelector('textarea').value, mode})
  });
  if (!response.ok) throw new Error('Local fixture rejected submission');
  const result = await response.json();
  current = result.id; originalPrompt = document.querySelector('textarea').value;
  history.replaceState(null, '', '/app/' + current);
  const row = document.createElement('a'); row.href = '/app/' + current;
  row.textContent = current; document.querySelector('#history').append(row);
  showImage();
};
document.querySelector('#chat-actions').onclick = () => document.querySelector('#chat-menu').hidden = false;
document.querySelector('#delete-chat').onclick = () => {
  document.querySelector('#chat-menu').hidden = true;
  document.querySelector('#delete-dialog').hidden = false;
};
document.querySelector('#confirm-delete').onclick = async () => {
  const response = await fetch('/delete', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:current})
  });
  if (!response.ok) throw new Error('Fixture rejected deletion');
  window.location.href = '/app';
};
if (current !== null) showImage();
</script></body></html>"""


class LocalFixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.submissions = []
        self.downloads = []
        self.deletions = []
        self.new_chat_clicks = 0
        self.missing_chat_checks = []
        self.chats = {"unrelated-keep": {"prompt": "An unrelated chat must remain"}}
        self.images = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body: bytes, content_type="application/json", attachment=False):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if attachment:
                    self.send_header("Content-Disposition", 'attachment; filename="fixture.png"')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = urlsplit(self.path)
                path = parts.path
                if path == "/app" or path.startswith("/app/"):
                    chat_id = path.removeprefix("/app/") if path.startswith("/app/") else None
                    with fixture.lock:
                        missing = chat_id is not None and chat_id not in fixture.chats
                        if missing:
                            fixture.missing_chat_checks.append(chat_id)
                            chat_id = None
                        prompt = fixture.chats[chat_id]["prompt"] if chat_id else ""
                        rows = "".join(f'<a href="/app/{key}">{key}</a>' for key in fixture.chats)
                    page = PAGE.replace("__HISTORY__", rows)
                    page = page.replace("__NOTICE__", '<div role="status">无法加载此对话。对话不存在或已被删除。</div>' if missing else '')
                    page = page.replace("__RESET_PATH__", "true" if missing else "false")
                    page = page.replace("__CURRENT_CHAT__", json.dumps(chat_id))
                    page = page.replace("__PROMPT__", json.dumps(prompt))
                    self.reply(page.encode("utf-8"), "text/html; charset=utf-8")
                elif path in {"/image.png", "/download.png"}:
                    chat_id = parse_qs(parts.query).get("id", [None])[0]
                    with fixture.lock:
                        if chat_id not in fixture.chats or chat_id not in fixture.images:
                            self.send_error(404)
                            return
                        image = fixture.images[chat_id]
                        if path == "/download.png":
                            fixture.downloads.append(chat_id)
                    self.reply(image, "image/png", attachment=path == "/download.png")
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path == "/new-chat":
                    with fixture.lock:
                        fixture.new_chat_clicks += 1
                    self.reply(b'{"ok":true}')
                    return
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path == "/submit":
                    if payload.get("mode") != "images" or not payload.get("prompt"):
                        self.send_error(400)
                        return
                    with fixture.lock:
                        chat_id = f"task-{len(fixture.submissions) + 1}"
                        fixture.chats[chat_id] = {"prompt": payload["prompt"]}
                        image = io.BytesIO()
                        Image.new("RGB", (384, 256), (55, 75 + 40 * len(fixture.submissions), 155)).save(image, format="PNG")
                        fixture.images[chat_id] = image.getvalue()
                        fixture.submissions.append(chat_id)
                    self.reply(json.dumps({"id": chat_id}).encode())
                elif self.path == "/delete":
                    chat_id = payload.get("id")
                    with fixture.lock:
                        if chat_id not in fixture.chats or chat_id == "unrelated-keep":
                            self.send_error(400)
                            return
                        del fixture.chats[chat_id]
                        fixture.deletions.append(chat_id)
                    self.reply(b'{"ok":true}')
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.app_url = self.origin + "/app"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def is_local(self, url):
        parts = urlsplit(url)
        return parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port == self.port

    def is_chat(self, url):
        return self.is_local(url) and re.fullmatch(r"/app/task-\d+", urlsplit(url).path) is not None


class FixtureSession:
    """Only this test accepts the loopback fixture as authenticated."""

    def __init__(self, context, fixture):
        self.page = context.new_page()
        self.fixture = fixture

    def navigate(self, url):
        if not self.fixture.is_local(url):
            raise ValueError("Smoke test refuses navigation outside its local fixture")
        self.page.goto(url, wait_until="domcontentloaded", timeout=15000)

    def auth_state(self):
        return "authenticated"

    def ensure_login(self, timeout=60):
        return True

    def diagnostics(self, directory):
        return {"local_fixture": True, "message": "No account or login data exists in this fixture"}


def run_check():
    report = {"test": "local_browser_smoke", "target": "loopback_fixture_only", "google_accessed": False}
    binary = Path(os.environ.get("CLOAKBROWSER_BINARY_PATH") or binary_info()["binary_path"])
    if not binary.is_file():
        raise RuntimeError("Install the browser before running this local-only check")
    with patch.dict(os.environ, {
        "CLOAKBROWSER_AUTO_UPDATE": "false", "CLOAKBROWSER_BINARY_PATH": str(binary),
    }), tempfile.TemporaryDirectory(prefix="gemini-browser-smoke-") as temporary, LocalFixture() as fixture:
        root = Path(temporary)
        profile = root / "profile"
        store = RunStore(root / "outputs")
        blocked_requests = []

        def restrict_request(route):
            if fixture.is_local(route.request.url):
                route.continue_()
            else:
                blocked_requests.append(route.request.url)
                route.abort()

        records = []
        with patch.object(gemini_web, "GEMINI_URL", fixture.app_url), patch.object(
            GeminiImages, "_is_chat_url", staticmethod(fixture.is_chat)
        ):
            for number in (1, 2):
                record = store.create(f"Local image task {number}", profile)
                context = launch_persistent_context(
                    profile, headless=True, accept_downloads=True,
                    args=["--disable-background-networking", "--disable-component-update", "--no-proxy-server",
                          "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
                    service_workers="block",
                )
                context.route("**/*", restrict_request)
                context.set_default_timeout(5000)
                try:
                    GeminiImages(FixtureSession(context, fixture), store).generate(record, timeout=20)
                    assert record["status"] == "succeeded", record
                    assert record["chat_cleanup"]["status"] == "deleted", record
                    assert record["new_chat_verified"] is True, record
                    assert record["image_mode"] == "tools_image_mode", record
                    image = verify_image(record["files"][0]["path"])
                    assert (image["width"], image["height"]) == (384, 256), image
                    assert set(fixture.chats) == {"unrelated-keep"}, fixture.chats
                    records.append(record)
                finally:
                    context.close()

        assert len(set(fixture.submissions)) == 2, fixture.submissions
        assert fixture.submissions == fixture.downloads == fixture.deletions
        assert fixture.new_chat_clicks == 2, fixture.new_chat_clicks
        assert set(fixture.missing_chat_checks) == set(fixture.deletions), fixture.missing_chat_checks
        assert records[0]["chat_url"] != records[1]["chat_url"]
        assert records[0]["files"][0]["sha256"] != records[1]["files"][0]["sha256"]
        for record in records:
            assert store.load(record["id"])["chat_cleanup"]["status"] == "deleted"
            verify_image(record["files"][0]["path"])
        report.update({
            "status": "passed", "tasks": 2, "new_chat_clicks": fixture.new_chat_clicks,
            "submissions": len(fixture.submissions), "downloads": len(fixture.downloads),
            "deletions": len(fixture.deletions), "distinct_task_chat_ids": True,
            "remaining_task_chats": 0, "unrelated_chat_preserved": True, "local_files_verified": True,
            "deleted_targets_rechecked_with_explicit_notice": True,
            "cached_browser_only": True, "blocked_nonlocal_requests": len(blocked_requests),
            "temporary_fixture_files_removed": True,
        })
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    report_path = PROJECT / ".runtime" / "browser-smoke-report.json"
    try:
        report = run_check()
    except BaseException as exc:
        write_json(report_path, {"test": "local_browser_smoke", "target": "loopback_fixture_only",
                                 "status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        raise
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
