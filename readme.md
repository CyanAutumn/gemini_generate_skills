# Gemini Browser Skill

通过 [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) 操作 [Gemini 网页](https://gemini.google.com/)，完成图片、参考图视频生成和下载，并为外部 Agent 提供可复用的 JSON 工作流。

本项目不调用 Gemini 生成 API。首次在可见浏览器中手动登录，之后复用专用浏览器的登录态。网页账号的订阅、功能权限、地区限制和额度仍然适用，不代表免费或无限生成。

每个生成任务都会新建独立聊天，下载并校验媒体后删除该任务聊天；本地结果和运行记录保留。工作流的每个步骤也遵循这一规则。

## 当前功能

| 功能 | 用法 |
| --- | --- |
| 登录与会话保存 | `login`，后续使用同一 profile |
| 登录状态检查 | `status`，不提交生成请求 |
| 文生图 | `image --prompt ...` |
| 图生图 | `image --reference ... --prompt ...` |
| 图生视频 | `video --reference ... --prompt ...` |
| 固定生成工作流 | JSON 保存素材、提示词、变量与顺序步骤 |
| 本地结果查询 | `result` / `workflow result` |
| 任务聊天补删 | `cleanup`，按任务记录定位 |

当前不支持批量、循环、条件分支、定时调度或视频文件作为参考输入。重复 `--reference` 表示同一任务使用多张参考图，不是批量生成。

## 快速开始

### 1. 安装

以下命令适用于 **Windows PowerShell**，均从项目根目录执行。需要 **Python 3.11 或更高版本**、可见桌面，以及能访问 Gemini 的网络和 Google 账号。已在 Windows x64、Python 3.14.5 环境验证；其他系统尚未验证。

```powershell
git clone https://github.com/CyanAutumn/gemini_generate_skills.git
cd gemini_generate_skills

python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gemini-browser/requirements.txt

$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
$cli = (Resolve-Path .\gemini-browser\scripts\gemini_browser.py).Path
& $py $cli --help
```

已有项目时跳过克隆和切换目录。后面的示例沿用 `$py` 与 `$cli`；换一个 PowerShell 窗口后，需要在项目根目录重新设置这两个变量。

依赖版本固定在 [requirements.txt](gemini-browser/requirements.txt)：CloakBrowser、Playwright、Pillow 和 PyAV。首次启动可能需要下载 CloakBrowser 的浏览器程序，请保留网络连接。

### 2. 首次登录

```powershell
& $py $cli login --timeout 900
& $py $cli status
```

`login` 打开专用浏览器窗口。在窗口内完成 Google 登录、两步验证和必要提示，等待工具确认成功并正常关闭浏览器，登录态会保存在本机。后续命令使用同一 profile 即可复用；过期后重新运行 `login`。

`status` 会打开浏览器检查状态，不生成媒体。`auth_state` 为 `authenticated` 表示已确认登录，`login_required` 表示需要登录，`loading` 表示暂未确认。

请使用本工具的独立 profile，不要直接指定日常 Chrome 的用户资料目录。同一 profile 同时只运行一个浏览器任务。profile 含敏感登录信息，不应提交到 Git 或交给其他人。

如果日常访问 Gemini 需要代理，可显式指定已有代理：

```powershell
& $py $cli status --proxy http://127.0.0.1:7897
```

请将地址和端口换成本机实际配置；后续登录、生成和清理也要沿用。也可设置环境变量 `GEMINI_BROWSER_PROXY`，命令行 `--proxy` 优先。该配置只作用于专用浏览器，不修改系统代理。

### 3. 生成并下载一张图片

```powershell
& $py $cli image --prompt "生成一张水彩插画：一只橘猫坐在窗边，柔和晨光，横向构图，无文字"
```

工具会新建聊天、提交一次请求、等待生成、原生下载并校验图片，再删除该任务聊天。输出包含任务 ID、实际文件路径和清理状态，文件默认保存在项目的 `outputs` 目录。

## 图片与视频命令

图生图示例使用项目自带的产品参考图：

```powershell
& $py $cli image --reference gemini-browser/examples/workflows/product-demo/assets/sample.jpg --prompt "将参考图中的杯子改为浅绿色，保留造型与构图，使用暖夕阳光线"
```

图生视频至少需要一张参考图片：

```powershell
& $py $cli video --reference gemini-browser/examples/workflows/product-demo/assets/sample.jpg --prompt "根据参考图生成产品展示视频：镜头缓慢推进，杯口热气轻轻上升，保持杯子外观与构图一致" --timeout 600
```

较长的提示词可保存为 UTF-8 文本文件，用 `--prompt-file` 替代 `--prompt`；两者二选一。该参数按文本读取，即使文件扩展名是 `.json`，也不会把其中字段解析成命令参数。

```powershell
# 先准备自己的 prompt.txt
& $py $cli image --prompt-file prompt.txt
```

`image` 默认等待生成 300 秒，`video` 默认 600 秒，均可通过 `--timeout` 修改。该参数不是视频时长；连接、登录、上传、下载和清理耗时另计。视频入口、可用参考图数量、成片时长和额度以账号实际页面及下载结果为准。

更多命令细节见 [使用说明](gemini-browser/references/usage.md)。

## JSON 工作流

工作流把固定图片、提示词、变量和步骤组织成一个本地包。可重复调用同一流程并替换参数，例如“固定产品参考图 → 按颜色生成新图 → 用新图生成展示视频”。模板可以固定约束，不能保证每次生成的像素完全相同。

### 使用自带示例

[product-demo](gemini-browser/examples/workflows/product-demo/workflow.json) 包含参考图、两份提示词和图生图到图生视频的两个步骤。先创建和预览：

```powershell
& $py $cli workflow create product-demo --spec gemini-browser/examples/workflows/product-demo/workflow.json
& $py $cli workflow list
& $py $cli workflow validate product-demo
& $py $cli workflow plan product-demo --set "subject=陶瓷咖啡杯" --set "color=浅绿色"
& $py $cli workflow run product-demo --set "subject=陶瓷咖啡杯" --dry-run
```

这些命令只操作本地文件，不打开浏览器，也不消耗网页生成额度。默认注册到当前工作目录下的 `workflows/product-demo`；同名流程不会被覆盖。

确认计划后，实际生成用：

```powershell
& $py $cli workflow run product-demo --set "subject=陶瓷咖啡杯" --set "color=浅绿色"
```

工作流按顺序执行，每一步下载校验通过且聊天删除确认后才进入下一步；失败立即停止，不自动重试，也不在旧聊天中继续。已完成的本地文件和运行记录会保留。

### 自定义 JSON

创建自己的目录，将规范保存为 `workflow.json`，把固定参考图放在同级的 `assets/sample.jpg`：

```json
{
  "schema_version": 1,
  "name": "my-product",
  "description": "固定产品图改色并生成展示视频",
  "variables": {
    "subject": null,
    "color": "浅绿色"
  },
  "steps": [
    {
      "id": "image",
      "type": "image",
      "prompt": "将参考图中的{{subject}}改为{{color}}，保留造型与构图，不添加文字",
      "references": ["assets/sample.jpg"]
    },
    {
      "id": "video",
      "type": "video",
      "prompt": "根据图片生成产品展示视频，保持{{subject}}的{{color}}外观，镜头缓慢推进",
      "references": [{"step": "image", "file_index": 0}]
    }
  ]
}
```

使用 `workflow create my-product --spec <规范路径>` 注册，再 `validate`、`plan` 或 `run`。

- `variables` 中的 `null` 表示运行时必填，其他标量值提供默认值；`{{变量名}}` 只做替换，不执行表达式。
- 固定资源路径相对规范文件所在目录；步骤输出引用只能指向前面图片步骤的输出，`file_index` 从 `0` 开始。
- 提示词可用内联 `prompt`，也可用 UTF-8 文件的 `prompt_file`，每步二选一。
- 步骤 `id` 不能重复或仅以大小写区分；变量替换后的提示词不能为空白，预检不通过时不会打开浏览器。
- 当前创建器会把提示词统一归档为 `prompts/<步骤ID>.txt`，在生成的 `workflow.json` 中保存 `prompt_file`，并复制固定素材。创建后的包可搬移，登录态不在包内。
- `--set` 可重复指定不同参数；也可用 `--params-file` 读取 JSON 参数对象，`--set` 覆盖文件中的同名值。

单步便捷创建、完整字段和结果查询见 [工作流说明](gemini-browser/references/workflows.md)。创建或修改流程本身不等于执行生成，实际生成由 `workflow run` 发起。

## 外部 Agent 接入

将 [gemini-browser/SKILL.md](gemini-browser/SKILL.md) 及其同目录资源作为一个完整 Skill 提供给支持本地 Skill 的 Agent；按所用 Agent 的加载方式配置。也可以让 Agent 直接阅读该文件并调用本项目 CLI。

Agent 需要访问运行本项目的电脑、Python、浏览器、素材和输出目录，并使用已登录的专用 profile。它不是远程 API 或 MCP 服务，纯云 Agent 不会自动获得本机浏览器和登录态。

可以这样提出需求：

> 创建一个“产品改色”工作流，固定使用指定的样例图，把颜色设为参数。先校验并展示计划。

> 调用 product-demo 工作流，主体为陶瓷咖啡杯，颜色为浅绿色，生成图片和视频并交付本地文件。

仅要求创建时，Agent 应交付配置和计划；要求运行时再实际生成。执行过程中需要人工登录或验证时，应提示用户在专用浏览器窗口操作。

## 结果、目录与聊天清理

| 内容 | 默认位置 / 参数 |
| --- | --- |
| 私有数据根目录 | Windows `%LOCALAPPDATA%/GeminiBrowserSkill`；`--data-dir` 可修改 |
| 浏览器 profile | `<私有数据目录>/profiles/default`；`--profile` 可切换名称 |
| 单媒体结果和记录 | `<项目根目录>/outputs/<媒体任务ID>/`；`--output-dir` 可修改 |
| 工作流注册目录 | `<执行时的当前目录>/workflows/`；`--workflows-dir` 可修改 |
| 工作流运行记录 | `outputs/workflow-runs/<工作流运行ID>/record.json` |
| 工作流步骤结果 | `outputs/workflow-runs/<工作流运行ID>/steps/<步骤ID>/<媒体任务ID>/` |

公共路径、profile 和代理参数可放在对应子命令前或后。从其他目录调用时，注意工作流注册目录随当前目录变化，默认输出目录仍是项目的 `outputs`。

```powershell
# 将示例 ID 换成工具返回的实际 ID
& $py $cli result MEDIA_RUN_ID
& $py $cli workflow result WORKFLOW_RUN_ID
& $py $cli cleanup MEDIA_RUN_ID
```

查询命令只读取本地记录并复验媒体，同时检查已记录的文件摘要；文件被替换时会报错。旧记录没有摘要时仍执行媒体校验。查询不打开网页、不重新生成；查询命令成功退出不代表原任务全部成功，还需查看原任务状态和 `chat_cleanup`。

完整成功要求本地媒体校验通过且聊天已确认删除。文件已保存但删除失败时属于部分完成；恢复网络或登录后，用 `cleanup` 补删。它只删除该任务记录指向的聊天，不清空其他历史聊天。正常失败或 Ctrl+C 也会尝试清理，外部强杀、断网、登录失效或页面变化可能阻止删除。

补删工作流内的聊天时，使用步骤记录中的**媒体任务 ID**，并精确指定该步骤的输出目录，例如：

```powershell
& $py $cli cleanup MEDIA_RUN_ID --output-dir "outputs/workflow-runs/WORKFLOW_RUN_ID/steps/image"
```

同时沿用原来的 `--profile`、`--data-dir` 和所需代理。不要把工作流运行 ID 当作媒体任务 ID。

本地记录包含提示词、引用素材和结果路径，应按内容决定是否分享。登录态、诊断截图与个人资料不应提交到仓库；`outputs/`、`.runtime/`、`.venv/` 和常见登录态目录已在 [.gitignore](.gitignore) 中排除。

## 常见问题

- **页面打不开或超时**：先确认本机能访问 Gemini，再检查显式代理配置。连接超时不代表生成尚未提交，不要盲目重发。
- **需要重新登录**：使用原 profile 运行 `login`，成功后按任务记录补删失败的聊天。
- **没有图片或视频入口、额度不足**：以账号页面提示为准，检查账号权限与额度；脚本不能提供账号没有的功能。
- **聊天删除失败**：检查网络、登录状态和账号是否允许删除。部分工作或学校账号可能受限制；无法确认删除时工具会报告清理失败。
- **页面改版导致定位失败**：可用 `status --inspect` 保存控件信息和截图，位置在私有数据目录的 `diagnostics` 下。分享前检查是否含个人信息或聊天内容。

## 开发与验证

```powershell
# 单元测试
& $py -X utf8 -m unittest discover -s tests -q

# 真实 CloakBrowser 访问本机模拟网页，不调用 Gemini 生成
& $py tests/browser_smoke.py
& $py tests/workflow_browser_smoke.py
```

本机已通过单元测试，以及独立聊天清理和两步工作流的本地浏览器测试。真实 Gemini 已验证登录态重启复用、文生图、图生图和图生视频的下载校验及聊天删除。完整两步工作流已在本机模拟网页验证，尚未把这一结果当作真实 Gemini 两步流程的验收。

图片使用 Pillow 校验；视频使用 PyAV 完整解码校验，不能把网页截图、参考图预览或损坏文件当作成功结果。真实网页的每次成功仍以本次媒体和清理记录为准。

## 项目结构

```text
gemini-browser/
  SKILL.md                     # Agent 入口
  agents/openai.yaml           # Skill 元数据
  requirements.txt            # 固定依赖
  scripts/                    # 浏览器、任务及工作流 CLI
  references/usage.md         # 登录、生成与清理说明
  references/workflows.md     # 工作流规范与操作说明
  examples/workflows/         # 自带产品示例、提示词与参考图
tests/                        # 单元测试和本地浏览器测试
```

## 参考

- [CloakBrowser 官方仓库](https://github.com/CloakHQ/CloakBrowser)
- [Gemini 图片生成、编辑与下载](https://support.google.com/gemini/answer/14286560?co=GENIE.Platform%3DDesktop&hl=en)
- [Gemini 视频生成与下载](https://support.google.com/gemini/answer/16126339?co=GENIE.Platform%3DDesktop&hl=en)
- [Gemini 聊天记录删除说明](https://support.google.com/gemini/answer/13666746?co=GENIE.Platform%3DDesktop&hl=en)
