# 固定工作流

工作流把提示词、固定参考图、变量及顺序步骤保存成一个本地包。外部 Agent 可以将用户的自然语言要求整理为规范，通过同一个 CLI 创建、检查和执行。它需要访问运行此 Skill 的电脑、脚本和文件；这不是将本机登录态暴露给纯云 Agent 的远程服务。

用户说“创建这个工作流”时，整理素材、模板和参数，完成 `create`、`validate` 与 `plan` 后交付；这不会自动消耗网页生成额度。用户说“运行某个工作流”或“创建后生成一次”时，再执行 `run`。修改现有流程可以编辑包内的提示词和清单；新增固定素材先放进包内，再校验相对路径和计划。不要把创建、修改流程当作已经获准生成媒体，也不要在用户已要求运行时重复索取确认。

先查看当前版本的帮助：

```powershell
python gemini-browser/scripts/gemini_browser.py workflow --help
python gemini-browser/scripts/gemini_browser.py workflow create --help
python gemini-browser/scripts/gemini_browser.py workflow run --help
```

## 规范与资源

当前 `schema_version` 为 `1`。只支持顺序的 `image` 和 `video` 步骤；不支持循环、条件、批量或调度。提示词与固定参考图相对规范文件所在目录组织，创建时会复制成可搬移的包，登录态不属于包内容。

项目附带 `gemini-browser/examples/workflows/product-demo/`，结构如下：

```text
product-demo/
  workflow.json
  prompts/image.txt
  prompts/video.txt
  assets/sample.jpg
```

完整规范：

```json
{
  "schema_version": 1,
  "name": "product-demo",
  "description": "根据参考图生成产品图片，再用该图片生成展示视频",
  "variables": {
    "subject": null,
    "color": "浅绿色",
    "motion": "镜头缓慢推进，杯口热气轻轻上升"
  },
  "steps": [
    {
      "id": "image",
      "type": "image",
      "prompt_file": "prompts/image.txt",
      "references": ["assets/sample.jpg"]
    },
    {
      "id": "video",
      "type": "video",
      "prompt_file": "prompts/video.txt",
      "references": [{"step": "image", "file_index": 0}]
    }
  ]
}
```

`prompts/image.txt`：

```text
根据参考图生成一张产品展示图片：主体是{{subject}}，将主体颜色调整为{{color}}，保留参考图中的产品造型与主要构图。使用干净的背景、柔和自然光和清晰细节，不添加文字、水印或无关物体。
```

`prompts/video.txt`：

```text
根据上传的产品图片生成一段产品展示视频：主体是{{subject}}，保持{{color}}的外观、造型与整体构图一致。{{motion}}。动作自然平稳，保持主体细节，不添加文字、水印或无关物体。
```

`sample.jpg` 是项目自有生成测试图的原样副本。可以替换为用户指定的固定素材，再创建另一个名称的工作流。字符串参考指向包内图片；对象参考 `{"step":"image","file_index":0}` 指向前面 `image` 步骤的第一个输出文件，索引从 `0` 开始。不能引用后续步骤或视频输出；`video` 至少需要一个图片参考。

变量必须预先声明。值为 `null` 表示运行时必填；字符串、数字或布尔值提供默认值，变量名允许中文。提示词用 `{{变量名}}` 替换，不执行表达式、脚本或 shell 命令；不要添加规范未支持的字段。

## 从规范创建并运行

以下命令从项目根目录运行，完整使用附带的包；`subject` 是唯一必填参数：

```powershell
python gemini-browser/scripts/gemini_browser.py workflow create product-demo --spec gemini-browser/examples/workflows/product-demo/workflow.json
python gemini-browser/scripts/gemini_browser.py workflow list
python gemini-browser/scripts/gemini_browser.py workflow show product-demo
python gemini-browser/scripts/gemini_browser.py workflow validate product-demo
python gemini-browser/scripts/gemini_browser.py workflow plan product-demo --set "subject=陶瓷咖啡杯"
python gemini-browser/scripts/gemini_browser.py workflow run product-demo --set "subject=陶瓷咖啡杯" --dry-run
python gemini-browser/scripts/gemini_browser.py workflow run product-demo --set "subject=陶瓷咖啡杯"
```

默认注册目录是执行命令时的当前工作目录下 `workflows`，可用 `--workflows-dir` 修改。`create NAME --spec FILE` 以命令中的 `NAME` 注册，即使规范中写了另一个名字；不会覆盖同名流程，需要另取名称。创建会复制提示词及固定素材，后续运行不依赖原素材位置。

`create`、`list`、`show`、`validate`、`plan` 和 `run --dry-run` 均不打开浏览器。计划用于检查渲染后的提示词、参数和引用关系；前面步骤的图片在真正运行后才产生，计划不是生成结果。

也可将参数保存为 UTF-8 JSON 文件，例如 `product-params.json`：

```json
{
  "subject": "陶瓷咖啡杯",
  "color": "浅绿色",
  "motion": "镜头缓慢推进，杯口热气轻轻上升"
}
```

```powershell
python gemini-browser/scripts/gemini_browser.py workflow plan product-demo --params-file product-params.json
python gemini-browser/scripts/gemini_browser.py workflow run product-demo --params-file product-params.json --set "color=天蓝色"
```

`--set` 可重复，但同一个键不能重复指定；同时提供参数文件时，`--set` 覆盖文件中的同名值。缺少必填变量或参数不合法时先修正规范或参数，不开启浏览器猜测执行。

## 单步便捷创建

单步流程可用同一个 `create` 的 `--type` 模式，不另设子命令。`--spec` 与 `--type` 二选一；`--reference`、`--var`、`--required` 均可重复，提示词用 `--prompt` 或 `--prompt-file` 二选一：

```powershell
python gemini-browser/scripts/gemini_browser.py workflow create cup-image --type image --reference gemini-browser/examples/workflows/product-demo/assets/sample.jpg --prompt "将参考图中的{{subject}}调整为{{color}}，保持造型与构图" --var "color=浅绿色" --required subject --description "固定产品改色图片流程"
python gemini-browser/scripts/gemini_browser.py workflow validate cup-image
python gemini-browser/scripts/gemini_browser.py workflow plan cup-image --set "subject=咖啡杯"
python gemini-browser/scripts/gemini_browser.py workflow run cup-image --set "subject=咖啡杯"
```

`--type video` 创建图生视频单步流程，必须提供至少一张 `--reference`。需要前一步输出、多个步骤或更完整的资源布局时，使用 `--spec`。

## 执行结果与精确补删

`--profile`、`--data-dir`、`--output-dir`、`--proxy` 和 `--workflows-dir` 可放在相应命令层级前或后。工作流按顺序逐步执行；每一步都新建自己的聊天，下载与校验成功且聊天删除确认后，才执行下一步。任何步骤失败即停止，不自动重试、不在旧聊天中续做，保留本地记录供检查。

```powershell
python gemini-browser/scripts/gemini_browser.py workflow result WORKFLOW_RUN_ID
```

将 `WORKFLOW_RUN_ID` 替换为工作流返回的运行 ID。默认输出位于：

```text
outputs/workflow-runs/<工作流运行ID>/record.json
outputs/workflow-runs/<工作流运行ID>/steps/<步骤ID>/<媒体任务ID>/record.json
```

`workflow result` 只读取本地运行情况，不打开网页、不重新执行。若某个媒体任务的 `chat_cleanup` 失败，使用该步骤记录中的媒体任务 ID，并把输出目录精确指向该步骤目录补删：

```powershell
python gemini-browser/scripts/gemini_browser.py cleanup MEDIA_RUN_ID --output-dir "outputs/workflow-runs/WORKFLOW_RUN_ID/steps/image"
```

同时沿用原 `--profile`、`--data-dir` 及所需代理。不要把工作流运行 ID 当作媒体任务 ID；补删仅针对记录中的任务聊天，不恢复生成或下载。断网、登录失效、外部强杀或页面变化可能使删除暂时失败，按记录明确报告，不能宣称无条件删除。

模板可固定流程、素材和约束，不能保证每次生成像素相同。下层文生图、图生图及图生视频已有的实测见 [使用说明](usage.md)；完整工作流的成功仍须以本次各步骤与清理记录为准。

## 已完成的验证

2026-11-01：配置、命令及运行器已通过离线检查；真实 CloakBrowser 在仅访问本机模拟网页的测试中完成了两步图生图到图生视频流程。视频上传内容的 SHA-256 与前一步图片输出一致，两个步骤分别新建并删除聊天，无关聊天保留，本地文件可复验。另一次独立 Agent 使用验证完成了带中文必填参数的单步工作流创建、两种参数预览，以及复制工作流包后的重新校验和预览。这些框架验证没有额外调用 Gemini 生成；实际网页的套餐、额度和页面状态仍由运行时检测。
