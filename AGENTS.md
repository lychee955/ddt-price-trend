# 项目约定

- 使用中文沟通，改动保持简单、聚焦；详细说明见 `README.md`。
- Python 3.12+，使用 uv 管理依赖和构建；前端使用 npm。依赖变更同步更新对应锁文件。
- 后端：FastAPI、SQLite、HTTPX、Beautiful Soup；前端：Vue 3、Element Plus、ECharts、Vite。

## 代码位置

- `src/ddt/cli.py`：命令入口；`api.py`：接口；`worker.py`：任务调度。
- `crawler.py`：网络采集；`parser.py`：页面解析；`compare.py`：快照比较。
- `src/ddt/db.py`：数据库操作；`src/ddt/sql/schema.sql`：建表 DDL。
- `frontend/src/`：页面源码；`src/ddt/static/`：构建产物，不直接编辑。
- `tests/fixtures/`：离线页面样本；`data/`：运行数据，不提交、不用于测试。

## 常用命令

- 安装：`uv sync --locked`；前端在 `frontend/` 执行 `npm ci`。
- 启动：先在 `frontend/` 执行 `npm run build`，再在根目录执行 `uv run ddt serve`。
- 检查：`uv run pytest`、`uv run ruff check src tests`。
- 打包：前端构建完成后执行 `uv build`，确保静态资源和 SQL 文件包含在包内。
- uv 不在 PATH 时，可使用 `.tools/uv-package/bin/uv.exe`（若存在）。

## 关键规则

- 仅采集弹弹堂账号（`game_id=80`、`cat_id=1`），覆盖上架中和公示期。
- 商品 ID 为唯一标识，价格存整数分；首次成功采集建立基线，每次保留快照。
- 仅完整成功批次更新比较基准；批次发布保持事务原子性，失败不能产生下架误报。
- 列表消失不等于成交；保留疑似下架与持续未发现状态，不凭标题合并商品。
- 保留串行限速、重试退避、冷却和任务互斥；手动触发不能绕过这些限制。
- SQL 保持独立文件；修改已有表必须考虑迁移，不能依赖 `CREATE TABLE IF NOT EXISTS` 更新结构。
- 后端改动运行相关离线测试；前端改动执行构建。真实采集只用于必要验证，避免反复请求网站。
