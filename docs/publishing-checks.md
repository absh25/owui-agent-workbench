# 发布前核验记录

日期：2026-10-04。范围是本仓库源码、设计文档、流程图和部署示例；没有在这次发布核验中执行真实 Jira 写入或重新运行收费模型实测。

## 回归结果

按实际依赖分组执行，205 个唯一测试全部通过：

| 分组 | 环境 | 结果 |
| --- | --- | --- |
| OWUI 后端（排除知识服务和备用 transport 两个专用模块） | 现有 Open WebUI 容器 | 167 / 167 |
| 轻量知识库 | retrieval-lab 容器 | 15 / 15 |
| OpenTerminal transport | Windows 宿主及 Linux OWUI 临时副本 | 9 / 9；两次验证同一组，不重复计数 |
| Jira 前端补丁安装器 | Windows 宿主 | 14 / 14 |

第一次直接在 OWUI 容器执行全量 discover，发现两个依赖问题：备用 runtime 包未挂载，知识库 PDF 测试的 pypdf 只在知识容器安装。后续按实际环境分组，未为解决测试问题修改镜像或真实运行配置。

备用 transport 的主动取消测试还发现 WebSocket 清理竞态：用户取消后，接收循环可能把主动关闭误报为断连并发送给 SDK reader。发布前修复了主动关闭与真正异常断连的区分。原取消测试增加 cancel/close 两个场景，检查 reader 正常结束、远端关闭指令和重复清理幂等；异常断连及非零退出测试仍通过。

## 可复现命令

从仓库根目录执行。后端测试的目录放入 sys.path，以保留 discover 使用的裸测试模块导入方式：

```powershell
docker compose -f lab/docker-compose.yml exec -T open-webui python -c "import pathlib, sys, unittest; sys.path.insert(0, '/lab/tests'); names=sorted(p.stem for p in pathlib.Path('/lab/tests').glob('test_*.py') if p.stem not in {'test_open_terminal_transport', 'test_retrieval_lab'}); suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())"
docker compose -f lab/docker-compose.yml exec -T retrieval-lab python -m unittest -q tests.test_retrieval_lab
python -m unittest discover -s frontend -p 'test_*.py' -v
```

Transport 在宿主可使用：

```powershell
python -m unittest tests.test_open_terminal_transport
```

本次也在已有真实 SDK 的 Linux 容器内复核。将 runtime 放入临时目录，不改变持久挂载：

```powershell
docker compose -f lab/docker-compose.yml exec -T open-webui python -c "from pathlib import Path; Path('/tmp/codex-publish-unit-tests-20261004').mkdir(exist_ok=True)"
docker compose -f lab/docker-compose.yml cp ./runtime open-webui:/tmp/codex-publish-unit-tests-20261004/
docker compose -f lab/docker-compose.yml exec -T -e PYTHONPATH=/tmp/codex-publish-unit-tests-20261004:/app/backend:/lab open-webui python -m unittest tests.test_open_terminal_transport
```

知识库测试中的 HTTP 输出来自 MockTransport/ASGITransport，不代表本次真实调用百炼。

## 文档与发布范围

- 根 README 已按当前代码重新核对：OWUI 平台能力与本项目实现分开说明，模型、SDK、授权和验收分别描述。
- 五张 SVG、五张 PNG 与离线查看器保存在 `docs/diagrams/`，共 77 个节点；XML、节点、图片、内嵌数据和本地链接已检查。
- 部署示例使用占位域名与凭据，实际 `.env` 没有纳入版本控制。
- 数据库、浏览器 profile、日志、本地 outputs 和演示 ZIP 没有发布。
- 保留上游来源与 Git 历史，没有向上游仓库推送本项目改动。

回归通过只说明这些检查用例覆盖的行为。历史真实模型实测见原设计与迭代文档；本记录不把已有记录当作本次重新运行结果，也不提供大规模成功率或租户隔离认证。
