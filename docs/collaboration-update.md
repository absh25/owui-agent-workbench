# 协作助手与 Jira 确认表单

OWUI 只保留一个协作聊天入口 `owui-main`，显示名称“协作助手”。同名 Pipe 与 Model 直接绑定，Model 的 `base_model_id` 为 `None`，默认工具包含编码 Agent、检索 Agent、Jira 和天气。规划仍由 OWUI 原生工具循环负责，编码由 Claude Agent SDK 管理，知识检索由检索 Agent 管理。

主助手与编码 Agent 已从 `qwen3.7-flash` 切换到 `qwen3.7-plus`；检索 Agent 仍单独配置为 `qwen3.7-flash`。模型配置同时保存在本地环境和 OWUI 的组件 Valves 中。更改前已备份数据库，Claude Agent SDK 仍是执行框架，实际推理使用百炼模型。

新模型调试实测通过：主助手只委派一次编码任务；SDK 先运行原有两项测试并得到退出码 1，再修改 `calculator.py`，随后重跑得到退出码 0。验证脚本独立复跑两项测试通过，确认测试文件 SHA256 未变化。示例保留在 OpenTerminal 的 `/workspace/wenguang.wu/debug-smoke-ab35e18ab1e0`，可用 `supervisor/smoke_coding_debug.py` 创建新示例复测。

HTML 动画交付现在要求写入实际项目目录；写文件和替换文件工具会核对该目录。编码工具重新读取完成的 HTML，保存为当前用户的 OWUI 附件；主助手最终回复附加下载链接和可渲染的 HTML 代码块。预览检查文件所有者，使用文件库里的真实内容，并忽略旧轮次、失败任务和其他工具返回的附件。读取不完整或交付失败会明确返回错误。`tests.test_visual_delivery` 覆盖目录、完整读取、失败任务、旧轮次和用户隔离。

编码交付增加独立验收：真实命令、文件快照、网页桌面/手机运行及截图复核均通过后才标记完成。失败会把检查结果与图片返回同一 SDK 会话修复，默认最多两次；达到上限返回未完成，主助手不能同一轮重新委派绕过。真实测试与网页自测均验证第一次失败、修改后第二次通过。详细配置及证据见 [coding-verification.md](coding-verification.md)。

运行 `python /lab/supervisor/register.py` 会先备份 OWUI 数据库，验证新入口和两个委派工具，再删除已知的 `claude_code`、`owui_supervisor` 旧 Pipe 及对应模型覆盖。旧 Supervisor 聊天只迁移模型元数据，聊天文本、旧 Claude 聊天及业务工具均保留。重复注册不会重新创建旧入口。

Jira 创建、修改、状态变更和评论在执行前展示可编辑表单。浏览器返回当前请求 ID 与最终字段；后端校验字段、当前请求和用户身份，原子保存修改后的参数后执行一次。取消、无效回复、断开连接、重复确认都不会绕过确认。执行失败不会自动重复提交。最终工具结果包含 `submitted_arguments`，主助手据此汇报用户修改后的实际内容。原生布尔确认仍兼容原有客户端。

工单类型下拉选项复用本轮已授权的 `get_jira_create_context` 只读结果，无须为弹窗额外查询。用户修改项目后，真正创建时仍重新检查目标项目权限和类型。Jira 配置正确不代表任意项目都存在或有创建权限。

模型列表中的 OWUI 默认 Arena 实验入口同时通过环境开关和持久设置 `evaluation.arena.enable=false` 关闭，刷新后选择器只显示协作助手。旧数据库的持久设置会覆盖环境默认值，因此注册脚本也设置这一项。

本轮性能改动减少两类开销：主助手复用 HTTP 客户端连接，检索 Agent 复用单任务内的文档概览。缓存仍计入工具预算，`attempts.mcp_calls` 记录实际 RPC 尝试数；不会跨用户共享检索结果，也不会共享模型会话消息。没有承诺固定倍速，远程推理、embedding 和编码测试仍会耗时。

主助手关闭 OWUI 原生知识库检索和额外子 Agent 工具，知识问题直接委派检索 Agent；委派需保留原问题和验收要求，避免降低证据标准。原生笔记、记忆等普通业务工具仍可用。

检索工具还接收 OWUI 服务端注入的本轮用户原始消息，作为检索范围和证据要求的依据，避免只依赖主助手转述。它不会传入其他轮次的用户消息、系统提示或工具结果，也不能据此授权编码或 Jira 写入。直接调用时没有聊天消息，仍支持显式 question/context/acceptance。

弹窗资产由 `frontend/patch_frontend.py` 安装进镜像，仅扩展带 `jira_form` 标记的确认事件，其余 OWUI 确认保持原流程。当前适配固定的 OWUI `v0.11.4` 编译前端，升级时若确认分支不匹配，镜像构建会失败，需重新适配。部署命令：

```powershell
cd lab
docker compose build open-webui
docker compose exec -T open-webui python /lab/supervisor/register.py
docker compose up -d --no-deps open-webui
```

前端补丁为聊天模块和引用它的入口生成新的文件名，并更新入口及预加载引用。原有模块继续保留，Svelte 原版本信息保持与运行时代码一致。部署后普通刷新已有页面即可加载新表单，弹窗内直接点击标题、描述等输入框修改，再决定取消或确认提交。

`supervisor/smoke_jira_confirmation.py` 只测试真实确认事件和取消路径，不批准真实 Jira 写入；字段修改后的提交由模拟写入测试验证。`qa/jira-live-ui.mjs` 使用独立浏览器配置检查真实 OWUI 页面，修改标题与描述后取消，并清理自己的测试聊天。

本轮验证：138 项后端测试通过；前端交付修复后，14 项补丁测试通过，包括入口引用、缓存版本、重复安装和兼容性检查。独立浏览器完成 7 组演示交互检查和桌面、窄屏、深色截图；真实 OWUI 页面验证标题与描述可编辑。真实 OWUI 检索到编码通路通过，OpenTerminal 独立运行两个测试并校验演示参数。真实 Jira 弹窗测试始终取消，没有批准或创建真实工单。
