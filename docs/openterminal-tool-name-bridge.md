# OpenTerminal 工具桥接修复

## 原因

Qwen 有时返回 `run_command`，但 Claude Agent SDK 的实际工具名是
`mcp__openwebui__run_command`。CLI 在工具执行前拒绝该名称，因此
OpenTerminal 服务健康并不意味着该次命令已执行。

## 执行链路

模型响应 → 请求级名称兼容桥 → Claude Agent SDK → OWUI MCP 工具 →
OWUI 治理和权限检查 → OpenTerminal → 工具结果 → 模型回复。

兼容桥监听临时的本机回环端口，使用随机鉴权令牌，只转发 Messages API。
上游密钥不交给该端口的调用方，不输出请求正文或凭据。
仅将本轮已授权且模型请求中实际提供的工具短名称映射为完整注册名称。
未知名称、工具参数、回复正文、工具结果均保持不变。无工具的 SDK
辅助请求不会做名称映射。接口关闭、取消和异常时回收端口和连接。

保留现有参数生成进度：长 `write_file` 输入显示已接收字符数和“尚未执行”，
使用单独的参数生成预算，不继续沿用工具结果后短回复预算。
失败调用不得标记成已成功执行；连续无效名称仍受停止保护。

从 OWUI 已授权终端工具描述读取实际沙箱工作目录，明确与 Claude CLI 的
OWUI 后端 cwd 区分，不硬编码其他用户的沙箱路径。
OWUI 返回的 `(payload, headers)` 先提取 payload，再判断错误；传输头不进入
模型证据，目录不存在等错误不再作为成功工具结果返回。

终端命令的非零 `exit_code` 同样作为 Agent 工具错误返回；后台进程尚未
返回退出码时保持待完成状态。该判断同时用于 SDK 和 Messages API 适配器。
`run_command` 的工作目录兼容处理覆盖 `/app/backend` 本身及其子目录，
仅对 OWUI 标记为 `terminal` 的工具生效；其他工具的同名参数保持原样。

## 验证

- 单元测试覆盖授权名称、未知名称、参数不变、端口鉴权、SSE 分片和无工具请求。
- 使用真实模型、真实 OWUI 工具和 OpenTerminal 创建唯一 HTML 文件，并执行命令
  验证文件内容及 CSS `@keyframes`，不是仅验证模型声称成功。
- 不绕过 OWUI、不开放 Claude 本地 Bash、不修改 Jira 账号或审批规则。

2026-10-02 验证结果：宿主机与真实 OWUI / Claude Agent SDK 容器中均通过
40 项单元测试。集成脚本改用 OWUI 自己的加载器读取数据库中保存的
`claude_code` 函数，并以配置的 OpenTerminal 所属用户获取授权工具。
`--sandbox-checks` 通过真实 OWUI callable 验证写文件、读文件、后端 cwd
修正、非零退出码、目录 404 和后台进程轮询；实测工作目录为
`/workspace/wenguang.wu`。完整模型链路还验证了连续写文件并执行命令读取。

可在实验环境中复现：

```powershell
docker compose -f lab/docker-compose.yml exec -T open-webui python /lab/smoke_test_terminal_agent.py --sandbox-checks
docker compose -f lab/docker-compose.yml exec -T open-webui python /lab/smoke_test_terminal_agent.py
```

这些测试验证执行与文件内容，未验证浏览器中的动画视觉效果。
网络、模型故障及超出安全预算的请求仍可能超时；不能保证所有任务均成功。
