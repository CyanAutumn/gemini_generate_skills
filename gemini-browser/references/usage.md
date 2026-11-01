# 使用说明

以下命令从包含 `gemini-browser` 的项目目录运行；从其他目录执行时使用脚本与依赖文件的实际路径。使用本机可用的 Python，建议在项目虚拟环境内安装依赖。

```powershell
python -m pip install -r gemini-browser/requirements.txt
python gemini-browser/scripts/gemini_browser.py --help
python gemini-browser/scripts/gemini_browser.py login --help
python gemini-browser/scripts/gemini_browser.py image --help
python gemini-browser/scripts/gemini_browser.py result --help
python gemini-browser/scripts/gemini_browser.py cleanup --help
```

## 登录与状态

```powershell
python gemini-browser/scripts/gemini_browser.py status
python gemini-browser/scripts/gemini_browser.py login --timeout 900
```

`login` 会启动可见浏览器。用户在窗口内完成 Google 登录、两步验证和必要提示。工具等待成功后，下次复用同一 profile。保存的是浏览器登录态，不是脚本保存的明文账号密码。

Windows 默认 profile 位于 `%LOCALAPPDATA%/GeminiBrowserSkill/profiles/default`。可用 `--profile` 切换独立配置，或用 `--data-dir` 指定私有数据根目录；不要指定日常 Chrome 的配置目录。profile 包含敏感登录信息，不得上传、提交到 Git 或作为调试附件。

同一用户资料目录不能同时供多个浏览器实例使用；[Playwright 官方文档](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)也明确不支持自动化日常 Chrome 的默认资料目录。复制普通 Chrome 目录到专用目录也不能保证 CloakBrowser 免登录：考虑到 Windows Chrome 的[应用绑定加密](https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html)，这是跨浏览器复用登录态可能失效的工程判断，仍应准备人工登录。已有 CloakBrowser 专用资料若采用本工具的 `<data-dir>/profiles/<profile>` 结构，可通过相同的 `--data-dir` 和 `--profile` 复用，先关闭占用该目录的实例。

`status` 返回 `auth_state`：`authenticated` 表示已确认登录，`login_required` 表示页面要求登录，`loading` 表示暂未确认。它不会提交生成请求。需要排查页面控件时可运行 `status --inspect`；截图和控件信息保存在私有数据目录的 `diagnostics` 中，可能包含聊天内容或个人信息，分享前先检查。

## 连接超时与代理

若页面连接超时，先确认这台电脑平时能否打开 Gemini。用户平时需要代理时，显式传入其现有、可用的代理地址，例如：

```powershell
python gemini-browser/scripts/gemini_browser.py status --proxy http://127.0.0.1:7897
```

示例端口不保证适用于本机。也可通过环境变量 `GEMINI_BROWSER_PROXY` 提供地址；命令行 `--proxy` 优先。代理只传给专用浏览器，不修改系统代理，工具不会自动探测或强制使用代理。后续 `login`、`image`、`cleanup` 应沿用所需连接方式。连接超时不能证明登录失效或图片任务未提交，已创建任务仍按任务记录处理。

## 单张图片

```powershell
python gemini-browser/scripts/gemini_browser.py image --prompt "生成一张水彩插画：一只橘猫坐在窗边，窗外是雨后的花园，柔和晨光，横向构图，无文字" --timeout 300
```

较长提示词使用 UTF-8 文本文件，避免命令行转义改变内容；`--prompt` 与 `--prompt-file` 二选一。

```powershell
python gemini-browser/scripts/gemini_browser.py image --prompt-file prompt.txt --output-dir outputs --timeout 300
```

每个 `image` 任务确认新建独立聊天后提交一次请求，等待生成、原生下载并校验本地图片，结束时删除本次聊天。不复用旧聊天继续生成或下载。输出目录默认项目的 `outputs`；预览截图不能替代下载结果。完整成功要求图片校验通过且 `chat_cleanup` 确认删除完成；图片已保存但删除失败时只能报告部分完成，并补删聊天。

`--profile`、`--data-dir`、`--output-dir`、`--proxy` 可写在子命令前或后。后续查看结果或补删时沿用原输出目录，补删还需沿用原数据目录和 profile。`image` 的 `--timeout` 控制生成等待时间；连接页面、登录等待、下载和清理另计，首次登录最多另外等待 900 秒。

## 查看本地结果与补删聊天

```powershell
python gemini-browser/scripts/gemini_browser.py result RUN_ID
python gemini-browser/scripts/gemini_browser.py cleanup RUN_ID
```

把 `RUN_ID` 替换为工具返回的任务 ID。`result` 只重新校验本地已保存图片并返回实际文件，不打开浏览器；缺失或损坏时报告校验错误，不重新生成或恢复网页下载。本地图片和任务记录会保留，删除网页聊天不会删除它们。

`cleanup` 仅补删该任务记录指向的聊天，不生成图片、不继续下载。默认只操作本工具确认新建并记录的任务聊天，不清空历史记录或其他聊天；用户已授权每任务结束删除，无需重复确认。无法唯一确认目标时停止，按任务记录人工核对，不猜测删除对象。

正常失败和 Ctrl+C 退出也会尝试删除聊天；外部强杀、断网、登录失效或页面变化可能导致删除失败。独立字段 `chat_cleanup` 记录清理状态，不能用“图片已生成”替代删除确认；清理失败会明确报告并保留任务 ID，待网络、登录或页面问题解决后用 `cleanup` 补删。程序崩溃时不能保证即时删除。

网页删除方式以[Google 官方帮助](https://support.google.com/gemini/answer/13666746?co=GENIE.Platform%3DDesktop&hl=en)为准：在侧栏准确目标聊天的 More 菜单选择 Delete。删除能力受账号权限影响，工作或学校账号可能无法删除；无法确认删除完成时报告清理失败。

## 状态处理与验收

- 等待人工操作：告诉用户浏览器中需要完成什么，并保留等待任务。
- 登录失效：重新登录后补删原任务聊天，不恢复生成或下载。
- 额度、账号或地区限制：说明页面实际反馈并停止，不假定固定每日次数。
- 已提交但超时：保留任务 ID 并核对清理状态，不盲目再次生成；清理失败时使用 `cleanup` 补删。
- 多条模型回复或下载目标不唯一：停止自动下载并尝试清理该任务聊天，报告实际状态。
- 页面入口变化：依据工具反馈检查实际入口；无法定位时报告阻塞位置，不假装已提交。
- 成功：本地图片校验通过且 `chat_cleanup` 已确认删除，交付文件及实际保存位置。图片已保存但清理失败时说明剩余事项；仅通过帮助、语法或离线检查不能证明网页链路跑通。

当前尚未实现视频和批量生成。网页路线不产生 Gemini API 调用费用，账号订阅、功能权限和网页额度仍适用。

2026-11-01 已在本机 Windows 验证登录态重启复用，以及“明确新建空白聊天 → 单次图片请求 → 原生下载与 Pillow 校验 → 删除 → 网页明确提示聊天不存在”的完整流程。普通输入框的明确图片请求（`prompt_request`）可正常生成；本次图片为 2752 × 1536 JPEG，本地文件在聊天删除后仍有效。另有两次连续独立任务的本地真实浏览器测试，确认各任务分别删除、无关聊天保留。实测对应当时本机账号与页面状态，每次仍以工具的图片校验和 `chat_cleanup` 确认结果为准。

## 官方参考

- [CloakBrowser 官方仓库](https://github.com/CloakHQ/CloakBrowser)：浏览器库及持久化配置，以当前所装版本为准。
- [Gemini 登录要求](https://support.google.com/gemini/answer/13278668?hl=en)：账号资格与登录入口。
- [图片生成、编辑与下载](https://support.google.com/gemini/answer/14286560?co=GENIE.Platform%3DDesktop&hl=en)：网页图片功能及原尺寸下载。
- [Gemini Apps 功能与额度](https://support.google.com/gemini/answer/16275805?hl=en)：功能权限与使用限制。
