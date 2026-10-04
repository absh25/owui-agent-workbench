# 当前编码与绘图流程

按 2026-10-04 当前实现核对。主助手负责委派，编码 Agent 负责实际执行，独立验收负责判断是否通过。

```mermaid
flowchart TD
    U["用户提出编码或绘图需求"] --> M["主助手整理任务、目录和验收要求"]
    M --> P{"委派参数是否合法？"}
    P -->|否| A["返回参数错误和纠正提示<br/>编码尚未启动"]
    A -->|最多纠正 2 次| P
    A -->|纠错次数耗尽| F["报告未完成及实际原因<br/>停止本轮，不绕过编码工具"]
    P -->|是| S["确认工作区、记录文件快照<br/>固定本次验收命令"]
    S --> C["Claude Agent SDK 驱动编码 Agent<br/>读取文件、写代码、运行命令和调试"]
    C -->|异常、超时或未正常结束| F
    C -->|正常结束| V["独立验收<br/>程序：执行测试或构建<br/>网页 / SVG：浏览器截图及视觉复核<br/>检查文件是否一致"]
    V --> Q{"验收通过？"}
    Q -->|否| R{"还有修复次数？<br/>默认最多修复 2 次"}
    R -->|有| B["把真实错误和截图反馈<br/>给同一个 SDK 会话"]
    B --> C
    R -->|没有| F
    Q -->|是| D["生成已验收报告及截图<br/>HTML / SVG 核对文件哈希并保存附件"]
    D --> E{"附件交付有错误？"}
    E -->|有| W["说明附件尚未交付完成<br/>返回实际错误和已有结果"]
    E -->|无| O["回复用户<br/>实际改动、检查结果<br/>HTML / SVG 附件及聊天预览"]

    classDef action fill:#e8f1ff,stroke:#4778b8,color:#16345c;
    classDef check fill:#fff3d6,stroke:#b98a28,color:#694900;
    classDef success fill:#e7f6ed,stroke:#479869,color:#235438;
    classDef failure fill:#ffe9e9,stroke:#c36c6c,color:#7c3030;
    class M,S,C,V,B,D action;
    class P,A,Q,R,E,W check;
    class O success;
    class F failure;
```

参数纠错与编码后的修复分别计数：参数最多纠正 2 次；验收失败默认最多修复 2 次，即最多 3 轮验收。SDK 执行异常不进入验收修复循环。

前端项目可能同时需要测试和截图。纯 HTML/SVG 绘图使用浏览器和视觉验收，不用 test、ls、echo 替代验收。纯分析与文档任务按实际要求检查，不强制运行代码测试。

自动附件主要针对 HTML/SVG。验收通过与附件交付是两个步骤：附件失败记录 delivery_errors 并说明交付未完成，当前不会自动回到修复循环，编码报告状态也可能仍为 completed。
