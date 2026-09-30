# 弹弹堂号价跟踪

使用 uv 管理的 Python 项目。采集游戏店弹弹堂账号（game_id=80，cat_id=1），覆盖上架与公示期。

## 启动

需要 Python 3.12+、uv，以及仅构建前端时需要的 Node.js 22+ 和 npm。

```sh
uv sync --locked
cd frontend
npm ci
npm run build
cd ..
uv run ddt serve
```

打开 http://127.0.0.1:8000 。`serve` 同时启动管理页面和独立采集进程。默认关闭自动采集，在「采集管理」中开启；首次手动采集建立基线。

当前工作区已完成依赖安装和前端构建，Windows 下也可直接运行 `./start.ps1`。

```sh
uv run ddt crawl       # 一次真实全量采集；运行中的 worker 会代为处理
uv run ddt worker      # 单独运行采集进程
uv run ddt backup      # SQLite 在线一致性备份
uv run pytest
uv run ruff check .
uv build
```

本次环境中安装的项目本地 uv 位于 `.tools/uv-package/bin/uv.exe`，若 uv 不在 PATH，可用此路径替换上述 `uv`。

## 数据与部署

- 数据默认保存在当前目录的 `data/tracker.db`，可通过 `DDT_DATA_DIR` 指定绝对路径。多进程必须使用同一目录。
- 前端构建写入 `src/ddt/static`，随后 `uv build` 会将静态资源一同打包进 wheel。
- 默认仅监听 127.0.0.1。需要对外服务时，设置 `DDT_API_TOKEN`（至少 24 字符）并使用 `uv run ddt serve --host 0.0.0.0`；页面会要求输入访问令牌。请在反向代理中配置 HTTPS。
- 服务器运行 `serve` 或分别托管 `ddt worker` 与 `uvicorn ddt.api:app`，只启动一个采集 worker。文件锁防止重复启动，数据库约束防止任务并发。
- 机器关闭或休眠时不采集；重启后不会补跑积压的每个时段。
- 修改采集间隔会重新计算下一次计划时间。停用自动采集不会中断当前任务。
- `uv run ddt backup` 将备份写到 data/backups；建议用系统计划任务每天执行并另存副本。
- 数据库用 `PRAGMA user_version` 管理迁移，当前为 v1。升级前先备份。

## 判定与故障保护

- 初次完整采集为基线。后续新增指首次被本系统发现，不等于真实发布时间。
- 每轮保存全部商品快照，包括价格不变的商品；消失商品保存价格为空的观察记录。
- 价格变化比较商品上次观察到的价格；重新出现的商品同时记录重新出现及价格变化。
- 商品看板「总涨跌」为当前 / 最后观察价相较首次成功收录价的累计变化，展示金额与百分比；首次收录为零，首次价格为零时不显示百分比。
- 点击「本次涨跌」「总涨跌」表头或旁边的箭头，可按金额升序、降序或取消排序；后端先排序再分页，本次无调价记录按零排序。
- 消失一次为疑似下架，两次为持续未发现，不推断已经成交。详情页仅在明确状态容器出现售出/下架提示时确认；当前真实在售页已验证，真实下架提示仍需更多样本校准。
- 缺页、重复商品、总数变化、价格解析错误或总量下降超过阈值（默认 30%）均拒绝发布批次，不更新历史基准。大量真实下架也可能触发保护；人工检查后可调整阈值再采集。
- 所有列表分页串行抓取，请求间隔默认 3–8 秒；超时与 5xx 最多重试三次，指数退避。
- 429 遵守 Retry-After；403、验证页和异常跳转立即停止；冷却持久化，手动触发也不能绕过。
- 连续三次失败或一次明确访问拦截触发冷却。不会破解验证码、轮换代理或自动切换浏览器绕过限制。
- 普通 HTTP 已验证能获取数据，因此第一版不引入 Playwright 运行时。以后若页面确实改为动态渲染，再增加浏览器适配器。
- 采集有时间跨度，并非交易所式瞬时快照。全量扫描后复核首页，仍无法观察两次采集之间的全部变化。
- 公示期、上架中之外的状态不在列表采集范围内；从范围中消失首先记为未发现。
- 来源页面仅作数据解析，不执行其脚本；标题等文本在页面中由 Vue 转义。

## 目录

```text
src/ddt/       API、SQLite 存储、采集、比较、独立 worker、CLI
frontend/      Vue + Element Plus + ECharts，Vite 构建
tests/         比较规则、解析、故障保护、API 与调度测试
tests/fixtures 公开页面样本（2026-09-16），仅用于离线回归测试
```

Python 使用标准库 sqlite3 显式事务，便于审核批次发布的原子性；第一版未引入 ORM 和 Redis。依赖分别锁定于 uv.lock 和 frontend/package-lock.json。

前端在支持 WebMCP 的浏览器中提供只读 `read_ddt_price_changes` 工具，不支持的浏览器不受影响。该可选工具尚未在浏览器 WebMCP 上下文中验证；常规 HTTP API 已测试。

## GitHub Actions 与免费只读看板

支持公开仓库中使用标准 Linux runner 单次采集，并将 JSON 与只读前端发布到 GitHub Pages。本地管理模式保持可用。

- `.github/workflows/ci.yml`：离线测试、前端两种模式构建及 wheel 资源检查。
- `.github/workflows/collect.yml`：恢复完整 SQLite 状态、单次采集、一致性备份与校验、静态导出、Pages 发布。
- 每 2 小时第 17 分钟计划运行。需设置仓库变量 `DDT_SCHEDULE_ENABLED=true` 才启用定时采集；GitHub 调度可能延迟或漏跑。
- 首次在 Actions 的 **Collect and publish → Run workflow** 选择 `initialize`。可以导入已有状态；之后选择 `collect` 采集，或 `publish` 仅重新发布网页。
- SQLite 完整状态保存在 `ddt-state-<run_id>-<attempt>` artifact，最新 3 份滚动保留、最长 90 天。失败和冷却也保存在状态中。数据库不提交到 Git、不发布到 Pages；公开仓库的 artifact 仍可被有读取权限的人下载。
- 公开网页只读取导出 JSON，可筛选、排序、查看涨跌及历史。网页显示最后成功时间和失败状态，不提供在线设置或直接触发采集。
- 状态丢失、损坏或上一轮未完成保存时停止采集；检查后通过 `recover` 明确恢复旧状态，本轮不采集并至少冷却 60 分钟。不要用重新初始化绕过失败或冷却。

Pages 设置、首次导入、故障恢复和免费额度控制详见 [操作手册](docs/github-actions-setup.md)；方案调研见 [部署调研](docs/github-actions-deployment.md)。

本地构建静态模式（PowerShell）：

```powershell
$env:VITE_STATIC_MODE = 'true'
$env:VITE_BASE_PATH = '/ddt-price-trend/'
cd frontend
npm ci
npm run build
cd ..
uv run ddt export --output frontend/dist/data
```

静态模式输出到 `frontend/dist`。恢复本地管理模式时删除这两个环境变量，再执行普通 `npm run build`；普通构建仍输出到 `src/ddt/static`。
