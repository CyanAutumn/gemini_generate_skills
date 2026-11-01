"""Gemini images: a fresh chat per task, native download, then targeted deletion."""

from __future__ import annotations

import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import TimeoutError as BrowserTimeoutError

from browser_session import GEMINI_URL, LoginRequired, emit, redact
from local_state import utc_now, verify_image, verify_video

IMAGE_MODE = re.compile(
    r"^(Create images?|Generate images?|生成图片|生成图像|创建图片|建立圖像|製作圖像|制作图片)(\s.*)?$", re.I
)
TOOLS = re.compile(r"^(Tools|工具|上传和工具|Uploads? (?:and|&) tools)(\s.*)?$", re.I)
VIDEO_MODE = re.compile(r"^(Create videos?|Generate videos?|Videos?|生成视频|制作视频|创建视频|视频|製作影片|建立影片)(\s.*)?$", re.I)
ADD_FILES = re.compile(r"^(Add files?|Add images?|Upload(?:s? (?:and|&) tools)?|添加文件|添加图片|上传和工具|上传图片|上傳檔案)(\s.*)?$", re.I)
UPLOAD_FILES = re.compile(r"^(Upload files?|Upload from (?:computer|device)|上传文件|从设备上传|上傳檔案)(?:[\s.。]|$)", re.I)
SHARE_MENU = re.compile(r"^(Share(?: and export)?|分享(?:和导出)?|共享(?:和导出)?|分享與匯出)$", re.I)
SEND = re.compile(r"^(Send( message)?|Submit|发送(消息)?|提交|傳送|送出)$", re.I)
STOP = re.compile(r"^(Stop( response| generating)?|停止(回答|回复|生成)?|停止回覆)$", re.I)
DOWNLOAD = re.compile(r"download|下载|下載", re.I)
NEW_CHAT = re.compile(r"^(New chat|Start (?:a )?new chat|发起新对话|新建聊天|新聊天|新对话|開始新對話)(\s.*)?$", re.I)
CHAT_MENU = re.compile(r"打开对话操作菜单|開啟對話操作選單|Open (?:chat|conversation) (?:actions )?menu", re.I)
DELETE = re.compile(r"^(Delete(?: chat| conversation)?|删除(?:对话|聊天)?|刪除(?:對話|聊天)?)$", re.I)
MORE = re.compile(r"more|更多选项|更多選項|更多", re.I)
MISSING_CHAT = re.compile(
    r"(?:对话|聊天|對話|conversation|chat).{0,100}(?:不存在|已被删除|已刪除|does(?:n['’]t| not) exist|has been deleted|not found)", re.I
)
BLOCKED = re.compile(
    r"limit (?:has been )?reached|reached (?:your |the )?.{0,45}limit|"
    r"out of (?:image |daily )?(?:generations|credits)|try again tomorrow|"
    r"image generation.{0,50}(?:unavailable|not available)|"
    r"video generation.{0,50}(?:unavailable|not available)|(?:can['’]?t|cannot|unable to).{0,25}(?:create|generate).{0,15}video|"
    r"(?:已达|达到|超出).{0,35}(?:上限|限额)|(?:次数|额度).{0,20}(?:用完|耗尽)|"
    r"(?:无法|不能|暂不支持).{0,15}(?:生成图片|生成图像|生成视频|制作视频)", re.I
)


class GenerationTimeout(RuntimeError):
    pass


class ChatCleanupError(RuntimeError):
    pass


class GeminiImages:
    def __init__(self, session, store):
        self.session = session
        self.store = store
        self.page = session.page

    def _first_visible(self, locator):
        for item in locator.all():
            if item.is_visible():
                return item
        return None

    def _control(self, pattern, scope=None):
        scope = scope if scope is not None else self.page
        for role in ("button", "menuitem", "menuitemcheckbox", "menuitemradio", "option"):
            control = self._first_visible(scope.get_by_role(role, name=pattern))
            if control is not None:
                return control
        return None

    def _wait_control(self, pattern, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            control = self._control(pattern)
            if control is not None:
                return control
            self.page.wait_for_timeout(250)
        return None

    def _dismiss_menu(self):
        self.page.keyboard.press("Escape")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not any(menu.is_visible() for menu in self.page.get_by_role("menu").all()):
                return
            self.page.wait_for_timeout(100)
        raise RuntimeError("工具菜单未能关闭，已停止后续操作。")

    def _editor(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            editor = self._first_visible(self.page.get_by_role("textbox"))
            if editor is None:
                editor = self._first_visible(self.page.locator('[contenteditable="true"], textarea'))
            if editor is not None:
                return editor
            self.page.wait_for_timeout(500)
        raise RuntimeError("没有找到 Gemini 提示词输入框。请检查当前页面或登录状态。")

    def _select_image_mode(self):
        if self._control(re.compile(r"取消选择.{0,3}(?:图片|图像)|Deselect.{0,8}image", re.I)) is not None:
            return "image_already_selected"
        control = self._control(IMAGE_MODE)
        if control is not None:
            control.click()
            return "image_button"
        tools = self._control(TOOLS)
        if tools is not None:
            tools.click()
            control = self._wait_control(IMAGE_MODE)
            if control is not None:
                control.click()
                if any(menu.is_visible() for menu in self.page.get_by_role("menu").all()):
                    self._dismiss_menu()
                return "tools_image_mode"
            self._dismiss_menu()
        # Gemini also accepts an explicit image request in the normal composer.
        return "prompt_request"

    def _select_video_mode(self):
        if self._control(re.compile(r"取消选择.{0,3}视频|Deselect.{0,8}video", re.I)) is not None:
            return "video_already_selected"
        for role in ("button", "menuitem", "menuitemcheckbox", "option"):
            control = self._first_visible(self.page.get_by_role(role, name=VIDEO_MODE))
            if control is not None:
                control.click()
                self._editor()
                return "video_button"
        tools = self._control(TOOLS)
        if tools is not None:
            tools.click()
            control = self._wait_control(VIDEO_MODE, 30)
            if control is not None:
                control.click()
                if any(menu.is_visible() for menu in self.page.get_by_role("menu").all()):
                    self._dismiss_menu()
                return "tools_video_mode"
            self._dismiss_menu()
        # Some Gemini layouts expose generation through the normal composer.
        # Accept only an actual downloadable video, never a text/image fallback.
        return "prompt_request"

    def _upload_references(self, record):
        references = record.get("references") or []
        if not references:
            return
        paths = []
        for reference in references:
            checked = verify_image(reference["path"])
            if reference.get("sha256") and checked["sha256"] != reference["sha256"]:
                raise ValueError("参考图片在验证后发生变化，已停止上传。")
            paths.append(checked["path"])
        inputs = self.page.locator('input[type="file"]')
        if inputs.count() == 1:
            if len(paths) > 1 and inputs.first.get_attribute("multiple") is None:
                raise RuntimeError("当前上传入口一次只接受一张参考图。")
            inputs.first.set_input_files(paths)
        else:
            upload = self._control(UPLOAD_FILES)
            # Gemini can replace its initial composer during hydration. A click
            # on that replaced toolbar may be lost. Retry only opening this local
            # menu, and only when it is visibly closed; never retry submission.
            for attempt in range(3):
                if upload is not None:
                    break
                menus_open = any(menu.is_visible() for menu in self.page.get_by_role("menu").all())
                if not menus_open:
                    add = self._control(ADD_FILES)
                    if add is None:
                        raise RuntimeError("没有找到当前聊天的参考图上传入口。")
                    add.click()
                upload = self._wait_control(UPLOAD_FILES, 10)
            if upload is None:
                raise RuntimeError("上传菜单中没有找到本地文件入口。")
            with self.page.expect_file_chooser(timeout=10000) as pending:
                upload.click()
            chooser = pending.value
            if len(paths) > 1 and not chooser.is_multiple():
                raise RuntimeError("当前上传入口一次只接受一张参考图。")
            chooser.set_files(paths)
        deadline = time.monotonic() + 120
        stable = 0
        self._uploaded_reference_count = len(paths)
        while time.monotonic() < deadline:
            stable = stable + 1 if self._reference_previews_ready(len(paths)) else 0
            if stable >= 2:
                record["references_uploaded"] = True
                self.store.save(record)
                emit("references_uploaded", run_id=record["id"], count=len(paths))
                return
            self.page.wait_for_timeout(500)
        raise RuntimeError("参考图上传后未能确认全部预览就绪，已停止提交。")

    def _reference_previews_ready(self, count):
        # Gemini's composer uses attachment chips; never count avatars, history
        # thumbnails or response images as evidence that a reference was uploaded.
        cards = self.page.locator('mat-basic-chip:has(img), mat-chip:has(img), [data-reference-preview]')
        visible = [card for card in cards.all() if card.is_visible()]
        if len(visible) != count:
            return False
        for card in visible:
            errors = card.locator('[role="alert"], [data-upload-error]')
            if any(error.is_visible() and error.inner_text().strip() for error in errors.all()):
                raise RuntimeError("参考图片上传失败，已停止提交。")
            busy = card.locator('[role="progressbar"], [aria-busy="true"], mat-progress-spinner')
            if any(item.is_visible() for item in busy.all()):
                return False
            images = card.locator("img")
            if images.count() != 1 or not images.first.evaluate("e=>e.complete&&e.naturalWidth>0"):
                return False
        return True

    def _ready_send(self):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            self._accept_video_intro()
            send = self._control(SEND)
            expected = getattr(self, "_uploaded_reference_count", 0)
            if (send is not None and send.is_enabled()
                    and (not expected or self._reference_previews_ready(expected))):
                try:
                    # A visible/enabled button may still be covered by onboarding.
                    # This performs only actionability checks, never a submission.
                    send.click(trial=True, timeout=1000)
                    return send
                except BrowserTimeoutError:
                    pass
            self.page.wait_for_timeout(500)
        raise RuntimeError("参考图上传后发送按钮未就绪，已停止提交。")

    def _accept_video_intro(self):
        # This observed informational introduction has no purchase, permission or
        # authentication action. Do not accept generic dialogs or upgrade offers.
        title = self._first_visible(self.page.get_by_text("创作视频", exact=True))
        model = self._first_visible(self.page.get_by_text("Gemini Omni 加持", exact=True))
        start = self._first_visible(self.page.get_by_role("button", name="立即体验", exact=True))
        if title is not None and model is not None and start is not None:
            start.click()
            start.wait_for(state="hidden", timeout=10000)

    @staticmethod
    def _is_chat_url(url):
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == "gemini.google.com"
                and re.fullmatch(r"/app/[A-Za-z0-9_-]+/?", parts.path) is not None)

    def _remember_url(self, record):
        # Capture once, only after a verified fresh-chat submission. Never replace
        # the owned URL if somebody navigates to a different conversation.
        if (record.get("new_chat_verified") and not record.get("chat_url")
                and record.get("status") in {"submitting", "submitted", "downloading", "download_failed", "timed_out", "blocked"}
                and self._is_chat_url(self.page.url) and self._prompt_matches(record)):
            record["chat_url"] = self.page.url
            self.store.save(record)
            emit("chat_saved", run_id=record["id"], chat_url=record["chat_url"])

    @staticmethod
    def _normalized(text):
        return re.sub(r"\s+", "", text or "")

    def _prompt_matches(self, record):
        queries = self.page.locator('user-query, [data-message-author-role="user"]')
        expected = self._normalized(record.get("submitted_prompt"))
        return (bool(expected) and queries.count() == 1
                and expected in self._normalized(queries.first.text_content()))

    def _new_chat(self, record):
        record["chat_policy"] = "new_then_delete"
        record["new_chat_verified"] = False
        record["chat_cleanup"] = {"status": "pending"}
        self.store.save(record)
        self.session.navigate(GEMINI_URL)
        deadline = time.monotonic() + 30
        new_chat = None
        while time.monotonic() < deadline:
            for role in ("link", "button"):
                new_chat = self._first_visible(self.page.get_by_role(role, name=NEW_CHAT))
                if new_chat is not None:
                    break
            if new_chat is not None:
                break
            self.page.wait_for_timeout(500)
        if new_chat is None:
            raise RuntimeError("没有找到新建聊天入口，已停止；不会在已有聊天中提交。")
        new_chat.click()
        editor = self._editor()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = urlsplit(self.page.url)
            messages = self.page.locator('user-query, model-response, [data-message-author-role]')
            if (current.hostname == urlsplit(GEMINI_URL).hostname and current.path.rstrip("/") == "/app"
                    and not messages.count()):
                break
            self.page.wait_for_timeout(250)
        else:
            raise RuntimeError("未能确认新建聊天为空白，已停止；不会复用原聊天。")
        # Clicking New chat on the already-empty /app route may preserve an
        # unsent composer draft. This dedicated profile must start each task
        # without text or attachments from the preceding task.
        editor.fill("")
        cards = self.page.locator('mat-basic-chip:has(img), mat-chip:has(img), [data-reference-preview]')
        for _ in range(20):
            visible = [card for card in cards.all() if card.is_visible()]
            if not visible:
                break
            remove = self._control(re.compile(r"关闭附件|移除附件|Remove (?:attachment|file)|Close attachment", re.I), visible[0])
            if remove is None:
                raise RuntimeError("新聊天仍有旧附件且无法清除，已停止提交。")
            remove.click()
            self.page.wait_for_timeout(100)
        else:
            raise RuntimeError("新聊天旧附件未能清除，已停止提交。")
        record["new_chat_verified"] = True
        self.store.save(record)
        emit("new_chat", run_id=record["id"], message="已确认空白新聊天，本任务结束后删除。")

    def _image_sources(self):
        return set(self.page.locator("img").evaluate_all("els => els.map(e => e.currentSrc || e.src)"))

    def generate(self, record, timeout):
        failure = None
        try:
            self._new_chat(record)
            media_type = record.get("media_type", "image")
            if media_type not in {"image", "video"}:
                raise ValueError("任务媒体类型只能是 image 或 video。")
            if media_type == "video" and not record.get("references"):
                raise ValueError("图生视频任务必须提供参考图片。")
            # Upload on the fresh composer before changing tools. Current Gemini
            # keeps references when switching image/video tools, and readiness is
            # checked again before sending to catch a layout that clears them.
            self._upload_references(record)
            mode = self._select_video_mode() if media_type == "video" else self._select_image_mode()
            record["video_mode" if media_type == "video" else "image_mode"] = mode
            if self.page.locator('user-query, model-response, [data-message-author-role]').count():
                raise RuntimeError("切换生成模式后不是空白聊天，已停止提交。")
            editor = self._editor()
            if media_type == "video":
                instruction = "请基于上传的参考图片生成一段视频，而不是图片或文字说明："
            elif record.get("references"):
                instruction = "请根据上传的参考图片和以下要求生成一张图片，而不是文字说明："
            else:
                instruction = "请生成一张图片，而不是文字说明："
            submitted_prompt = instruction + "\n\n" + record["prompt"]
            editor.fill(submitted_prompt)
            baseline = self._image_sources()
            send = self._ready_send() if record.get("references") else self._control(SEND)
            # Save before the only non-idempotent UI action. Never auto-retry this action.
            record["submitted_prompt"] = submitted_prompt
            record["submission_attempted"] = True
            record["status"] = "submitting"
            self.store.save(record)
            emit("submitting", run_id=record["id"], media_type=media_type, mode=mode)
            if send is not None:
                send.click(timeout=15000)
            else:
                editor.press("Enter", timeout=15000)
            record["status"] = "submitted"
            self._remember_url(record)
            self.store.save(record)
            self._wait_and_download(record, timeout, baseline)
        except BaseException as exc:
            failure = exc
            self._record_error(record, exc)
            raise
        finally:
            try:
                self.cleanup(record)
            except BaseException as cleanup_exc:
                if failure is None:
                    raise
                emit("cleanup_failed", run_id=record["id"], message=redact(str(cleanup_exc)),
                     action="请使用 cleanup 补删本任务聊天，不要复用原聊天。")
        emit("succeeded", run_id=record["id"], files=record["files"], chat_cleanup=record["chat_cleanup"])

    def cleanup(self, record):
        state = record.get("chat_cleanup", {}).get("status")
        if state in {"deleted", "not_created"}:
            return
        try:
            self._remember_url(record)
            if not record.get("chat_url"):
                if (record.get("status") in {"prepared", "waiting_for_login", "failed_before_submit"}
                        and not record.get("submission_attempted") and not record.get("submitted_prompt")):
                    record["chat_cleanup"] = {"status": "not_created", "updated_at": utc_now()}
                    self.store.save(record)
                    return
                raise ChatCleanupError("已尝试提交但没有取得聊天链接，无法安全定位清理目标。请人工检查本次新聊天。")
            if not self._is_chat_url(record["chat_url"]):
                raise ChatCleanupError("清理目标不是 Gemini 独立聊天地址，拒绝删除。")
            record["chat_cleanup"] = {"status": "deleting", "updated_at": utc_now()}
            self.store.save(record)
            evidence = self._delete_task_chat(record)
            record["chat_cleanup"] = {"status": "deleted", "updated_at": utc_now(),
                                      **(evidence if isinstance(evidence, dict) else {})}
            self.store.save(record)
            emit("chat_deleted", run_id=record["id"], message="本任务聊天已删除，本地图片和记录保留。")
        except BaseException as exc:
            record["chat_cleanup"] = {"status": "failed", "error": redact(str(exc)), "updated_at": utc_now()}
            self.store.save(record)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise ChatCleanupError("任务聊天清理未完成：" + redact(str(exc))) from exc

    def _validate_task_chat(self, record):
        if not self._same_chat(self.page.url, record["chat_url"]):
            raise ChatCleanupError("当前页面与任务聊天地址不符，拒绝删除。当前路径="
                                   + urlsplit(self.page.url).path + "，目标路径=" + urlsplit(record["chat_url"]).path)
        if self.session.auth_state() != "authenticated":
            raise ChatCleanupError("登录状态未确认，无法删除任务聊天。")
        # Legacy runs predate the explicit ownership marker. They are deletable
        # only when their unique submitted prompt matches this one-turn chat.
        if not record.get("new_chat_verified"):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._prompt_matches(record):
                    return
                self.page.wait_for_timeout(500)
            raise ChatCleanupError("无法确认旧记录中的提示词属于当前聊天，拒绝删除。")

    @staticmethod
    def _same_chat(first, second):
        a, b = urlsplit(first), urlsplit(second)
        return (a.scheme, a.hostname, a.path.rstrip("/")) == (b.scheme, b.hostname, b.path.rstrip("/"))

    def _open_chat_menu(self, record):
        if any(menu.is_visible() for menu in self.page.get_by_role("menu").all()):
            self._dismiss_menu()
        controls = [b for b in self.page.get_by_role("button", name=CHAT_MENU).all() if b.is_visible()]
        if len(controls) == 1:
            controls[0].click()
            return
        path = urlsplit(record["chat_url"]).path.rstrip("/")
        row = self.page.locator(f'a[href="{path}"], a[href="{record["chat_url"]}"]')
        if row.count() != 1:
            raise ChatCleanupError("没有唯一匹配本任务的聊天行，拒绝操作其他聊天。")
        row.hover()
        container = row
        for _ in range(3):
            hrefs = container.evaluate(
                "e => [e, ...e.querySelectorAll('a[href]')]"
                ".filter(n => n.matches('a[href]')).map(n => n.getAttribute('href'))"
            )
            chat_links = [urljoin(record["chat_url"], href) for href in hrefs if href]
            chat_links = [url for url in chat_links if self._is_chat_url(url)]
            # A sidebar/list ancestor may also contain a different chat's More
            # button. Never inspect its controls beyond the unique target row.
            if len(chat_links) != 1 or not self._same_chat(chat_links[0], record["chat_url"]):
                break
            buttons = [b for b in container.get_by_role("button", name=MORE).all() if b.is_visible()]
            if len(buttons) == 1:
                buttons[0].click()
                return
            if len(buttons) > 1:
                break
            container = container.locator("..")
        raise ChatCleanupError("没有找到本任务聊天的菜单按钮。")

    def _delete_task_chat(self, record):
        if not self._same_chat(self.page.url, record["chat_url"]):
            self.session.navigate(record["chat_url"])
        self.session.ensure_login(60)
        self.page = self.session.page
        # Give a newly opened chat time to render its own header controls.
        self._editor()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            missing = self._missing_chat_notice()
            if missing and not self.page.locator('user-query, [data-message-author-role="user"]').count():
                return {"evidence": "chat_not_found", "already_absent": True}
            if self._same_chat(self.page.url, record["chat_url"]) and self._prompt_matches(record):
                break
            self.page.wait_for_timeout(500)
        else:
            raise ChatCleanupError("未能确认任务聊天已加载或已不存在，拒绝删除其他聊天。")
        self._validate_task_chat(record)
        self._open_chat_menu(record)
        menus = self.page.get_by_role("menu")
        menus.last.wait_for(state="visible", timeout=10000)
        delete = self._control(DELETE, menus.last)
        if delete is None:
            raise ChatCleanupError("本任务菜单没有删除入口，账号可能不支持删除。")
        delete.click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            dialogs = [d for d in self.page.get_by_role("dialog").all() if d.is_visible()]
            if dialogs:
                if len(dialogs) != 1:
                    raise ChatCleanupError("删除确认窗口不唯一，已停止。")
                confirm = self._control(DELETE, dialogs[0])
                if confirm is None:
                    raise ChatCleanupError("未找到当前聊天的删除确认按钮。")
                confirm.click()
                break
            if not self._same_chat(self.page.url, record["chat_url"]):
                break
            self.page.wait_for_timeout(250)
        return self._verify_chat_deleted(record)

    def _missing_chat_notice(self):
        notices = self.page.locator('[role="alert"], [role="status"], mat-snack-bar-container')
        for notice in notices.all():
            if notice.is_visible() and MISSING_CHAT.search(notice.inner_text()):
                return True
        return False

    def _verify_chat_deleted(self, record):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not self._same_chat(self.page.url, record["chat_url"]):
                break
            self.page.wait_for_timeout(500)
        else:
            raise ChatCleanupError("删除后仍停留在任务聊天，尚未确认清理成功。")
        # Revisit only to verify absence, never to continue or generate. A hidden
        # sidebar, delayed history or pagination is not evidence of deletion.
        self.session.navigate(record["chat_url"])
        self.session.ensure_login(60)
        self.page = self.session.page
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._missing_chat_notice() and not self.page.locator('user-query, [data-message-author-role="user"]').count():
                return {"evidence": "chat_not_found"}
            if self._prompt_matches(record):
                raise ChatCleanupError("删除后仍能打开本任务聊天，未确认清理成功。")
            self.page.wait_for_timeout(500)
        raise ChatCleanupError("没有取得网页明确的聊天不存在提示，不能确认删除成功。")

    def _generated_images(self, baseline):
        responses = self.page.locator('model-response, [data-message-author-role="assistant"]')
        response_count = responses.count()
        if not response_count:
            return []
        if response_count != 1:
            raise RuntimeError("原聊天包含多条回复，无法唯一确认本任务的图片，已停止下载。请人工核对原聊天，勿重新提交。")
        scope = responses.last
        candidates = []
        for image in scope.locator("img").all():
            if not image.is_visible():
                continue
            info = image.evaluate("e => ({src:e.currentSrc || e.src, complete:e.complete, w:e.naturalWidth, h:e.naturalHeight})")
            if info["src"] not in baseline and info["complete"] and info["w"] >= 256 and info["h"] >= 256:
                candidates.append((image, info["src"]))
        return candidates

    def _generated_videos(self):
        responses = self.page.locator('model-response, [data-message-author-role="assistant"]')
        if not responses.count():
            return []
        if responses.count() != 1:
            raise RuntimeError("本任务出现多条回复，无法唯一确认视频，已停止自动下载。")
        scope = responses.last
        candidates = []
        for video in scope.locator("video").all():
            if not video.is_visible():
                continue
            info = video.evaluate("e=>({src:e.currentSrc||e.src||e.querySelector('source')?.src||'',ready:e.readyState,w:e.videoWidth,h:e.videoHeight,duration:Number.isFinite(e.duration)?e.duration:0})")
            if info["src"] and info["ready"] >= 1 and info["w"] > 0 and info["h"] > 0 and info["duration"] > 0:
                candidates.append((video, info["src"]))
        return candidates

    def _blocked_reason(self):
        responses = self.page.locator('model-response, [data-message-author-role="assistant"], [role="alert"]')
        text = "\n".join(x.inner_text() for x in responses.all() if x.is_visible())
        for line in text.splitlines():
            if BLOCKED.search(line):
                return redact(line.strip())
        return None

    def _wait_and_download(self, record, timeout, baseline):
        deadline = time.monotonic() + timeout
        next_report = 0
        previous_source = None
        stable = 0
        while time.monotonic() < deadline:
            self._remember_url(record)
            if self.session.auth_state() == "login_required":
                raise LoginRequired("会话已失效；本任务停止，登录后可用 cleanup 补删任务聊天。")
            if record.get("chat_url") and not self._same_chat(self.page.url, record["chat_url"]):
                raise RuntimeError("当前页面已离开本任务聊天，停止生成等待。")
            blocked = self._blocked_reason()
            if blocked:
                record["status"] = "blocked"
                record["error"] = blocked
                self.store.save(record)
                raise RuntimeError("Gemini 页面反馈：" + blocked)
            is_video = record.get("media_type") == "video"
            candidates = self._generated_videos() if is_video else self._generated_images(baseline)
            stopping = self._control(STOP)
            if candidates and stopping is None:
                source = candidates[0][1]
                stable = stable + 1 if source == previous_source else 1
                previous_source = source
                if stable >= 2:
                    self._download(record, candidates[0][0])
                    return
            else:
                stable = 0
            if time.monotonic() >= next_report:
                emit("generating", run_id=record["id"], chat_url=record.get("chat_url"),
                     message="等待 Gemini 视频生成；不会重新提交提示词。" if is_video else "等待 Gemini 图片生成；不会重新提交提示词。")
                next_report = time.monotonic() + 15
            self.page.wait_for_timeout(1500)
        record["status"] = "timed_out"
        self.store.save(record)
        raise GenerationTimeout("等待生成超时，任务结束并尝试删除本次聊天；不会复用聊天或自动重新生成。")

    def _download(self, record, image):
        media_type = record.get("media_type", "image")
        # Gemini overlays a play button on videos. Hovering the video element
        # itself waits forever for that sibling overlay to disappear; its card
        # legitimately owns both elements and reveals the download controls.
        (image.locator("..") if media_type == "video" else image).hover()
        self.page.wait_for_timeout(500)
        button = None
        container = image
        share_scope = None
        for _ in range(10 if media_type == "video" else 7):
            container = container.locator("..")
            # Do not widen a card search into the whole chat or another message.
            tag = container.evaluate("e => e.tagName.toLowerCase()")
            images = (container.locator("video").count() if media_type == "video" else
                      container.locator("img").evaluate_all("els => els.filter(e => e.naturalWidth >= 256 && e.naturalHeight >= 256).length"))
            if tag in {"body", "html", "main", "model-response"} or images > 1:
                break
            buttons = [b for b in container.get_by_role("button", name=DOWNLOAD).all() if b.is_visible()]
            if len(buttons) == 1:
                button = buttons[0]
                break
            if len(buttons) > 1:
                break
            if media_type == "video" and self._video_export_menu(container) is not None:
                share_scope = container
        if button is None and share_scope is not None:
            share = self._video_export_menu(share_scope)
            share.click()
            self.page.get_by_role("menu").last.wait_for(state="visible", timeout=10000)
            menus = [m for m in self.page.get_by_role("menu").all() if m.is_visible()]
            if len(menus) != 1:
                raise RuntimeError("视频导出菜单不唯一，已停止下载。")
            button = self._control(re.compile(r"^(Download video|下载视频|下載影片)$", re.I), menus[0])
        if button is None:
            record["status"] = "download_failed"
            self.store.save(record)
            raise RuntimeError("结果已出现，但没有找到对应媒体的原生下载按钮。任务结束并尝试删除本次聊天。")
        record["status"] = "downloading"
        self.store.save(record)
        emit("downloading", run_id=record["id"])
        try:
            with self.page.expect_download(timeout=120000 if media_type == "video" else 60000) as pending:
                button.click()
            download = pending.value
            destination = self.store.path_for(record["id"]).parent / (media_type + ".download")
            download.save_as(str(destination))
            info = verify_video(destination) if media_type == "video" else verify_image(destination)
            extensions = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif",
                          "MP4": ".mp4", "WEBM": ".webm", "MOV": ".mov", "MKV": ".mkv", "AVI": ".avi"}
            final_path = destination.with_name(media_type + extensions[info["format"]])
            destination.replace(final_path)
            info["path"] = str(final_path.resolve())
            record["files"] = [info]
            record["status"] = "succeeded"
            record.pop("error", None)
            self.store.save(record)
            emit(media_type + "_saved", run_id=record["id"], files=record["files"])
        except BaseException:
            record["status"] = "download_failed"
            self.store.save(record)
            raise

    def _video_export_menu(self, scope):
        buttons = []
        for button in scope.get_by_role("button", name=SHARE_MENU).all():
            if not button.is_visible():
                continue
            name = (button.get_attribute("aria-label") or button.inner_text()).strip()
            explicit = re.search(r"and export|和导出|與匯出", name, re.I)
            if explicit or button.get_attribute("aria-haspopup") in {"menu", "true"}:
                buttons.append(button)
        return buttons[0] if len(buttons) == 1 else None

    def _record_error(self, record, exc):
        with suppress(Exception):
            self._remember_url(record)
            record["error"] = redact(str(exc))
            if isinstance(exc, LoginRequired):
                record["status"] = "waiting_for_login"
            elif record["status"] in {"prepared", "waiting_for_login"}:
                record["status"] = "failed_before_submit"
            self.store.save(record)
            # Failure evidence is local; it must never include Google login forms.
            record["diagnostics"] = self.session.diagnostics(self.store.path_for(record["id"]).parent / "diagnostics")
            self.store.save(record)
