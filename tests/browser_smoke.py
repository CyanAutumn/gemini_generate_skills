"""Manual loopback-only check: python tests/browser_smoke.py.

Real CloakBrowser performs text-to-image, image-to-image, and image-to-video
tasks, each with a new chat, native download, and confirmed deletion.
References use a real file chooser and blob previews. No Google account is used.
The temporary browser profile and generated fixture files are removed on exit.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import av
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "gemini-browser" / "scripts"))

from cloakbrowser import binary_info, launch_persistent_context
from playwright.sync_api import TimeoutError as BrowserTimeoutError

import gemini_web
from gemini_web import GeminiImages
from local_state import RunStore, verify_image, verify_media, write_json


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Local Gemini fixture</title>
<link rel="icon" href="data:,"><style>
body { font:16px sans-serif; margin:32px; }
textarea { display:block; width:600px; height:100px; }
button, a { margin:8px; padding:8px; }
user-query, model-response { display:block; margin-top:20px; }
.image-card { display:inline-block; padding:12px; border:1px solid #888; }
.image-card img { display:block; width:384px; height:256px; }
.video-card { position:relative; display:inline-block; padding:12px; border:1px solid #888; }
.video-overlay { position:absolute; left:132px; top:76px; width:144px; height:128px;
  z-index:2; display:grid; place-items:center; background:rgba(0,0,0,.3); color:white; }
video { display:block; width:384px; height:256px; }
#previews img { width:80px; height:60px; object-fit:contain; }
.download { display:none; }
.image-card:hover .download { display:inline-block; }
[role=dialog] { border:1px solid #888; padding:12px; }
[hidden] { display:none !important; }
</style></head><body>
<h1>Local Gemini fixture (not a Google service)</h1>
__NOTICE__
<a href="/app" aria-label="发起新对话" id="new-chat">发起新对话</a>
<nav id="history">__HISTORY__</nav>
<button aria-label="上传和工具" id="tools">上传和工具</button>
<div id="tools-menu" role="menu" hidden>
  <button role="menuitemcheckbox" id="images">Create images</button>
  <button role="menuitemcheckbox" id="videos">Create videos</button>
  <button role="menuitem" id="upload">Upload files</button>
</div>
<input type="file" accept="image/*" multiple id="reference-input" hidden>
<input type="file" accept="text/plain" id="unrelated-file-input" hidden>
<div id="previews"></div>
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
let mode = __MODE__;
let references = [];
let current = __CURRENT_CHAT__;
let originalPrompt = __PROMPT__;
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') setTimeout(() => {
    const exportMenu = document.querySelector('#video-menu');
    if (exportMenu && !exportMenu.hidden) fetch('/dismiss-video-menu', {method:'POST'});
    for (const menu of document.querySelectorAll('[role="menu"]')) menu.hidden = true;
  }, 200);
});
if (__RESET_PATH__) history.replaceState(null, '', '/app');
document.querySelector('#new-chat').onclick = event => {
  event.preventDefault();
  fetch('/new-chat', {method:'POST'});
  current = null; originalPrompt = ''; mode = 'text'; references = [];
  history.replaceState(null, '', '/app');
  document.querySelector('#responses').innerHTML = '';
  document.querySelector('textarea').value = '';
  document.querySelector('#reference-input').value = '';
  document.querySelector('#previews').innerHTML = '';
  document.querySelector('#tools-menu').hidden = true;
  document.querySelector('#chat-actions').hidden = true;
};
document.querySelector('#tools').onclick = () => {
  const menu = document.querySelector('#tools-menu'); menu.hidden = !menu.hidden;
};
document.querySelector('#images').onclick = () => {
  mode = 'images'; // Gemini's checkbox may keep this upload menu open.
};
document.querySelector('#videos').onclick = () => {
  mode = 'videos'; // Deliberately keep Upload files visible after mode selection.
};
document.querySelector('#upload').onclick = () => document.querySelector('#reference-input').click();
document.querySelector('#reference-input').onchange = async event => {
  document.querySelector('#send').disabled = true;
  for (const file of event.target.files) {
    const payload = await file.arrayBuffer();
    const response = await fetch('/reference', {method:'POST', body:payload});
    if (!response.ok) throw new Error('Fixture rejected image upload');
    const receipt = await response.json(); references.push(receipt.id);
    const preview = document.createElement('img');
    preview.src = URL.createObjectURL(file); preview.alt = file.name;
    const card = document.createElement('div'); card.dataset.referencePreview = '';
    card.append(preview); document.querySelector('#previews').append(card);
  }
  document.querySelector('#tools-menu').hidden = true;
  document.querySelector('#send').disabled = false;
};
function showMedia() {
  const media = mode === 'videos' ? `
    <div class="video-card"><video preload="metadata" controls src="/video.mp4?id=${current}"></video>
      <div class="video-overlay" aria-label="Play video overlay">▶</div>
      <button aria-label="Share" aria-haspopup="menu" onclick="document.querySelector('#video-menu').hidden=false">Share</button>
      <div role="menu" id="video-menu" hidden>
        <button role="menuitem" onclick="window.location.href='/download.mp4?id=${current}'">Download video</button>
      </div>
    </div>` : `
    <div class="image-card"><div><img src="/image.png?id=${current}" alt="Local generated fixture"></div>
      <button class="download" aria-label="Download full size"
        onclick="window.location.href='/download.png?id=${current}'">Download full size</button>
    </div>`;
  document.querySelector('#responses').innerHTML = `
    <user-query></user-query>
    <model-response>${media}</model-response>`;
  document.querySelector('user-query').textContent = originalPrompt;
  document.querySelector('#chat-actions').hidden = false;
}
document.querySelector('#send').onclick = async () => {
  if (current !== null) throw new Error('Fixture refuses submission in an existing chat');
  const response = await fetch('/submit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({prompt:document.querySelector('textarea').value, mode, references})
  });
  if (!response.ok) throw new Error('Local fixture rejected submission');
  const result = await response.json();
  current = result.id; originalPrompt = document.querySelector('textarea').value;
  history.replaceState(null, '', '/app/' + current);
  const row = document.createElement('a'); row.href = '/app/' + current;
  row.textContent = current; document.querySelector('#history').append(row);
  showMedia();
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
if (current !== null) showMedia();
</script></body></html>"""


def video_fixture_bytes():
    payload = io.BytesIO()
    with av.open(payload, "w", format="mp4") as output:
        stream = output.add_stream("libx264", rate=10)
        stream.width, stream.height = 96, 64
        stream.pix_fmt = "yuv420p"
        stream.options = {"preset": "ultrafast", "crf": "20"}
        for index in range(12):
            frame = av.VideoFrame.from_image(Image.new("RGB", (96, 64), (30 + 10 * index, 70, 140)))
            frame.pts, frame.time_base = index, Fraction(1, 10)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return payload.getvalue()


class LocalFixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.submissions = []
        self.submission_details = {}
        self.downloads = []
        self.deletions = []
        self.new_chat_clicks = 0
        self.missing_chat_checks = []
        self.reference_receipts = []
        self.video_export_menu_dismissals = 0
        self.chats = {"unrelated-keep": {"prompt": "An unrelated chat must remain"}}
        self.images = {}
        self.video = video_fixture_bytes()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body: bytes, content_type="application/json", attachment=False, filename="fixture.png"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if attachment:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
                        mode = fixture.chats[chat_id].get("mode", "images") if chat_id else "text"
                        rows = "".join(f'<a href="/app/{key}">{key}</a>' for key in fixture.chats)
                    page = PAGE.replace("__HISTORY__", rows)
                    page = page.replace("__NOTICE__", '<div role="status">无法加载此对话。对话不存在或已被删除。</div>' if missing else '')
                    page = page.replace("__RESET_PATH__", "true" if missing else "false")
                    page = page.replace("__CURRENT_CHAT__", json.dumps(chat_id))
                    page = page.replace("__PROMPT__", json.dumps(prompt))
                    page = page.replace("__MODE__", json.dumps(mode))
                    self.reply(page.encode("utf-8"), "text/html; charset=utf-8")
                elif path in {"/image.png", "/download.png", "/video.mp4", "/download.mp4"}:
                    chat_id = parse_qs(parts.query).get("id", [None])[0]
                    with fixture.lock:
                        if chat_id not in fixture.chats or chat_id not in fixture.images:
                            self.send_error(404)
                            return
                        image = fixture.images[chat_id]
                        if path.startswith("/download."):
                            fixture.downloads.append(chat_id)
                    is_video = path.endswith(".mp4")
                    self.reply(fixture.video if is_video else image, "video/mp4" if is_video else "image/png",
                               attachment=path.startswith("/download."), filename="fixture.mp4" if is_video else "fixture.png")
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path == "/dismiss-video-menu":
                    with fixture.lock:
                        fixture.video_export_menu_dismissals += 1
                    self.reply(b'{"ok":true}')
                    return
                if self.path == "/new-chat":
                    with fixture.lock:
                        fixture.new_chat_clicks += 1
                    self.reply(b'{"ok":true}')
                    return
                if self.path == "/reference":
                    payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    with Image.open(io.BytesIO(payload)) as reference:
                        reference.verify()
                    digest = hashlib.sha256(payload).hexdigest()
                    with fixture.lock:
                        fixture.reference_receipts.append({"id": digest, "size_bytes": len(payload)})
                    self.reply(json.dumps({"id": digest}).encode())
                    return
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path == "/submit":
                    mode = payload.get("mode")
                    if mode not in {"images", "videos"} or not payload.get("prompt"):
                        self.send_error(400)
                        return
                    if mode == "videos" and not payload.get("references"):
                        self.send_error(400)
                        return
                    with fixture.lock:
                        chat_id = f"task-{len(fixture.submissions) + 1}"
                        fixture.chats[chat_id] = {"prompt": payload["prompt"], "mode": mode,
                                                  "references": payload.get("references", [])}
                        image = io.BytesIO()
                        Image.new("RGB", (384, 256), (55, 75 + 40 * len(fixture.submissions), 155)).save(image, format="PNG")
                        fixture.images[chat_id] = image.getvalue()
                        fixture.submissions.append(chat_id)
                        fixture.submission_details[chat_id] = payload
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
        reference = root / "reference.png"
        Image.new("RGB", (80, 60), "orange").save(reference)
        reference_info = verify_image(reference)
        blocked_requests = []

        def restrict_request(route):
            if fixture.is_local(route.request.url):
                route.continue_()
            else:
                blocked_requests.append(route.request.url)
                route.abort()

        records = []
        obstructed_video_hover_checks = []
        actual_download = GeminiImages._download

        def download_after_overlay_check(driver, record, media):
            if record.get("media_type") == "video":
                try:
                    media.hover(timeout=500)
                except BrowserTimeoutError:
                    obstructed_video_hover_checks.append(record["id"])
                else:
                    raise AssertionError("The fixture must block direct video hover with its sibling overlay")
            return actual_download(driver, record, media)

        with patch.object(gemini_web, "GEMINI_URL", fixture.app_url), patch.object(
            GeminiImages, "_is_chat_url", staticmethod(fixture.is_chat)
        ), patch.object(GeminiImages, "_download", download_after_overlay_check):
            tasks = [("image", True), ("video", True), ("image", False)]
            for number, (media_type, with_reference) in enumerate(tasks, 1):
                record = store.create(f"Local {media_type} task {number}", profile)
                record.update(media_type=media_type, references=[reference_info] if with_reference else [])
                store.save(record)
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
                    mode = record["video_mode"] if media_type == "video" else record["image_mode"]
                    assert mode == ("tools_video_mode" if media_type == "video" else "tools_image_mode"), record
                    info = verify_media(record["files"][0]["path"], media_type)
                    assert info["media_type"] == media_type, info
                    assert (info["width"], info["height"]) == ((96, 64) if media_type == "video" else (384, 256)), info
                    if media_type == "video":
                        assert info["duration_seconds"] > 0 and info["decoded_frames"] == 12, info
                    chat_id = urlsplit(record["chat_url"]).path.rsplit("/", 1)[-1]
                    received = fixture.submission_details[chat_id]["references"]
                    assert received == ([reference_info["sha256"]] if with_reference else []), received
                    assert bool(record.get("references_uploaded")) == with_reference, record
                    assert set(fixture.chats) == {"unrelated-keep"}, fixture.chats
                    records.append(record)
                finally:
                    context.close()

        assert len(set(fixture.submissions)) == 3, fixture.submissions
        assert fixture.submissions == fixture.downloads == fixture.deletions
        assert fixture.new_chat_clicks == 3, fixture.new_chat_clicks
        assert set(fixture.missing_chat_checks) == set(fixture.deletions), fixture.missing_chat_checks
        assert len({record["chat_url"] for record in records}) == 3
        assert len({record["files"][0]["sha256"] for record in records}) == 3
        assert len(fixture.reference_receipts) == 2, fixture.reference_receipts
        assert fixture.video_export_menu_dismissals == 1, fixture.video_export_menu_dismissals
        assert len(obstructed_video_hover_checks) == 1, obstructed_video_hover_checks
        assert all(receipt["id"] == reference_info["sha256"] for receipt in fixture.reference_receipts)
        for record in records:
            assert store.load(record["id"])["chat_cleanup"]["status"] == "deleted"
            verify_media(record["files"][0]["path"], record["media_type"])
        report.update({
            "status": "passed", "tasks": 3, "modes": ["image_to_image", "image_to_video", "text_to_image"],
            "new_chat_clicks": fixture.new_chat_clicks,
            "submissions": len(fixture.submissions), "downloads": len(fixture.downloads),
            "deletions": len(fixture.deletions), "distinct_task_chat_ids": True,
            "remaining_task_chats": 0, "unrelated_chat_preserved": True, "local_files_verified": True,
            "deleted_targets_rechecked_with_explicit_notice": True,
            "cached_browser_only": True, "blocked_nonlocal_requests": len(blocked_requests),
            "temporary_fixture_files_removed": True,
            "real_reference_uploads": len(fixture.reference_receipts), "reference_bytes_verified": True,
            "upload_menu_preserved_after_mode_selection": True, "video_share_menu_download": True,
            "leftover_video_export_menu_dismissed_before_cleanup": True,
            "direct_video_hover_blocked_by_sibling_overlay": True,
            "video_parent_card_hover_download_succeeded": True,
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
