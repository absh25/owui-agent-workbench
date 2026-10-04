# 详细架构流程图

这里保存当前实现的五张流程图，共 77 个可查看说明的节点：

| 图 | 内容 | 静态图片 |
| --- | --- | --- |
| 01 | 主助手统筹、检索与编码 Agent 委派和交接 | [SVG](01-multiagent-overview.svg) · [PNG](01-multiagent-overview.png) |
| 02 | 检索授权、查询循环、证据核对与停止条件 | [SVG](02-retrieval-agent.svg) · [PNG](02-retrieval-agent.png) |
| 03 | 编码参数校验、授权、执行循环与失败出口 | [SVG](03-coding-agent.svg) · [PNG](03-coding-agent.png) |
| 04 | 独立验收、同会话修复和附件交付 | [SVG](04-verification-delivery.svg) · [PNG](04-verification-delivery.png) |
| 05 | Jira 预检、可编辑确认、取消和实际提交 | [SVG](05-jira-confirmation.svg) · [PNG](05-jira-confirmation.png) |

## 离线查看

GitHub 的源码页面不会执行 HTML 查看器。下载或克隆本仓库后，用浏览器打开本目录的 [index.html](index.html)，即可切换五张图、缩放、全屏，点击节点查看说明。查看器不依赖网络服务。

`charts.json` 与查看器内嵌的图表数据一致，英文文件名对应本目录的 SVG/PNG。图中的执行角色、正常路径、重试和停止出口依据当前项目实现绘制；Jira 工具和独立验收模块没有计作额外 Agent。

流程的可维护文本版本见 [Mermaid 源码](../multiagent-flow-detailed.md)。
