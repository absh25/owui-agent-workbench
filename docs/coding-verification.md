# 编码交付的自动验收

编码 Agent 正常结束只是进入验收阶段。只有 SDK 正常结束并且独立验收的 `passed` 为 `true`，编码工具才返回 `status=completed`。失败结果不会产生正式 HTML 交付预览；主助手明确标记未完成，并禁止在同一用户轮次重新委派以绕过修复上限。

委派参数包括 `verification_commands`（测试、构建或检查命令）、`preview_target`（项目内 HTML/SVG 或 OpenTerminal 本地 HTTP 服务）和 `read_only`（仅分析、不修改）。命令在编码任务开始时固定。已有项目可自动发现 package.json 的 test/build/typecheck/lint 或 Python unittest；新程序应明确提供实际测试命令。命令不允许管道、吞掉失败、重定向或固定输出代替验收。后台进程须取得真实退出码，空测试集不能作为测试通过。声明保留原有测试时，快照还会检查原测试未被修改或删除。

程序独立记录项目文件的 SHA256，检查实际发生的修改，不依赖 Agent 声称的 changed_files。纯只读分析、文档修改可不运行代码测试；代码修改须有可执行检查，纯 HTML/SVG 可使用浏览器验收。验收期间文件变化会使验收失效。快照最多处理 5000 个文件；大型仓库应选具体项目目录。

`verification-browser` 使用固定 Playwright 1.63.0，在独立容器中以 UID 1000 运行。它不发布宿主端口，工作区以只读方式挂载，接口需现有终端密钥。网页在 1280×800 和 390×844 两种尺寸运行，记录页面/控制台/资源加载错误、空白与横向溢出，并取得两张桌面帧和一张手机截图。预览只接受当前授权项目内的 HTML/SVG，以及 OpenTerminal 应用服务器；应用应绑定 0.0.0.0。文件预览的源内容 SHA256 必须与本次验收快照一致。

真实截图送入视觉模型，按任务和验收要求检查布局、主体姿态、裁切及动作连接。默认使用编码模型，也可通过编码工具 Valves 的 `VISUAL_REVIEW_MODEL` 指定支持图片输入的模型。服务不可用、截图损坏、模型不支持图片或返回格式无效都视为未完成验收。视觉判断不能保证主观美感，也不能代替交互功能测试；应用交互仍应提供相关测试命令。

验收失败后，真实命令输出、问题和截图返回同一个 SDK 会话继续修复，再独立验收。默认最多修复 2 次，保留每轮验收历史；每轮 SDK 的 MAX_TURNS 以及任务整体 TIMEOUT_SECONDS 仍有效。SDK 轮数错误不会被修复查询绕过。成功后保存实际验收截图为当前用户附件，回复提供截图链接。

2026-10-03 实测：原鹈鹕动画能正常打开，但视觉复核拒绝了角色悬空、脚不踩踏板、翅膀不接触车把和手机适配问题。两个受控自测均在第一次验收失败后由同一个真实 SDK 会话修改，在第二次通过：

- `/workspace/wenguang.wu/verification-smoke-code-b20e08b92d55`：原两项 unittest 退出码 1；修改 calculator.py 后独立重跑退出码 0，原测试文件未变。
- `/workspace/wenguang.wu/verification-smoke-web-85e2f71197bc`：红色卡片与错误标题被真实截图复核拒绝；收到截图后改成蓝色卡片和指定标题，桌面/手机复核通过，保存三张真实截图及 HTML 附件。

验证命令：`python -m unittest tests.test_coding_verification tests.test_supervisor tests.test_visual_delivery tests.test_supervisor_registration`。真实闭环脚本为 `python /lab/supervisor/smoke_coding_verification.py --live`，每次创建独立自测目录并调用实际配置模型。安装时只更新当前编码工具的源代码/参数规格及主助手提示词，保留模型、用户设置和聊天，并先备份数据库。

委派参数错误与执行失败分开处理。命令或路径格式不合法时返回 `invalid_arguments`、`execution_started=false` 和明确纠正提示，主助手可修正最多两次；这不会启动 SDK，也不会耗掉编码修复次数。纯 HTML/SVG 使用相对 `preview_target` 及 `verification_commands=[]`，文件检查和截图验收由程序完成。真正执行失败或验收耗尽时直接回复未完成，不再让模型发起替代工具；模型返回当前未授权工具名时，整个工具批次不会转交执行。

独立 SVG 在浏览器里作为响应式图片验收，不假设 SVG 文档有 HTML body。交付保存原 SVG（image/svg+xml）及原字节校验；预览使用带 base64 图片的沙盒 HTML 包装。附件内容必须与真实验收的源 SHA256 一致，不能只根据模型声称的文件名交付。

2026-10-04 回归验证：上述四组 unittest 共 48 项通过。`python /lab/supervisor/smoke_cat_delivery.py --live` 通过真实用户身份调用主助手，主助手仅委派 `delegate_coding_task`，编码 Agent 在 `/workspace/wenguang.wu/cat-delivery-smoke-d2aff6af5e04` 生成 `cat_doodle.svg`。桌面和手机截图、视觉复核、原 SVG 附件下载及聊天中的图片预览均验证通过，第一次验收即通过。工作区留存原件 `outputs/cat_doodle-verified.svg` 和实际浏览器截图 `outputs/cat-preview-verified.png`；自测聊天已清理，原用户聊天未修改。
