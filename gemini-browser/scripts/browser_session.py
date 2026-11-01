"""Visible CloakBrowser sessions. Authentication stays inside the local profile."""

from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from cloakbrowser import launch_persistent_context
from local_state import ProfileLock, write_json

GEMINI_URL = "https://gemini.google.com/app"
SIGN_IN = re.compile(r"^(Sign in|Log in|登录|登入|登入帳戶|登录帐号)$", re.I)
ACCOUNT = re.compile(r"Google (Account|帐号|账号|帐户|賬戶|帳戶|帳號)", re.I)


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def redact(text: str) -> str:
    text = re.sub(r"(https?|socks5h?)://[^\s/@]+(?::[^\s/@]*)?@", r"\1://[redacted]@", text)
    text = re.sub(r'(https?://[^\s?<>"\']+)\?[^\s<>"\']*', r'\1?[redacted]', text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    if ACCOUNT.search(text):
        return "Google Account [redacted]"
    return text[:500]


class LoginRequired(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, profile_dir: Path, proxy: str | None = None):
        self.profile_dir = Path(profile_dir).resolve()
        self.lock = ProfileLock(self.profile_dir)
        self.context = None
        self.page = None
        self.proxy = proxy

    def __enter__(self):
        self.lock.__enter__()
        try:
            self.context = launch_persistent_context(
                self.profile_dir,
                headless=False,
                viewport={"width": 1280, "height": 850},
                accept_downloads=True,
                proxy=self.proxy,
                args=["--window-size=1400,1000"],
            )
            self.context.set_default_timeout(8000)
            self.page = next(
                (p for p in self.context.pages if urlsplit(p.url).hostname == "gemini.google.com"),
                None,
            )
            if self.page is None:
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.bring_to_front()
            return self
        except BaseException:
            if self.context:
                with suppress(Exception):
                    self.context.close()
            self.lock.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self.context:
                try:
                    self.context.close()
                except BaseException as close_error:
                    if exc_type is None:
                        raise
                    # Preserve the operation's exception and interruption status.
                    # Reporting a secondary teardown failure must not replace it.
                    with suppress(BaseException):
                        emit("browser_close_failed", error_type=type(close_error).__name__,
                             message=redact(str(close_error)))
        finally:
            self.lock.__exit__(exc_type, exc_value, traceback)

    def navigate(self, url=GEMINI_URL):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def _visible(self, locator) -> bool:
        return any(el.is_visible() for el in locator.all())

    def auth_state(self) -> str:
        if self.page.is_closed():
            raise LoginRequired("浏览器窗口已关闭。请重新运行登录命令。")
        host = urlsplit(self.page.url).hostname or ""
        if host == "accounts.google.com":
            return "login_required"
        if host != "gemini.google.com":
            return "loading"
        if self._visible(self.page.get_by_role("link", name=SIGN_IN)) or self._visible(
            self.page.get_by_role("button", name=SIGN_IN)
        ):
            return "login_required"
        account = self.page.locator(
            '[aria-label*="Google Account"], [aria-label*="Google 帐号"], '
            '[aria-label*="Google 账号"], [aria-label*="Google 帳戶"], '
            '[aria-label*="Google 帳號"], [aria-label*="Google 帐户"], '
            'a[href*="accounts.google.com/SignOutOptions"]'
        )
        editor = self.page.locator('[contenteditable="true"], textarea, [role="textbox"]')
        if self._visible(account) and self._visible(editor):
            return "authenticated"
        return "loading"

    def ensure_login(self, timeout: float = 900) -> bool:
        deadline = time.monotonic() + timeout
        notified = False
        while time.monotonic() < deadline:
            current = self.page
            current_open = current is not None and not current.is_closed()
            current_gemini = current_open and urlsplit(current.url).hostname == "gemini.google.com"
            state = self.auth_state() if current_open else "login_required"
            # A navigated Gemini chat owns this operation, including while its shell
            # is loading. Only look elsewhere when this tab cannot complete login.
            if not current_gemini or state == "login_required":
                best_page = None
                best_state = None
                best_rank = 1 if current_gemini else 0
                try:
                    for candidate in reversed(self.context.pages):
                        if candidate == current or candidate.is_closed():
                            continue
                        if urlsplit(candidate.url).hostname != "gemini.google.com":
                            continue
                        self.page = candidate
                        candidate_state = self.auth_state()
                        rank = {"login_required": 1, "loading": 2, "authenticated": 3}[candidate_state]
                        if rank > best_rank:
                            best_page, best_state, best_rank = candidate, candidate_state, rank
                        if candidate_state == "authenticated":
                            break
                finally:
                    self.page = current
                if best_page is not None:
                    self.page, state = best_page, best_state
            if self.page is None or self.page.is_closed():
                raise LoginRequired("浏览器窗口已关闭。请重新运行登录命令。")
            if state == "authenticated":
                emit("authenticated", message="已确认 Gemini 登录，会话保存在本机专用浏览器目录。")
                return True
            if state == "login_required" and not notified:
                emit(
                    "waiting_for_login",
                    message="请在已打开的 CloakBrowser 窗口中完成 Google 登录和验证；成功后脚本自动继续。",
                    profile_dir=str(self.profile_dir),
                )
                notified = True
            self.page.wait_for_timeout(1500)
        raise LoginRequired("等待登录超时。登录进度保留在专用浏览器目录中，可重新运行 login。")

    def snapshot(self) -> dict:
        # Only describe rendered controls. Never serialize cookies, storage or input values.
        auth_state = self.auth_state()
        if urlsplit(self.page.url).hostname != "gemini.google.com":
            return {"url": "[authentication page]", "auth_state": auth_state, "controls": []}
        controls = self.page.locator("button, [role=button], textarea, [contenteditable=true]").evaluate_all(
            """els => els.filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height)
            .map(e => ({tag: e.tagName, role: e.getAttribute('role'),
            label: e.getAttribute('aria-label') || e.getAttribute('data-placeholder') || '',
            text: e.isContentEditable || e.tagName === 'TEXTAREA' ? '' : (e.innerText || '').slice(0, 150)}))"""
        )
        for control in controls:
            control["label"] = redact(control["label"])
            control["text"] = redact(control["text"])
        return {"url": self.page.url, "auth_state": auth_state, "controls": controls}

    def diagnostics(self, directory: Path) -> dict:
        directory.mkdir(parents=True, exist_ok=True)
        if urlsplit(self.page.url).hostname != "gemini.google.com":
            return {"diagnostics": "登录页面不抓取截图或表单。"}
        info = self.snapshot()
        write_json(directory / "page-controls.json", info)
        self.page.screenshot(path=str(directory / "page.png"), full_page=False)
        return {"controls": str(directory / "page-controls.json"), "screenshot": str(directory / "page.png")}
