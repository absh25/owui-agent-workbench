# DEMO-PARAM-001：现场演示参数规范

本文是为 OWUI Agent Workbench 现场演示人工编写的虚构资料，仅用于演示知识检索、证据交接和程序验证。它不是行业标准，没有真实传感器实验数据，不保证真实性能。

## window_size 窗口参数

window_size 必须为 Python 整数，且明确排除 bool；合法范围为 64 到 4096，包含两端；必须是 2 的幂。默认值为 256。

validate_window_size(value) 对合法值返回原整数。类型不符、bool、越界或不是 2 的幂时，统一抛出 ValueError，不能自动截断、四舍五入或替换为默认值。

合法示例：64、256、4096。类型非法示例：True、False、256.0、"256"、None。数值非法示例：0、63、100、4097、8192。

## threshold 阈值参数

threshold 默认值为 0.65。可接受 int 或 float，但必须排除 bool；转为浮点数后必须为有限值，且满足 0 到 1，包含两端。不允许 NaN、正负无穷、字符串或 None。合法值返回浮点数，非法值统一抛出 ValueError。

这些参数仅是演示约定。本文没有通用检测阈值公式、检测准确率、真实误报率或跨设备适用性证据。不能根据 threshold=0.65 推导误报率，也不能把该数值称为工程标准或性能保证。

## 演示验收与文件边界

固定测试覆盖 window_size 的 3 个合法输入、5 个类型非法输入、5 个数值非法输入，共 13 个子用例，组织为 3 个 unittest 测试方法。

编码 Agent 仅修改 demo_window_validator.py，实现 validate_window_size；保留 test_demo_window_validator.py 的字节及断言不变。验收命令为 python -m unittest -v test_demo_window_validator。程序测试成功还须由平台独立验收确认，不能仅依据 Agent 的完成描述。
