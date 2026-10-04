# 来源说明

OWUI Agent Workbench 在 tfriedel/openwebui-claude-code 基础上扩展。

上游仓库：https://github.com/tfriedel/openwebui-claude-code

当前项目起点提交：`5bbc1fc`。仓库保留上游 Git 历史。基础 Pipe、sandbox 和 anthropic-proxy 等路径来自上游，`claude_agent_pipe.py` 在本项目中也有修改；其他扩展的归属可通过提交历史核对。

本项目新增或扩展主助手统筹、独立检索任务、编码委派与验收、浏览器与视觉复核、附件交付、Jira 工具治理与可编辑确认，以及相关测试和设计文档。

上游 README 声明 MIT，但当前基础快照没有独立 LICENSE 文件。这里保留原有声明，不虚构上游版权人或重新许可其代码。旧 Pipe 说明保存于 `docs/legacy-pipe.md`。

Open WebUI、Claude Agent SDK、OpenTerminal、Playwright 等组件遵循各自项目的许可和使用条款。Docker 构建引用依赖镜像或安装软件包，不表示这些软件成为本项目原创代码。
