# 当前多 Agent 协作流程

核对日期：2026-10-04。以下图示描述当前代码行为，不把后续规划画成已实现能力。

当前为主助手委派式协作：主助手规划任务，检索 Agent 与编码 Agent 各自在独立上下文中执行。直接委派时主助手消费结构化结果；复合任务中，主助手制定依赖计划，执行器把实际成功的直接依赖结果加入编码上下文，无需主模型额外总结证据。专业 Agent 不直接互相调用或共享完整聊天上下文。

视觉复核是独立验收模块，不是第四个 Agent；Jira MCP 是业务工具，不是 Jira Agent；请求内执行器也不是新的自主 Agent。普通 native 工具调用保持当前 OWUI 串行路径，只有 `execute_task_plan` 内具备条件的节点真实并行。业务写入继续走直接调用及确认流程，不进入任务计划。

默认预算来自代码中的 Valves 配置，部署时可配置。图中“验收通过”与“附件交付成功”是不同状态；编码 `completed` 不能替代附件结果检查。

## 图 0：请求内任务计划与真实并发

这个流程对应新增的 `execute_task_plan`。现有 [五张 PNG/SVG](diagrams/README.md) 展开单专业执行、验收与确认，尚未重导出任务 DAG、30 秒排队和非阻塞视觉建议；当前行为以本文 Mermaid 为准。

### 复合任务的依赖图

```mermaid
flowchart LR
    U["OWUI 用户提出复合目标"] --> M["主助手一次制定任务计划<br/>tasks / id / tool / arguments / depends_on"]
    M --> P["execute_task_plan<br/>校验当前请求授权工具、参数和完整 DAG"]
    P --> A["A 检索 Agent<br/>知识查询、原文核对"]
    P --> B["B Jira 只读工具<br/>真实可见项目查询"]
    A --> G{"实际结果 sufficient？"}
    G -->|"是"| C["C 编码 Agent<br/>只等待 A，注入真实依赖结果"]
    G -->|"否"| X["C blocked<br/>没有执行编码，报告证据缺口"]
    C --> V["独立验收与有限修复<br/>真实测试、固定标准、原测试保护"]
    V --> CE["C 成功或失败的实际终态"]
    B --> W["等待计划内全部节点达到终态"]
    CE --> W
    X --> W
    W --> R["Pipe 按执行报告生成 Markdown<br/>状态、时间、峰值、引用与验收"]
    R --> U

    classDef main fill:#eaf2ff,stroke:#416fac,color:#163659;
    classDef retrieval fill:#e8f7f4,stroke:#318278,color:#174d47;
    classDef coding fill:#f0edff,stroke:#7663ab,color:#443267;
    classDef gate fill:#fff4df,stroke:#b68832,color:#6d4d16;
    classDef fail fill:#ffeded,stroke:#be6262,color:#7b3333;
    class U,M,R main;
    class A retrieval;
    class C,CE coding;
    class P,B,G,V,W gate;
    class X fail;
```

A、B 无依赖，实际运行区间可以重叠。C 只直接依赖 A，A 充分证据完成后即可开始，不等待无关 B；B 失败只影响依赖 B 的下游。最终汇总等待所有节点达到终态，不把某个分支失败当成其他独立任务必须失败的理由。

计划同时包含检索与编码时，编码必须直接依赖该检索节点；一个计划最多一次检索、一次编码。检索 `sufficient` 才算节点成功；编码须 `completed` 且 `verification.passed=true` 才成功。专业执行预算和工作区锁仍然适用，不通过拆分计划内委派绕过。

### 执行器的调度、停止与收尾

```mermaid
flowchart TD
    P0["接收本次 tasks<br/>仅使用当前已授权且经治理的 callable"] --> P1{"全计划有效？<br/>最多 6 项；真实准入工具与参数<br/>依赖存在、无环、资料直接依赖"}
    P1 -->|"否"| PI["invalid_arguments<br/>execution_started=false<br/>有限参数纠正，尚未启动任何任务"]
    P1 -->|"是"| P2["初始化状态<br/>无依赖 ready；有依赖 waiting<br/>建立请求内截止时间"]
    P2 --> P3{"还有 pending 或 running？"}
    P3 -->|"没有"| PR["生成真实报告<br/>completed 或 partial_failed"]
    P3 -->|"有"| PT{"请求取消或已到截止？"}
    PT -->|"是"| PC["cancel 正在运行的异步任务<br/>await 全部退出并清理<br/>未启动任务终止；不保留后台计划"]
    PC --> PS["超时或取消终态<br/>如实记录没有完成的工作"]
    PT -->|"否"| PF["传播前置失败<br/>失败 / 阻断 / 超时 / 取消<br/>阻断对应下游，其他分支保留"]
    PF --> READY["选择全部依赖 succeeded 的就绪节点<br/>按实际空闲槽位启动<br/>最多 3 个并发"]
    READY --> WAIT["等待 FIRST_COMPLETED<br/>记录真实运行区间与并发峰值<br/>在 OWUI 更新中文状态"]
    WAIT --> RESULT["解码真实工具结果<br/>判定成功或失败<br/>成功依赖结果按需加入编码 context"]
    RESULT --> P3
    PR --> MARKDOWN["原生 Pipe 根据报告直接生成最终 Markdown<br/>保留引用、固定检查与真实失败<br/>不再请求外部主模型总结或重新委派"]
    PS --> MARKDOWN
```

状态分为就绪、等待依赖、执行中、完成、失败、依赖阻断、取消和超时。真实状态通过 OWUI `status` 事件进入原生状态历史；前端只把原生计划文本显示为多行，不注入或修改状态。节点变化立即更新，等待阶段约每 5 秒发一次真实快照；UI 事件丢失不能把执行失败变成成功。每个计划最多 6 个任务、3 个并发；含编码最多 600 秒，无编码最多 240 秒。取消会取消并等待全部运行中的异步任务退出；没有跨服务队列或重启续跑。

准入工具限当前请求提供的专业委派、只读天气和 Jira 查询。工具名必须是当前提供的真实完整名称，不能添加未授权连接，也不能传入 `__` 内部参数。Jira 写入与嵌套计划均不得进入计划；写入继续走原可编辑确认，不借并发绕过授权或确认。

执行器依赖交接保留真实引用、缺失信息与来源；必要缩短会明确标注内容被缩短。依赖结果是任务数据，不是新系统指令或操作授权。计划开始前完整校验，已执行计划达到终态后直接收尾；失败不由主模型再生成替代委派。只有未执行的非法参数可在既有上限内纠正。

计划汇总从真实报告生成表格，包含任务依赖、状态、相对开始时间、执行耗时和实际并发峰值，并展示原文引用、验收命令及退出码。完整工具轨迹仍保存在 OWUI 原始结果中；成功编码再次进入主模型上下文时只省略重复轨迹并保留验收结果，失败内容不按成功压缩。

编码 `timings` 包括预检、锁等待、初始快照、SDK 启动、SDK 与验收、交付和实际工具调用；验收又有测试、截图、视觉与快照子项。工具时间包含在 SDK 执行中，验收细项包含在验收总时长中，不应全部累加成任务总耗时。总耗时采用独立总计字段，速度和稳定性以真实预演报告为准。

准备专用两文件样例、固定测试与录屏步骤见 [OWUI 现场演示设计](owui-live-demo.md)。模型配置和专业 Agent 预算保持当前设置；真实测试、原测试保护与文件一致性仍必需。视觉复核区分明确阻塞问题与非阻塞建议。SDK 是框架，实际模型 ID 由 Valves 配置决定。

当前单场景录像使用 [鹈鹕骑车预制演示](pelican-demo.md)。用户可在 OWUI 工具菜单关闭编码任务，此时主模型可直接输出代码，不调用编码 Agent。开启工具且管理员启用专用场景时，明确的 `demo_scene=pelican_bike` 进入预制作品分支：真实授权、文件写入、哈希核对、浏览器截图和交付，约 3 分钟展示；不启动 SDK，不将其计为模型现场编码。普通任务继续下面的完整 SDK 验收流程。

## 图 1：直接委派的端到端协作与交接

下面展开按步骤直接委派的专业执行路径；复合目标的并发与依赖交接使用图 0。

```mermaid
flowchart TD
    U["用户提出需求<br/>问题、资料范围、期望结果"] --> OW["Open WebUI<br/>认证用户、聊天上下文、当前可用工具"]
    OW --> ACL["复用用户与连接授权<br/>过滤未授权工具"]
    ACL --> M["主助手 / Orchestrator<br/>理解目标、整理上下文、规划下一步"]
    M --> ROUTE{"当前步骤需要什么能力？"}

    subgraph MAIN["主助手直接处理"]
        QA["普通问答、代码片段解释<br/>必要时调用当前可用的搜索或记忆工具"]
    end
    ROUTE -->|普通问答| QA
    ROUTE -->|多个独立任务与依赖| PLAN["execute_task_plan<br/>请求内任务 DAG，详见图 0"]
    PLAN --> PLANFINAL["按实际报告直接汇总<br/>终态后不重新委派"]
    PLANFINAL --> FINAL

    subgraph RETRIEVAL["专业执行者 1：检索 Agent"]
        RT["delegate_retrieval_task<br/>完整问题、必要上下文、证据验收条件"]
        RL["独立模型与只读工具循环<br/>查文档、搜索、读取原文、核对证据<br/>详见图 2"]
        RR["返回结构化检索结果<br/>status、summary、evidence<br/>missing_information、stop_reason、attempts"]
        RS{"证据是否充分？"}
        GAP["证据不足或执行失败<br/>说明缺口 / 实际原因<br/>本轮停用再次检索与编码委派"]
        NEXT{"用户是否要求依据资料实现代码？"}
        HANDOFF["主助手准备编码上下文<br/>结论、实际引用、适用条件、验收要求"]
        ANSWER["主助手依据实际原文回答<br/>保留来源与适用条件"]
    end
    ROUTE -->|依赖知识库依据| RT
    RT --> RL --> RR --> RS
    RS -->|sufficient| NEXT
    RS -->|insufficient_evidence / failed| GAP
    NEXT -->|否| ANSWER
    NEXT -->|是| HANDOFF

    subgraph CODING["专业执行者 2：编码 Agent + 验收交付模块"]
        CT["delegate_coding_task<br/>任务、工作目录、必要上下文、验收条件<br/>测试命令、预览入口、read_only"]
        CL["Claude Agent SDK 执行工具循环<br/>实际编辑、运行、调试<br/>独立验收失败后反馈同会话修复<br/>详见图 3"]
        CR["返回编码与交付报告<br/>status、changed_files、tool_evidence<br/>verification、artifacts、delivery_errors"]
    end
    ROUTE -->|直接编码 / 仓库分析 / HTML 或 SVG 绘图| CT
    HANDOFF --> CT
    CT --> CL --> CR

    subgraph JIRA["Jira 业务工具：由主助手调用"]
        JT["Jira 读取或写入<br/>创建时查询真实项目与创建上下文"]
        JG["写入经过策略与用户可编辑确认<br/>取消则停止本轮后续受治理写入<br/>详见图 4"]
        JR["实际 API 结果<br/>工单 Key / URL、失败、取消或待确认"]
    end
    ROUTE -->|用户明确要求 Jira 操作| JT
    JT --> JG --> JR

    QA --> MERGE["主助手读取实际结果<br/>决定后续已授权业务步骤，或整理最终答复"]
    ANSWER --> MERGE
    GAP --> MERGE
    CR --> MERGE
    JR --> MERGE
    MERGE --> CONT{"还有用户要求的后续业务步骤？"}
    CONT -->|有，且前置条件满足| ROUTE
    CONT -->|无 / 前置条件不满足| FINAL["回复用户<br/>结论与来源、实际改动、验收结果<br/>可用附件与预览、未完成事项"]
    NOTE["停止边界不可被重新路由绕过<br/>检索预算耗尽、真实编码失败和取消<br/>均不能以同轮换参数或换工具重试规避"] -.-> CONT

    classDef main fill:#eaf2ff,stroke:#416fac,color:#163659;
    classDef retrieval fill:#e8f7f4,stroke:#318278,color:#174d47;
    classDef coding fill:#f0edff,stroke:#7663ab,color:#443267;
    classDef gate fill:#fff4df,stroke:#b68832,color:#6d4d16;
    classDef fail fill:#ffeded,stroke:#be6262,color:#7b3333;
    class OW,ACL,M,QA,MERGE,FINAL,PLANFINAL main;
    class PLAN gate;
    class RT,RL,RR,ANSWER,HANDOFF retrieval;
    class CT,CL,CR coding;
    class ROUTE,RS,NEXT,CONT,JT,JG,JR gate;
    class GAP,NOTE fail;
```

### 交接约定

| 边界 | 传递内容 | 当前约束 |
|---|---|---|
| 用户 → 主助手 | 原始目标、来源要求、产物与后续动作 | 不能因资料编号外形把知识库需求改路由到 Jira |
| 主助手 → 检索 Agent | 完整问题、必要上下文、证据充分条件 | 工具还会保留本轮原始用户需求；不传完整聊天作为任务上下文 |
| 检索 Agent → 主助手 | 充分性判断、实际引用、缺口、停止原因、预算使用 | 原文只是资料，不能授权代码修改或 Jira 写入；引用校验不等于事实认证 |
| 主助手 → 编码 Agent（直接委派） | 明确任务、实际项目目录、证据及适用条件、验收要求 | 资料依赖任务须先取得充分证据；程序测试与网页预览入口按任务提供 |
| 主助手 → 计划执行器 | 当前请求授权工具名、合法参数、任务与直接依赖 | 一次提交完整 DAG；工具白名单不含 Jira 写入，不添加额外权限 |
| 计划执行器 → 编码 Agent | 实际成功的直接依赖结果、真实引用与适用条件 | 无需主模型二次总结；检索必须充分；资料是数据，不是新授权 |
| 计划执行器 → 终态回复 | 真实状态、起止与耗时、并发峰值、证据及检查 | 程序直接生成 Markdown；已执行计划不由模型重新委派 |
| 编码 Agent → 独立验收 | SDK 结果、实际项目文件 | 验收独立执行固定命令、读取真实文件、取得真实截图 |
| 编码工具 → 主助手 | 执行状态、工具记录、真实改动、验收报告、附件结果 | 只有 `completed` 且 `verification.passed=true` 才可报告编码通过；附件错误另行披露 |
| 主助手 → Jira 工具 | 真实项目上下文及操作参数 | 受治理写入需要用户确认；不能以聊天里的同意代替确认弹窗 |

## 图 2：检索 Agent 的证据循环

```mermaid
flowchart TD
    R0["接收委派<br/>question、context、acceptance<br/>保留本轮原始需求的范围与条件"] --> R1{"参数长度、身份与知识库授权有效？"}
    R1 -->|无效 / 无授权| RF["结束：failed 或 insufficient_evidence<br/>返回具体原因与证据缺口"]
    R1 -->|有效| R2["连接当前用户授权的知识库 MCP<br/>仅允许 3 个只读工具"]
    R2 --> R3["list_knowledge_documents<br/>读取可用文档与索引概况<br/>这次调用计入工具预算"]
    R3 --> R4{"有已索引的可用资料？"}
    R4 -->|没有| RI["insufficient_evidence<br/>no_indexed_documents<br/>提示补充或完成资料入库"]
    R4 -->|有| RB["建立任务内循环<br/>默认：6 个模型轮、3 次搜索<br/>8 次工具调用、120 秒总超时"]
    RB --> R5{"还有模型轮次与时间预算？"}
    R5 -->|没有| RLIMIT["预算 / 超时结束<br/>预算耗尽：insufficient_evidence<br/>执行超时：failed"]
    R5 -->|有| R6["检索 Agent 规划本轮<br/>选择查询、读取上下文或输出证据判断"]
    R6 --> R7{"模型是否请求工具？"}

    R7 -->|是| R8{"工具在授权名单内<br/>且对应预算尚未耗尽？"}
    R8 -->|未授权 / 服务不可用| RF
    R8 -->|预算耗尽| RLIMIT
    R8 -->|是| R9["顺序执行本批工具调用<br/>list_knowledge_documents<br/>search_knowledge / read_knowledge_chunks"]
    R9 --> R10["记录真实分块、来源、原文与调用轨迹<br/>搜索计数 / 工具计数增加<br/>缓存命中仍消耗工具调用预算"]
    R10 --> R5

    R7 -->|否，输出判断| R11{"输出是否完整<br/>且 JSON 结构有效？"}
    R11 -->|被截断| RI2["insufficient_evidence<br/>incomplete_assessment<br/>要求缩小问题范围"]
    R11 -->|有效结构| R12["程序核对引用<br/>chunk_id 来自本任务实际返回分块<br/>quote 与原文匹配<br/>sufficient 须有引用且无缺口"]
    R11 -->|格式错误| R13{"还有判断纠正次数<br/>且仍有模型轮次？"}
    R12 --> R14{"引用与充分性声明有效？"}
    R14 -->|无效| R13
    R13 -->|有：最多纠正 2 次| R15["把格式 / 引用校验错误和已取得原文<br/>反馈给同一个检索任务<br/>纠正轮次仍计入 6 轮总预算"]
    R15 --> R5
    R13 -->|没有| RI3["insufficient_evidence<br/>invalid_assessment / invalid_evidence<br/>不能把候选片段当作充分依据"]
    R14 -->|有效| R16{"最终判断是否 sufficient？"}
    R16 -->|是| RP["sufficient<br/>实际引用、原文摘录、结论<br/>missing_information 为空"]
    R16 -->|否| RI4["insufficient_evidence<br/>列出尚缺的资料、参数或适用条件"]

    RF --> RR["交回主助手<br/>status、summary、evidence<br/>missing_information、stop_reason、attempts"]
    RI --> RR
    RI2 --> RR
    RI3 --> RR
    RI4 --> RR
    RLIMIT --> RR
    RP --> RR
    RR --> RG{"主助手读取 status"}
    RG -->|sufficient| ROK["引用回答<br/>或按用户要求将证据交给编码 Agent"]
    RG -->|insufficient_evidence / failed| RSTOP["说明缺口或失败原因<br/>本轮移除再次检索与编码委派<br/>禁止绕过预算重新委派"]

    classDef action fill:#e8f7f4,stroke:#318278,color:#174d47;
    classDef gate fill:#fff4df,stroke:#b68832,color:#6d4d16;
    classDef success fill:#e7f6ed,stroke:#479869,color:#235438;
    classDef fail fill:#ffeded,stroke:#be6262,color:#7b3333;
    class R0,R2,R3,RB,R6,R9,R10,R12,R15,RR action;
    class R1,R4,R5,R7,R8,R11,R13,R14,R16,RG gate;
    class RP,ROK success;
    class RF,RI,RI2,RI3,RI4,RLIMIT,RSTOP fail;
```

检索最多进行 3 次 `search_knowledge` 搜索，不等于只能读取 3 个分块。`read_knowledge_chunks` 等调用独立计入 8 次工具预算。模型格式 / 引用纠正不会额外增加 6 轮上限。120 秒包括本次任务的连接与执行；超时或模型 / 连接故障按执行失败处理。

检索的引用校验约束“引用必须来自实际取得的原文”，仍不能证明原文自身正确，也不能保证模型的全部推理正确。

## 图 3：编码 Agent、独立验收与交付

```mermaid
flowchart TD
    C0["主助手委派编码<br/>task、repo、context、acceptance<br/>verification_commands、preview_target、read_only"] --> C1{"委派参数格式合法？"}
    C1 -->|否| CA["invalid_arguments<br/>stage=validation<br/>execution_started=false、retryable=true<br/>返回 correction"]
    CA --> CQ{"本轮还能纠正参数？"}
    CQ -->|能：最多纠正 2 次| CC["主助手按 correction 修改参数<br/>保留 delegate_coding_task<br/>尚未启动 SDK，不消耗修复预算"]
    CC --> C1
    CQ -->|不能| CF["未完成：停止本轮编码委派<br/>不换工具或重新委派绕过限制"]

    C1 -->|是| C2["校验 OWUI 用户身份<br/>复用用户的 OpenTerminal 连接授权<br/>从真实工具描述取得工作区根目录"]
    C2 --> C3{"repo 与预览路径在工作区内？"}
    C3 -->|参数路径不合法| CA
    C3 -->|合法| C4["获取进程内 asyncio 工作区锁<br/>按 terminal_id 锁定<br/>默认最多排队 30 秒"]
    C4 --> CREADY{"规定等待时间内获得锁？"}
    CREADY -->|否| CBUSY["workspace_busy<br/>execution_started=false<br/>SDK / 验收未启动，不取消持锁任务"]
    CBUSY --> REPORT
    CREADY -->|是| C5["在实际终端检查 realpath<br/>项目目录存在且不越出工作区<br/>拒绝符号链接逃逸"]
    C5 -->|目录不存在 / 执行失败| CF
    C5 -->|通过| C6["初始文件快照与 SHA256<br/>构造验收器、固定本次验收命令<br/>提供的命令或从项目自动发现"]
    C6 -->|命令 / 预览配置不合法| CA
    C6 -->|通过| C7["建立 Claude Agent SDK 会话<br/>只接入授权的 OpenTerminal MCP 工具<br/>不启用宿主内置文件与命令工具"]
    C7 --> C8["编码 Agent 模型与工具循环<br/>读取 / 搜索文件、编辑实现<br/>运行命令、轮询后台进程、调试<br/>保留实际工具证据"]
    C8 --> C9{"SDK 是否正常返回 success？"}
    C9 -->|异常 / 超时 / 轮次错误 / 无完成结果| CFAIL["status=failed<br/>说明已有改动与实际问题<br/>不进入验收修复循环"]
    C9 -->|是| V0["进入独立验收<br/>SDK 正常结束不能直接标为完成"]

    subgraph VERIFICATION["独立验收模块：由程序控制，视觉复核不是独立 Agent"]
        V1["读取当前真实文件快照<br/>计算实际 changed_files<br/>检查 read_only 与原测试保护要求<br/>收集 Agent 声明的 open_issues"]
        V2["执行固定测试 / 构建命令<br/>取得真实进程退出码与输出<br/>零测试不能算通过<br/>禁止吞错、管道或固定输出替代验收"]
        V3{"任务是否需要网页 / SVG 验收？"}
        V4["Playwright 浏览器真实渲染<br/>桌面 1280 × 800：2 帧<br/>手机 390 × 844：1 帧<br/>检查加载错误、空白和横向溢出"]
        V5["视觉模型复核真实截图<br/>用户明确要求 / 可用性缺陷进入 issues<br/>未要求的审美或可选改进进入 suggestions<br/>建议不阻塞通过"]
        V6["核对文件一致性<br/>本地入口 source_sha256 与快照一致<br/>验收前后文件未发生变化"]
    end
    V0 --> V1 --> V2 --> V3
    V3 -->|需要| V4 --> V5 --> V6
    V3 -->|不需要| V6
    V6 --> VP{"全部必要检查通过？"}
    VP -->|否| VR{"还有验收修复次数<br/>且整体执行预算未耗尽？"}
    VR -->|有：默认最多修复 2 次| VF["记录本轮验收历史<br/>真实命令输出、问题及截图<br/>反馈同一个 SDK 会话继续修复<br/>固定命令不可替换"]
    VF --> C8
    VR -->|修复耗尽| VFAIL["status=verification_failed<br/>verification.passed=false<br/>停止本轮，不重新委派绕过上限"]
    VR -->|整体超时| CFAIL

    VP -->|是| CP["status=completed<br/>verification.passed=true<br/>记录真实改动、检查及各轮历史"]
    CP --> D0["保存实际验收截图为用户附件<br/>尝试交付 HTML / SVG 原件"]
    D0 --> D1["原件哈希与验收 source_sha256 核对<br/>检查文件有效性与附件所有权<br/>SVG 原件保留，预览使用响应式 img 包装"]
    D1 --> D2{"原件与预览交付有错误？"}
    D2 -->|没有| DOK["返回 artifacts 与截图链接<br/>聊天附加可见预览<br/>主助手汇报实际结果"]
    D2 -->|有| DERR["记录 delivery_errors<br/>编码报告可能仍是 completed<br/>说明附件交付未完成<br/>不会自动回到 SDK 修复循环"]

    CFAIL --> REPORT["交回主助手<br/>报告真实完成状态、验收证据<br/>文件 / 附件位置及未解决问题"]
    VFAIL --> REPORT
    CF --> REPORT
    DOK --> REPORT
    DERR --> REPORT

    classDef action fill:#f0edff,stroke:#7663ab,color:#443267;
    classDef gate fill:#fff4df,stroke:#b68832,color:#6d4d16;
    classDef success fill:#e7f6ed,stroke:#479869,color:#235438;
    classDef fail fill:#ffeded,stroke:#be6262,color:#7b3333;
    class C0,C2,C4,C5,C6,C7,C8,CC,V0,V1,V2,V4,V5,V6,VF,D0,D1,REPORT action;
    class C1,CA,CQ,C3,CREADY,C9,V3,VP,VR,D2 gate;
    class CP,DOK success;
    class CF,CBUSY,CFAIL,VFAIL,DERR fail;
```

### 编码流程的具体边界

- 参数纠错与验收修复分别计数。参数最多纠正 2 次；默认验收修复最多 2 次，对应首次验收加两次修复后的验收，共最多 3 轮。
- SDK 默认 `MAX_TURNS=20`，SDK 执行＋独立验收＋修复共用默认 600 秒预算，不含授权、预检与锁前排队。排队独立默认最多 30 秒（`WORKSPACE_WAIT_SECONDS`）；到期为 `workspace_busy` 且没有启动 SDK/验收，不取消持锁任务。请求内计划的整体截止同时适用，先到的停止条件生效。修复不重置整次执行预算。
- 进程内锁按 `terminal_id` 共享，不是分布式锁，也没有多机并发调度器。它主要防止当前进程中的同一终端编码任务互相改写文件。
- 锁与 `realpath` 检查确认任务工作目录边界；它们不是对任意 shell 命令的完整操作系统级隔离。命令运行仍依赖 OpenTerminal 的执行环境与用户授权。
- 验收器是顺序执行的：文件检查 → 固定命令 → 浏览器 / 视觉检查 → 文件一致性检查。图中没有并行投票或 Agent 互相讨论。
- 代码修改需要可执行验收；前端项目可能同时需要测试与截图。纯 HTML/SVG 绘图用 `verification_commands=[]`，由文件、浏览器与视觉检查验收，不用 `test`、`ls`、`echo` 替代。
- 纯只读分析与文档修改按任务要求检查，不强制运行代码测试。`read_only=true` 仍会核对文件是否实际被修改。
- 本地 HTML/SVG 入口核对源文件 SHA256。HTTP 入口检查运行页面，不能用这个机制宣称已完整证明运行服务与全部代码来源一致。
- SVG 以图片形式渲染与预览，不作为活动脚本文档运行。视觉 `issues` 限明确要求和可用性缺陷；未要求的美化放入非阻塞 `suggestions`。视觉检查不能保证主观美感，也不能替代实际点击、输入等交互测试。
- 自动原件附件当前主要面向 HTML/SVG；其他程序文件不因此自动全部打包为附件。附件生成错误记录为 `delivery_errors`，与编码验收结果分开。
- 当前失败会由主助手据实际报告停止；失败后的文件改动不会自动回滚。

## 图 4：Jira 真实上下文、可编辑确认与提交

```mermaid
flowchart TD
    J0["用户明确要求 Jira 业务操作<br/>由主助手调用 Jira MCP<br/>Jira 本身不是独立 Agent"] --> J1{"读取还是写入？"}
    J1 -->|读取| JREAD["执行当前授权的 Jira 查询工具<br/>返回真实工单或查询失败"]
    J1 -->|写入| J2{"是否创建新工单？"}

    J2 -->|是| J3["get_jira_create_context<br/>未指定项目则先列真实可见项目<br/>取得目标项目创建权限与可用类型"]
    J3 --> J4{"项目明确、实际可用<br/>且当前身份有创建权限？"}
    J4 -->|多个项目且未选择| JSELECT["请用户选择真实项目<br/>不能猜测示例项目 Key"]
    JSELECT --> J3
    J4 -->|不存在 / 无权限| JDENY["停止写入<br/>说明权限或真实上下文缺口"]
    J4 -->|是| J5["准备创建草稿<br/>真实 project_key、issue_type<br/>拟定 summary、description 等字段"]
    J2 -->|修改 / 评论 / 状态流转| J5B["准备对应写入参数<br/>真实 issue_key、字段或目标状态"]
    J5 --> J6["工具执行边界<br/>用户 / 聊天上下文与策略校验<br/>持久化请求、预览与审计记录"]
    J5B --> J6

    J6 --> J7{"写入策略允许进入确认？"}
    J7 -->|DENY，例如删除工单| JDENY
    J7 -->|需要用户确认| J8["显示可编辑确认弹窗<br/>展示完整拟提交参数<br/>暂停提交，等待当前用户响应"]
    J8 --> J9{"确认响应"}
    J9 -->|取消| JCANCEL["CANCELLED<br/>不提交该请求<br/>停止本轮后续受治理写入"]
    J9 -->|未收到有效响应 / 响应不匹配| JPENDING["WAITING_FOR_APPROVAL<br/>execution_performed=false<br/>不把错误、字符串或字典当作同意"]
    J9 -->|确认原草稿 / 编辑后确认| J10["核对 request_id 与 tool_name<br/>校验编辑后参数及原用户策略<br/>保存实际确认的最终参数"]
    J10 --> J11{"确认仍有效，且请求未重复执行？"}
    J11 -->|无效 / 被策略拒绝| JDENY
    J11 -->|请求已处理| JEXIST["返回已有结果或待核实状态<br/>不再次提交"]
    J11 -->|是| J12["执行 Jira API<br/>创建工具再次检查实际项目权限与类型<br/>提交最终确认参数"]
    J12 --> J13{"API 返回是否明确？"}
    J13 -->|成功| JSUCCESS["保存实际 API 结果与审计记录<br/>用户编辑字段时返回 submitted_arguments<br/>以最终提交值汇报 Key / URL 等结果"]
    J13 -->|失败| JFAIL["FAILED<br/>说明真实错误，不声称提交成功"]
    J13 -->|调用中断 / 结果未知| JUNKNOWN["UNKNOWN 或待核实<br/>先检查实际 Jira 结果<br/>避免重复创建或重复修改"]

    JREAD --> JREPORT["主助手向用户汇报<br/>真实结果 / 最终字段 / 失败 / 取消 / 待确认"]
    JDENY --> JREPORT
    JCANCEL --> JREPORT
    JPENDING --> JREPORT
    JEXIST --> JREPORT
    JSUCCESS --> JREPORT
    JFAIL --> JREPORT
    JUNKNOWN --> JREPORT

    classDef action fill:#eaf2ff,stroke:#416fac,color:#163659;
    classDef gate fill:#fff4df,stroke:#b68832,color:#6d4d16;
    classDef success fill:#e7f6ed,stroke:#479869,color:#235438;
    classDef fail fill:#ffeded,stroke:#be6262,color:#7b3333;
    class J0,J3,J5,J5B,J6,J8,J10,J12,JREAD,JREPORT action;
    class J1,J2,J4,J7,J9,J11,J13,JSELECT,JPENDING,JEXIST gate;
    class JSUCCESS success;
    class JDENY,JCANCEL,JFAIL,JUNKNOWN fail;
```

Jira 写入确认是当前受治理写入的专门边界，不代表项目全部工具调用都需要强审批。读取与其他允许的工具有不同策略。确认事件匹配、参数持久化、重复执行检查与用户取消约束由工具中间件处理；创建权限和项目类型由 Jira 客户端再次核验。

用户编辑确认字段后，主助手应依据工具返回的 `submitted_arguments` 汇报最终内容；未编辑时按实际确认草稿与 API 结果汇报。确认完成不等于 Jira API 已提交成功。旧请求的结果未核实时不能盲目重复，新一轮用户请求则需要新的确认。

## 当前实现映射

| 图中职责 | 源文件 |
|---|---|
| 主助手提示、工具过滤、请求内编排入口、当轮停止边界、结果预览 | [`supervisor/native_pipe.py`](../supervisor/native_pipe.py) |
| 任务参数校验、请求内并发、真实依赖交接、取消清理 | [`supervisor/task_plan.py`](../supervisor/task_plan.py) |
| 计划终态 Markdown、参数纠正计数、成功上下文压缩 | [`supervisor/plan_results.py`](../supervisor/plan_results.py) |
| 专用演示样例与固定测试准备 | [`supervisor/prepare_demo_live.py`](../supervisor/prepare_demo_live.py) |
| 检索连接授权、检索循环、预算、引用校验 | [`supervisor/retrieval_tool.py`](../supervisor/retrieval_tool.py) |
| 编码参数、终端授权、工作区锁、SDK 会话、结果状态 | [`supervisor/coding_tool.py`](../supervisor/coding_tool.py) |
| 固定命令、文件快照、截图与视觉复核、同会话修复 | [`supervisor/verification.py`](../supervisor/verification.py) |
| 浏览器渲染、桌面与手机截图、SVG 图片包装 | [`verification_browser/server.py`](../verification_browser/server.py) |
| HTML/SVG 原件、附件、哈希和预览 | [`supervisor/artifacts.py`](../supervisor/artifacts.py) |
| 用户可编辑确认、参数约束与事件校验 | [`governance/confirmation.py`](../governance/confirmation.py) |
| 写入策略、确认结果、重复请求与审计 | [`governance/tool_middleware.py`](../governance/tool_middleware.py) |
| Jira 创建上下文、权限、类型与实际 API | [`lab/jira-mcp/jira_client.py`](../lab/jira-mcp/jira_client.py) |
| 当前 Jira 写入规则与删除拒绝规则 | [`policies/jira-workflow.yaml`](../policies/jira-workflow.yaml) |

## 维护与导出

本文维护六个 Mermaid 代码块：图 0 的复合任务 DAG 与请求内调度，以及图 1–4 的直接委派、检索、编码验收和 Jira。现有五张导出 PNG/SVG 是专业执行展开图，尚未导出新增调度图；更新时明确区分，不把旧导出图标为已经包含 DAG。节点 ID 按 `P`、`R`、`C/V/D`、`J` 区分职责，便于核对引用和流程边。

更改预算、工具授权、状态语义、确认行为或附件交付时，先对照对应实现更新图中的判断与停止分支，再更新录屏材料。不要只改数字，而保留已经不成立的路径。

可用支持 Mermaid 的 Markdown 预览查看图示，或将每个 `mermaid` 代码块单独保存为 `.mmd` 后通过 Mermaid CLI 导出 SVG。例如在已经安装 Mermaid CLI 的环境中运行：

```text
mmdc -i flow.mmd -o flow.svg -b transparent
```

此命令只是可选导出方式；文档本身不执行任务、不调用模型、不创建 Jira 工单。
