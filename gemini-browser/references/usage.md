# 使用说明

以下命令从包含 `gemini-browser` 的项目目录运行；从其他目录执行时使用脚本与依赖文件的实际路径。使用本机可用的 Python，建议在项目虚拟环境内安装依赖。

```powershell
python -m pip install -r gemini-browser/requirements.txt
python gemini-browser/scripts/gemini_browser.py --help
python gemini-browser/scripts/gemini_browser.py login --help
python gemini-browser/scripts/gemini_browser.py image --help
python gemini-browser/scripts/gemini_browser.py video --help
python gemini-browser/scripts/gemini_browser.py result --help
python gemini-browser/scripts/gemini_browser.py cleanup --help
python gemini-browser/scripts/gemini_browser.py workflow --help
```

## 创建和调用固定工作流

需要保存固定提示词、素材、变量及顺序步骤时，读取 [工作流说明](workflows.md)。提供的 [product-demo 示例](../examples/workflows/product-demo/workflow.json)依次执行图生图和图生视频，第二步引用第一步的图片；先通过本地校验与计划，再运行：

```powershell
python gemini-browser/scripts/gemini_browser.py workflow create product-demo --spec gemini-browser/examples/workflows/product-demo/workflow.json
python gemini-browser/scripts/gemini_browser.py workflow validate product-demo
python gemini-browser/scripts/gemini_browser.py workflow plan product-demo --set "subject=陶瓷咖啡杯"
python gemini-browser/scripts/gemini_browser.py workflow run product-demo --set "subject=陶瓷咖啡杯"
```

默认工作流注册目录是当前工作目录下的 `workflows`，可用 `--workflows-dir` 指定。创建会复制提示词和素材，形成可搬移的包；同名流程已存在时不会覆盖。`workflow result RUN_ID` 只查询本地工作流结果。详细变量规则、完整规范、便捷创建及步骤补删方法见工作流说明。

创建、列表、展示、校验、计划与 `run --dry-run` 不打开浏览器。实际运行仍需要本机文件、浏览器与登录权限；工作流固定步骤和约束，不保证每次像素相同。下面已通过的单图和单视频实测不等于完整工作流已经实测成功。

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

示例端口不保证适用于本机。也可通过环境变量 `GEMINI_BROWSER_PROXY` 提供地址；命令行 `--proxy` 优先。代理只传给专用浏览器，不修改系统代理，工具不会自动探测或强制使用代理。后续 `login`、`image`、`video`、`cleanup` 应沿用所需连接方式。连接超时不能证明登录失效或生成任务未提交，已创建任务仍按任务记录处理。

## 文生图与图生图

```powershell
python gemini-browser/scripts/gemini_browser.py image --prompt "生成一张水彩插画：一只橘猫坐在窗边，窗外是雨后的花园，柔和晨光，横向构图，无文字" --timeout 300
```

较长提示词使用 UTF-8 文本文件，避免命令行转义改变内容；`--prompt` 与 `--prompt-file` 二选一。

```powershell
python gemini-browser/scripts/gemini_browser.py image --prompt-file prompt.txt --output-dir outputs --timeout 300
```

图生图在同一条命令中添加参考图片；`--reference` 可重复，每个参数对应一个本地图片文件。工具先校验文件，再上传至当前新任务；多张参考图用于同一张结果，不是批量生成。

```powershell
python gemini-browser/scripts/gemini_browser.py image --reference "reference.png" --prompt "保留参考图的角色与构图，改成柔和的水彩风格"
python gemini-browser/scripts/gemini_browser.py image --reference "character.png" --reference "scene.jpg" --prompt-file prompt.txt
```

## 图生视频

`video` 要求至少一张参考图片，`--reference` 可重复；提示词同样用 `--prompt` 或 `--prompt-file` 二选一。默认生成等待时间为 600 秒，可用 `--timeout` 调整；这不是成片时长设置。

```powershell
python gemini-browser/scripts/gemini_browser.py video --reference "reference.png" --prompt "让参考图中的猫轻轻转头，窗外叶片随风摇动，镜头缓慢推进，保持原有水彩风格" --timeout 600
python gemini-browser/scripts/gemini_browser.py video --reference "character.png" --reference "scene.jpg" --prompt-file video-prompt.txt
```

实际顺序是新建聊天、先上传参考图、再选择可用的视频工具；首次使用可能出现 Omni 介绍弹窗，需处理当前可见弹窗后继续。发送前重新检查附件预览完整，并执行不触发提交的可点击检查（trial），确认发送按钮无遮挡后只提交一次。未找到视频工具入口时，可使用明确的视频提示词请求生成，但普通文字回复、静态图片或参考图预览都不能算视频成功。视频通过网页原生下载项保存，部分界面位于 Share → Download video；只保存本地文件，不发布到其他平台。

[Google 的 Gemini Apps 视频帮助](https://support.google.com/gemini/answer/16126339?co=GENIE.Platform%3DDesktop&hl=en)目前说明最多可上传 5 张图片，生成可能需要数分钟，具体数量与功能仍以账号实际页面为准。该帮助未承诺固定成片秒数，不沿用旧教程的固定时长或每日次数；以当前页面与下载文件的实际结果报告。个人账号的视频功能需要 Google AI 计划及相应年龄资格，工作或学校账号需要合格许可证；没有权限或额度时明确报告并停止。

## 每任务新聊天、下载与删除

每个 `image` 或 `video` 任务确认新建独立聊天；仅在已确认空白的本次专用聊天内清除残留草稿和附件，再上传本次参考图、切换图片或视频工具，发送前重查附件预览和按钮可点击状态，只提交一次。随后等待生成、原生下载并校验本地媒体，结束时删除本次聊天。不复用旧聊天继续生成或下载。输出目录默认项目的 `outputs`；页面截图、上传预览不能替代下载结果。完整成功要求媒体校验通过且 `chat_cleanup` 确认删除完成；文件已保存但删除失败时只能报告部分完成，并补删聊天。

`--profile`、`--data-dir`、`--output-dir`、`--proxy` 可写在子命令前或后。后续查看结果或补删时沿用原输出目录，补删还需沿用原数据目录和 profile。`image` 默认生成等待 300 秒，`video` 默认 600 秒；连接页面、登录等待、上传、下载和清理另计，首次登录最多另外等待 900 秒。

## 查看本地结果与补删聊天

```powershell
python gemini-browser/scripts/gemini_browser.py result RUN_ID
python gemini-browser/scripts/gemini_browser.py cleanup RUN_ID
```

把 `RUN_ID` 替换为工具返回的任务 ID。`result` 只重新校验本地已保存图片或视频并返回实际文件，不打开浏览器；缺失或损坏时报告校验错误，不重新生成或恢复网页下载。本地媒体和任务记录会保留，删除网页聊天不会删除它们。

`cleanup` 仅补删该任务记录指向的聊天，不生成媒体、不继续下载。默认只操作本工具确认新建并记录的任务聊天，不清空历史记录或其他聊天；用户已授权每任务结束删除，无需重复确认。无法唯一确认目标时停止，按任务记录人工核对，不猜测删除对象。

正常失败和 Ctrl+C 退出也会尝试删除聊天；外部强杀、断网、登录失效或页面变化可能导致删除失败。独立字段 `chat_cleanup` 记录清理状态，不能用“媒体已生成”替代删除确认；清理失败会明确报告并保留任务 ID，待网络、登录或页面问题解决后用 `cleanup` 补删。程序崩溃时不能保证即时删除。

网页删除方式以[Google 官方帮助](https://support.google.com/gemini/answer/13666746?co=GENIE.Platform%3DDesktop&hl=en)为准：在侧栏准确目标聊天的 More 菜单选择 Delete。删除能力受账号权限影响，工作或学校账号可能无法删除；无法确认删除完成时报告清理失败。

## 状态处理与验收

- 等待人工操作：告诉用户浏览器中需要完成什么，并保留等待任务。
- 登录失效：重新登录后补删原任务聊天，不恢复生成或下载。
- 额度、账号或地区限制：说明页面实际反馈并停止，不假定固定每日次数。
- 已提交但超时：保留任务 ID 并核对清理状态，不盲目再次生成；清理失败时使用 `cleanup` 补删。
- 多条模型回复或下载目标不唯一：停止自动下载并尝试清理该任务聊天，报告实际状态。
- 页面入口变化：依据工具反馈检查实际入口；无法定位时报告阻塞位置，不假装已提交。
- 成功：本地媒体校验通过且 `chat_cleanup` 已确认删除，交付文件及实际保存位置。文件已保存但清理失败时说明剩余事项；仅通过帮助、语法或离线检查不能证明网页链路跑通。

当前不支持批量生成，也不接受视频文件作为参考输入。网页路线不产生 Gemini API 调用费用，账号订阅、功能权限和网页额度仍适用；不硬编码额度，按页面限制及恢复时间处理。

2026-11-01 已在本机 Windows 验证登录态重启复用，以及“明确新建空白聊天 → 单次文生图请求 → 原生下载与 Pillow 校验 → 删除 → 网页明确提示聊天不存在”的完整流程。普通输入框的明确图片请求（`prompt_request`）可正常生成，本地文件在聊天删除后仍有效；另有两次连续独立任务的本地真实浏览器测试，确认各任务分别删除、无关聊天保留。

同日图生图实测也已完成：参考图中的蓝色杯子按提示变为绿色杯子与暖夕阳光线；工具确认参考图上传，使用图片工具模式（`tools_image_mode`），下载 2752 × 1536 JPEG，通过 Pillow 校验与视觉核对，并在删除后取得 `chat_not_found` 证据。

同日图生视频已完成真实全流程：确认参考图上传，使用视频工具模式（`tools_video_mode`），原生下载 MP4/H.264（1280 × 720，10.0 秒，4,592,406 字节）；PyAV 完整解码校验 240 帧通过，聊天删除后取得 `chat_not_found` 证据，命令成功退出。首次 Omni 介绍弹窗及发送前遮挡检查已适配。10 秒仅为本次结果，后续任务仍以实际下载文件的校验结果及 `chat_cleanup` 删除确认为准，不承诺固定时长或所有账号均可生成。

## 官方参考

- [CloakBrowser 官方仓库](https://github.com/CloakHQ/CloakBrowser)：浏览器库及持久化配置，以当前所装版本为准。
- [Gemini 登录要求](https://support.google.com/gemini/answer/13278668?hl=en)：账号资格与登录入口。
- [图片生成、编辑与下载](https://support.google.com/gemini/answer/14286560?co=GENIE.Platform%3DDesktop&hl=en)：网页图片功能及原尺寸下载。
- [视频生成与下载](https://support.google.com/gemini/answer/16126339?co=GENIE.Platform%3DDesktop&hl=en)：Gemini Apps 的参考图视频流程，不是 Gemini API 或 Flow。
- [Gemini Apps 功能与额度](https://support.google.com/gemini/answer/16275805?hl=en)：功能权限与使用限制。
