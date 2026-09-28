# 代理重试实施记录

日期：2026-09-28；执行者：Codex。

## 实施清单

- [x] SQLite 共享平台窗口、代理库存、失效及提取租约
- [x] 请求上下文、代理与总时限控制
- [x] 解析重试与快手会话兼容
- [x] 自动测试与真实链路核验
- [x] 配置及中文文档

## 已核对的现有实现

- `src/api/parse.py`：解析、积分、请求日志；部分 Cookie 错误属于无媒体兜底。
- `src/parsers/base_parser.py`：所有解析器共用 Requests Session 创建入口。
- `src/parsers/kuaishou_parser.py`：移动端、桌面端、备用 URL、GraphQL 的独立请求。
- `src/parsers/xiaohongshu_parser.py`：构造时请求，登录重定向及内容删除分类。
- `utils/web_fetcher.py`：短链请求；`src/db.py`：SQLite WAL 和短事务。
- 巨量官方接口说明：https://www.juliangip.com/help/api/unlimited/

完整依赖安装输出：`.codex/dependency-install.log`。

## 实施结果

- 新增 `src/utils/proxy_manager.py`：SQLite 窗口/库存/失效/提取租约、每秒提取限流、主动备用补充、共享退避、进程接管。
- 新增 `src/utils/parser_transport.py`：请求上下文、固定代理、总预算、不可恢复的当前 IP 失败标记、会话释放。
- `src/api/parse.py`：从原分享链接完整重试，最多三个 IP，明确错误分类；积分、平台限流、日志只计一次外部请求。
- 快手改用解析器会话，独立调用间清理 Cookie，同次重定向保留 Cookie；原路由、请求头、GraphQL 和字段提取保留。
- 新提取链接已写本地 `.env`（Git 忽略），Compose 透传，直接启动时需导出环境变量。配置说明见 `docs/proxy-retry.md`。
- 修正现有 API 文档认证测试的外网访问：只模拟解析执行入口，保留认证与路由断言。

## 验证命令与完整输出

1. `python3 -m venv .venv` 和 `.venv/bin/pip install -r requirements.txt pytest`：成功，完整输出 `.codex/dependency-install.log`。
2. 首轮现有 API/快手/小红书/短链回归：87 passed、85 subtests passed，输出 `.codex/initial-tests.log`。
3. `.venv/bin/python -m pytest -q tests/test_proxy_retry.py tests/test_parser_transport.py tests/test_kuaishou_parser.py`：39 passed、22 subtests passed，完整输出 `.codex/proxy-transport-tests.log`。
4. `.venv/bin/python -m pytest -q`：最终 422 passed、431 subtests passed，25 条现有依赖弃用警告，耗时 13.03 秒；完整输出 `.codex/full-tests.log`。
5. `git diff --check`：退出码 0，无输出。
6. `.venv/bin/python -m compileall -q app.py src utils tests`：退出码 0，无输出。

自动回归覆盖实际双进程提取协调、49 秒有效期预取、失效传播、租约恢复、窗口到期、供应商退避、超时、Cookie 失败与内容错误、退款/日志/限流、会话 Cookie 隔离及快手各条回退。

## 真实链路验证

命令：`.venv/bin/python .codex/live-validation.py`；完整输出 `.codex/live-validation.log`。脚本读取本地 `.env`，使用临时数据库且关闭后台无限补充，本次共提取三个真实 IP。

- 样例：`https://v.m.chenzhongtech.com/fw/photo/3xbr5pi8hxi4e6s`。
- 直连：HTTP 200，成功获取媒体，耗时 1.77 秒。
- 代理：前两个代理出现网络访问失败，被立即剔除；第三个代理成功获取媒体，HTTP 200，总耗时 32.11 秒。
- 库存结果：前两个地址 invalid=1，第三个 invalid=0，验证失败剔除及换 IP 后成功链路。

## 环境与验证边界

- 本机 Python 3.14；未发现 Python 3.11 或 Docker 可执行文件，因此未实际构建 Docker 镜像。代码沿用项目 Python 3.11 支持的语法及依赖。
- 真实样例验证成功不代表所有快手内容都可免 Cookie 解析。备用续期与并发边界通过可控时钟和本地 HTTP 服务测试验证；供应商持续不可用仍返回明确错误。
- 未执行 Git 提交、推送或线上部署。
