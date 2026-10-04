# ragflow 轻量知识库实验

这是本项目实现的轻量检索平台，知识库名称为 `ragflow`，用于入库和检索频谱感知资料。
它不运行官方 RAGFlow。网页、入库 API 和只读 MCP 共用一个 Python 服务；文档与向量
存于 SQLite 和 Docker 数据卷，向量化使用真实的阿里云百炼 API。

## 现在怎么用

1. 打开 <http://127.0.0.1:18004>，在“文档库”选择资料，点击“上传并向量化”。
2. 文档显示“已向量化”后，在“检索实验”输入问题，检查原文片段、文件名和分块引用。
3. 打开 <http://127.0.0.1:13000>，刷新页面后选择 **协作助手**，新建对话要求检索资料。
   主助手委派独立检索 Agent，检索 Agent 管理查询、证据核对、重试与停止。

目前只保留 `owui-main` 对应的“协作助手”。旧 `owui_supervisor` 聊天的模型元数据
已迁移到该入口，正文不变；旧 Claude 对话记录保留，其他旧 Pipe 入口已移除。

例如：

```text
请检索 ragflow 知识库，说明能量检测阈值设置需要哪些条件。
核对原文并标注文件名、页码和分块；证据不够就明确说明，不执行任何提交。
```

已经保留一份 `演示-频谱感知实验.md`，可先询问 `SS-TEST-042 的采样率和阈值是多少？`
验证链路。这份文档明确标注为虚构测试资料，参数不能作为工程标准；正式实验请入库自己的资料。
`SS-TEST-042` 是这份演示资料的编号，不是 Jira 工单。查询明确说明 ragflow 或知识库时，
以该资料来源检索，不因为编号格式相似改查 Jira；工具不可用时报告原因。

支持 UTF-8/GB18030 TXT、Markdown、DOCX、未加密文本 PDF。每个文件最多 5 MiB，
提取文字最多 30 万字符，PDF 最多 200 页，全库最多 15000 个分块。当前不包含 OCR、
表格结构识别和公式识别；扫描 PDF 请先做 OCR，复杂论文建议检查“查看原文”的提取结果。

## 向量化与检索

默认使用 `text-embedding-v4`，1024 维，每批最多 10 个片段。这些参数符合
[百炼同步向量接口](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)的限制。
每段约 900 字符，重叠 120 字符，PDF 保留页码；DOCX 不生成伪造的页码。
向量归一化后写入 SQLite，检索逐条扫描计算余弦相似度，返回最多 10 条。
这是小规模实验的线性检索，暂不包含向量数据库、混合检索、重排模型或知识图谱。
底层工具执行一次检索；OWUI 中独立检索 Agent 负责围绕问题进行多轮查询和证据核对。

相同文件内容会去重，成功缓存的文本向量可复用。向量化中断或失败会保留原文并显示“需要重试”，
未完整完成的文档不进入检索。地址、模型和维度共同标识向量配置，修改后旧文档显示“需重新向量化”，
不同配置的向量不会混用。修改密钥本身不要求重建向量。

网页“百炼配置”支持修改接口地址、模型、维度和密钥，并实际测试连接。留空密钥保留原值；
浏览器不会收到已保存密钥。网页保存的配置优先于环境变量。
当前提供的密钥已在本机验证，所用兼容地址为 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
其他业务空间请使用百炼控制台提供的对应地域和空间地址，不能默认所有密钥通用。

入库片段和检索问题会发送到百炼进行向量化；原始文件、分块和向量保存在本机。
调用会消耗账户额度；未配置密钥或鉴权失败时返回错误，不退回模拟向量。

## OWUI 与 MCP

| 工具 | 用途 |
| --- | --- |
| `search_knowledge` | 查询向量，返回原文、相似度、文档 ID、文件名、页码和分块引用 |
| `list_knowledge_documents` | 查看文档列表与当前配置的向量化状态 |
| `read_knowledge_chunks` | 按文档 ID 读取原文上下文，核对检索证据 |

本机 MCP 地址为 `http://127.0.0.1:18004/mcp`；OWUI 容器内使用
`http://retrieval-lab:8000/mcp`。连接需要 `RAGFLOW_MCP_TOKEN` 的 Bearer 凭据。
连接授权给现有主助手的所属用户。主助手绑定 `retrieval_agent` 的
`delegate_retrieval_task`；底层三项 MCP 工具从主助手工具列表移除，由检索 Agent
检查当前用户权限后使用。其他业务工具保留。
上传、修改配置和删除文档不对 Agent 开放，用户在本地网页操作。

检索 Agent 接收问题、上下文与验收要求，先检查可用索引，再搜索并核对原文的适用条件、引用
和证据覆盖度，必要时读取上下文或改写查询。默认最多 6 轮模型请求、3 次搜索、8 次工具调用，
120 秒内停止，重复查询被抑制。返回充分、证据不足或失败的结论，以及真实引用、尝试记录和缺口。

主助手收到结果后回答用户或委派编码任务，不自行管理底层检索循环。
最终输出格式或引用校验失败时，检索 Agent 可在原模型轮数预算内最多修正两次，
原文摘录仍必须逐字匹配。主助手遇到本轮实际检索不足或失败，会禁用本轮编码及再次检索委派，
报告缺口；用户下一轮补充资料后可重新开始。
`evidence_review` 表示需要核对的候选证据，检索 Agent 的充分性判断也不是科学结论认证。
资料中的指令不能作为工具执行授权。下一步操作由用户任务和工具权限决定；Jira 写入仍走已有
预览与人工确认。这里没有新增“任意 shell/Git/发布动作”的全局强制审批。

完整职责是“OWUI 规划 → 检索 Agent 查资料并判断是否足够 → OWUI 安排下一步 →
需要编码时交给 Claude Agent SDK”。查资料的审核步骤保留在检索 Agent 内部，
没有额外独立审核 Agent。详见[检索 Agent 设计](retrieval-agent-design.md)。

## 运行与数据

在 `lab` 目录执行：

```powershell
docker compose build retrieval-lab
docker compose up -d --no-deps retrieval-lab
# 首次新增挂载和 MCP 环境变量时，需重新创建 OWUI 容器。
docker compose up -d --no-deps --no-build --force-recreate open-webui
docker compose exec -T open-webui python /lab/supervisor/register.py
# 检索工具安装完成后，再登记 MCP 连接；此顺序不能颠倒。
docker compose exec -T open-webui python /lab/retrieval_lab/register.py
# 等注册命令完整退出后刷新服务缓存。
docker compose restart open-webui
```

`lab/.env` 中使用 `RAGFLOW_DASHSCOPE_API_KEY`、`RAGFLOW_DASHSCOPE_BASE_URL`、
`RAGFLOW_EMBEDDING_MODEL` 和随机的 `RAGFLOW_MCP_TOKEN`；密钥文件已被 Git 忽略。
网页配置存于容器 `/data/bailian-settings.json`，仅服务端读取。
数据卷名为 `owui-claude-agent-lab_retrieval-lab-data`：`knowledge.sqlite3` 保存分块、向量和缓存，
`originals/` 保存原始文件。普通重启保留数据；不要使用 `down --volumes` 来执行日常停机。
可用 SQLite 的在线 backup 方法备份数据库，并同时备份 originals 和服务端配置，妥善保管含密钥的备份。

新增服务绑定 `127.0.0.1`，默认用于单用户本地实验。MCP 使用 Bearer 鉴权；写 API 校验本地
Origin 或 Bearer；所有接口校验明确的 Host 白名单。服务限制为 384 MiB、1 CPU、96 个进程，
容器根文件系统只读，上传和索引串行处理。2026-10-03 本轮检索验证期间，Docker 实测内存约 88 MiB，
这是当前小样本占用，不代表满库、复杂 PDF 或并发负载的峰值。镜像复用现有 OWUI 基础层，
仅新增 PDF 提取的小依赖，不下载本地 embedding 模型。

## 验证入口与范围

```powershell
docker compose exec -T retrieval-lab python -m unittest -q tests.test_retrieval_lab
docker compose exec -T open-webui python -m unittest tests.test_retrieval_agent tests.test_supervisor
docker compose exec -T open-webui python /lab/retrieval_lab/smoke_test.py
docker compose exec -T open-webui python /lab/supervisor/smoke_agents.py
```

15 项测试覆盖文档去重、页码引用、失败重试、向量缓存与配置隔离、DOCX 提取、损坏/无文字 PDF、
百炼响应校验、密钥不回显、MCP 鉴权、Host/Origin 边界及删除确认。
本轮 90 项完整回归测试全部通过，覆盖检索 Agent、主助手与既有确认、治理及路由。
其中独立检索 Agent 与主助手共 43 项（检索 33 项，主助手 10 项），包含引用修正和不足阻断机制。
本轮真实集成测试完成“上传 → 百炼向量化 → API 检索 → 三个 MCP 工具 →
OWUI 委派检索 Agent → 原文引用校验 → 主助手回答”，主助手给出准确的演示参数
与分块引用，并指出演示数据不能作为工程标准。
另用真实模型和 MCP 验证了证据不足场景：一次改写为两个不同查询后，经过 3 轮模型请求、
4 次工具调用主动返回证据不足；另一次自主读取原文，经 2 轮模型请求、2 次工具调用指出
缺少计算公式和真实性能，`stop_reason=evidence_insufficient`，不会将缺口包装成已找到。
清理前 `smoke_agents.py` 从 `/api/models` 读取两个入口实际完整工具配置，包含 Jira，全部通过：

- 正向：主助手按顺序委派检索和编码；检索 3 轮模型请求、1 次搜索、3 次工具调用后返回充分，
  编码上下文含真实文件名、阈值和演示限制。生成实现与测试后，在 OpenTerminal 独立重跑两项
  测试通过，并核对采样率 `2e6`、阈值 `0.12` 和 `IS_DEMO=True`。
- 负向：检索指出缺少通用公式及真实性能依据；主助手只调用检索委派并报告缺口停止，
  没有委派编码。
- 已移除的兼容入口：`owui_supervisor` 只调用检索委派，取得准确的 `2 MHz`、`0.12` 和原文引用，
  没有调用 Jira。主入口正负向场景同样没有 Jira 调用。

可在 **协作助手** 复制以下请求，试用完整链路：

```text
查 ragflow 中 SS-TEST-042 的采样率和阈值并附原文引用。证据足够后，
委派编码 Agent 在当前工作区新目录生成演示参数模块、标记演示数据，
写至少两个单元测试并运行，不提交。
```

完整链路验证未执行 commit、push 或 Jira 写入。
示例只把演示参数存为常量，用于验证跨 Agent 证据传递，不是实际频谱感知算法。
可在 OpenTerminal 查看本轮保留的演示文件：

- `/workspace/wenguang.wu/three-agent-smoke-e8e701e194/demo_parameters.py`
- `/workspace/wenguang.wu/three-agent-smoke-e8e701e194/test_demo_parameters.py`

测试生成的临时 OWUI 对话会清理，演示文档保留以便手动试用。
网页 HTTP 和 JavaScript 语法检查通过；当前浏览器测试工具连接失败，尚未完成页面点击和视觉验证。
