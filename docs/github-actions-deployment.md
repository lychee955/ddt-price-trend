# GitHub Actions 免费部署调研

调研日期：2026-09-30。依据当前工作区代码及 GitHub 官方文档；尚未在 GitHub runner 上执行真实采集。

用户已确认：代码仓库和商品价格看板均可公开。以下优先选择公开仓库方案；私有仓库额度仅供比较。

## 结论

本项目可以改造成“GitHub Actions 定时单次采集 + GitHub Pages 只读价格看板”，在公开仓库、标准 Linux runner、存储不超过免费额度的条件下运行。它适合个人使用、容忍采集延迟的场景。

现有 FastAPI 管理平台不能直接完整部署到 Actions。Actions 提供临时任务执行环境，Pages 只托管静态内容；保留网页手动采集、在线修改设置、实时任务进度和访问令牌保护，需要另一个常驻后端。

建议先验证 Actions 环境下的单次采集和状态恢复，再建设只读看板。若要求完整功能且不支付云服务器费用，最省改动的是在已有常开电脑运行现有服务，Actions 负责测试、构建和发布软件包；电脑、电力和网络仍由自己提供。

## 当前项目的适配情况

| 位置 | 已有能力 / 问题 | 对部署的影响 |
| --- | --- | --- |
| `src/ddt/cli.py` | `ddt crawl` 执行一次采集，成功返回 0、失败返回 1 | 可用于定时任务，不需要常驻 `serve` 或 `worker` |
| `src/ddt/worker.py` | `worker(once=True)`；失败次数、冷却、批次恢复 | 可保留采集规则，但必须恢复和保存数据库 |
| `src/ddt/db.py` | `DDT_DATA_DIR`、SQLite WAL、事务、互斥 | 临时 runner 必须从外部恢复完整状态 |
| `src/ddt/cli.py` | `ddt backup` 使用 SQLite backup API | 已有一致性备份入口；不要只复制运行中的 `tracker.db` |
| `frontend/src/App.vue` | 请求绝对路径 `/api/...`，后端筛选、排序、分页，5 秒轮询 | 直接上传构建产物到 Pages 会失效，需静态数据适配 |
| `frontend/vite.config.js` | 未设置项目 Pages 的 `base` | 需适配 `/<repository>/` 路径 |
| `.gitignore` / 项目约定 | `data/` 不提交 | 不采用把 SQLite 定期提交到代码分支的方式 |

目前没有 `.github/workflows/`。本次只增加调研文档，不更改已有业务代码和本地运行数据。

## 免费额度与运行限制

- **公开仓库**：标准 GitHub-hosted runner 的运行分钟数免费；larger runner 即使在公开仓库也收费。[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- **私有仓库**：GitHub Free 每月包含 2,000 分钟；额度由仓库所有者账户共享，还要计入其他项目和 CI 的使用。[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- **存储**：GitHub Free 包含 500 MB artifact 存储（与 Packages 共享相关额度），每仓库 10 GB cache。公开仓库运行时间免费不代表 artifact 存储无限免费。[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- **任务时长**：GitHub-hosted runner 单个 job 最长 6 小时；每次使用新的执行实例，不能依靠上一轮的本地目录。[任务限制](https://docs.github.com/en/actions/reference/limits)、[工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- **Pages**：GitHub Free 支持公开仓库的 Pages；它是静态托管，不能运行 Python、FastAPI 或服务器端 SQLite 查询。网站大小上限 1 GB，月带宽软限制 100 GB。[Pages 功能](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)、[Pages 限制](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
- **调度**：最短间隔 5 分钟，但高负载时可能延迟或丢弃；仅默认分支运行。公开仓库连续 60 天无仓库活动，定时工作流会自动停用。页面应展示最后成功采集时间，不能把 cron 当作实时保证。[定时事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

本项目应从每 2 小时一次开始，选择第 17 分钟等非整点时刻；不是建议按 GitHub 允许的最短间隔采集。保留现有串行 3–8 秒间隔、重试退避和冷却，不为缩短 Actions 用时提高请求频率。

私有仓库按 30 天估算，下面的“单轮用时”包括环境准备、状态下载、采集、备份和上传，属于预算情景，不是实测值：

| 频率 | 每月轮数 | 单轮 5 分钟 | 单轮 10 分钟 |
| --- | ---: | ---: | ---: |
| 每小时 | 720 | 3,600 分钟 | 7,200 分钟 |
| 每 2 小时 | 360 | 1,800 分钟 | 3,600 分钟 |
| 每 4 小时 | 180 | 900 分钟 | 1,800 分钟 |

因此私有仓库优先每 4 小时一次，并为 CI、重试和 31 天月份留出余量；实际频率应根据前几轮耗时调整。

为了维持零付费，使用标准 Linux runner，控制 artifact 数量和大小，不扩大 cache 的免费容量。若账户已绑定付款方式，应设置相关 Actions/存储预算为 0 并启用超预算停止使用；只设置邮件告警不能阻止收费。预算不追溯设置前的使用。[预算设置](https://docs.github.com/en/billing/how-tos/set-up-budgets)、[预算限制](https://docs.github.com/en/billing/concepts/budgets-and-alerts)

## 推荐架构：定时采集与只读看板

```text
schedule / workflow_dispatch
        ↓
恢复最近一次已验证的完整 SQLite 状态
        ↓
Python 3.12 + uv sync --locked
        ↓
uv run ddt crawl（原有限速、互斥、完整性校验）
        ↓
uv run ddt backup → 校验 → 保存新状态
        ↓ 仅成功批次更新公开业务数据
导出 JSON → Vue 静态模式 → GitHub Pages
```

推荐拆分三个工作流职责：

1. **CI**：push / pull request 运行 `uv run pytest`、`uv run ruff check src tests`、`npm ci`、`npm run build`；构建后可执行 `uv build` 并核对静态资源和 SQL 打包。只使用离线样本，不触发真实采集。
2. **采集**：schedule / workflow_dispatch 恢复状态、执行 `ddt crawl`、保存状态。定时和手动共用同一个 concurrency group，`cancel-in-progress: false`，防止中断正在保存的批次。不累积补跑过时计划。
3. **页面发布**：使用已经保存成功的状态生成 JSON 和前端构建。采集失败时沿用上次成功业务数据；可单独更新最后尝试时间、失败状态等元数据，不生成虚假的价格变化或下架事件。

所有写入同一个远程状态的流程必须共享串行边界；现有文件锁只能保护单台 runner，不能代替跨任务互斥。页面发布也要避免旧版本覆盖新版本。

页面需要的数据包括概览、商品列表、变化记录、采集批次及商品历史。商品筛选/排序/分页可在浏览器执行；历史按商品分文件，并用同一版本的 manifest 对齐，避免一次下载全部历史。保持商品 ID 唯一标识、价格整数分及失败批次造成的历史空档。

静态模式隐藏在线设置、网页直接采集、令牌登录和常驻 worker 在线指示，改为显示更新时间、数据是否过期及采集状态。仓库管理员通过 GitHub 的 Run workflow 手动采集。不要把具有写权限的 GitHub token 或 `DDT_API_TOKEN` 放进前端文件；公开静态页面的登录框无法保护可直接下载的 JSON。

## 最关键的问题：跨任务保存数据库

必须保存整个 SQLite 状态，包括历史快照、成功基准、设置、失败次数、冷却和任务记录；只保存最后一次成功采集的数据库会丢失随后发生的失败和冷却。

### 首阶段：滚动 artifact + 独立备份

对小数据库，artifact 是最容易验证的状态接力方式，但不应作为唯一长期备份：默认 90 天过期，删除所属 workflow run 也会删除 artifact。只有登录且有仓库读取权限的人能下载，因此公开仓库的 artifact 不是私有存储。[保留与删除](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/remove-workflow-artifacts)、[下载权限](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)

实现要求：

- 恢复最新的**有效状态备份**，不能只找最新的成功工作流；失败采集也可能生成必须恢复的状态。
- 下载或校验失败立即停止；只有明确初始化才允许空数据库建立新基线，不把“备份找不到”当成首次运行。
- 每次保存使用唯一名称及 manifest（状态序号、源工作流、数据库版本、校验和、最近成功批次）。恢复后执行 SQLite 完整性检查。
- 把采集失败与备份失败分开处理：采集返回 1 后仍运行状态备份，最后工作流保留失败结果；不能用简单的成功后上传流程。
- 仅在新备份上传并校验成功后，删除超出保留数量的旧备份。例如保留最近 3 个，再定期下载到本地留副本；同时计入 CI 和 Pages 发布的 artifact 占用。
- 强制取消、runner 故障或超时无法保证收尾备份执行。发现上一轮异常终止或保存失败应暂停下一轮真实采集，先确认状态和冷却，不能静默恢复旧状态继续请求。
- 远程状态保存失败时不发布新页面；保留旧备份，报告故障，恢复后再继续。

容量需要实测：假设每个完整备份 20 MB，每 2 小时一次且保留 30 天，将产生约 7.2 GB 占用；只留 3 个约 60 MB。这里只是说明为什么必须滚动清理；项目目前每轮保留完整快照，数据库本身也会持续增长，不能承诺永久低于免费额度。

### 长期存储备选

| 方法 | 判断 |
| --- | --- |
| Actions cache | 不作为数据库主存储；可能被淘汰，超过 7 天未访问会删除，也不是私有秘密存储。仅缓存依赖。[缓存限制](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching) |
| SQLite 提交到代码或数据分支 | 不采用，违反本项目运行数据不提交的约定，且二进制历史持续膨胀 |
| GitHub Release assets | 可调研为少量版本化数据库备份；单文件必须小于 2 GiB，官方未规定 Release 总大小或带宽上限，但这不等于数据库托管承诺。需评估用途适配、权限、恢复协议和保留策略，不把它视为无限免费数据库。[Release 文档](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) |
| 独立对象存储 | 适合完整状态文件的长期主存储，但必须另查提供商的免费额度、绑卡要求和超额处理；不在本次结论中承诺免费 |
| 已有常开电脑上的 SQLite | 最少业务改动，保留现有完整功能；不具备无设备成本、全天候云托管的效果 |

如果代码公开而完整数据库需私有，优先考虑独立私有存储；也可对备份加密后上传，密钥放 GitHub Secrets，但失去密钥就不能恢复。公开看板仍只能导出允许公开的字段。不要把完整数据库、访问凭据和内部请求日志放进 Pages。

## 验证与实施顺序

1. 采用已获同意的公开代码仓库和公开只读看板；发布前确定 JSON 字段白名单，数据库备份另外管理。Free 下私有仓库不能直接使用 Pages，该限制不影响本次公开方案。
2. 添加 CI，先验证 Linux 下离线测试、构建和打包，保持 `uv.lock` / `package-lock.json` 对齐。
3. 用离线测试验证两轮状态恢复、失败状态续存、冷却不能绕过、备份损坏、状态缺失和任务中断保护。
4. 状态链路验证后，在 GitHub 上仅做一次必要的真实全量采集，记录网络可达性、耗时和状态文件大小。标准 runner 使用云数据中心出口，本地可采集不代表 GitHub 出口一定可用。[runner 网络说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
5. 验证网页静态数据模式和 Pages 子路径；成功、失败、过期数据及商品历史空档均要能正确展示。
6. 再开启低频 cron，观察配额并维护备份；真实访问被拦截时遵循项目冷却规则，不轮换代理或换 runner 绕过。

还需关注平台用途边界：GitHub 将 Actions 定位于项目软件的开发、测试、部署和发布，限制把它当服务器或无关计算服务。定时生产采集是否符合具体使用场景不能仅凭技术可运行就保证；如持续作为采集后端，应确认用途符合其条款。Pages 适合个人项目展示，不适合作为交易平台或商业 SaaS。[GitHub 附加产品条款](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)

本次没有进行真实网站采集，没有新增工作流、上传数据或改变仓库可见性。落地改动应集中在工作流、状态恢复/备份脚本、只读导出和前端静态模式，继续使用现有解析、比较和事务发布逻辑。
