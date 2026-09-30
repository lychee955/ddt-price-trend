# Actions / Pages 操作手册

## 首次部署

1. 将代码推送至公开仓库的默认分支，等待 CI 通过。
2. 仓库 Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**。
3. 在 Actions → Collect and publish → Run workflow 选择 `initialize`。该操作只初始化/导入状态并发布页面，不发送采集请求。
4. 初始化通过后新建一次 `collect` 运行，验证 GitHub 出口下单次完整采集；失败时先检查日志与冷却，不反复重试。
5. 在 Settings → Secrets and variables → Actions → Variables 新建 `DDT_SCHEDULE_ENABLED`，值为 `true`，开启每 2 小时第 17 分钟的计划任务。默认 UTC；相隔 2 小时在北京时间仍是同样频率。

看板地址为 `https://<owner>.github.io/<repository>/`。首次没有采集数据时显示空状态；成功采集后展示商品和走势。

## 导入现有本地历史

推荐首次导入已有数据库，而不是重新建立基线：

1. 确认本地没有正在采集的任务。备份支持在线一致性读取，但云端种子不能包含 queued/running 的未结束任务。
2. 在本地运行 `uv run ddt state-pack --output .tmp/seed-state`。输出 `tracker.db` 和 `manifest.json`；脚本校验 SQLite、外键、数据库版本和 SHA-256。
3. 将这两个文件直接压缩为 `state.zip`，ZIP 根目录不能额外包含文件夹。PowerShell 示例：

   ```powershell
   Compress-Archive -LiteralPath .tmp/seed-state/tracker.db,.tmp/seed-state/manifest.json -DestinationPath .tmp/state.zip
   ```

4. 在当前 GitHub 仓库创建一个备份 Release，例如标签 `initial-state`，上传 `state.zip`。该文件是公开完整数据库，可能含运行日志；发布前应检查其中没有私有信息。它不计入 Git 历史，但仍属于独立公开备份。
5. 运行 `initialize`，将 `seed_release` 填为 `initial-state`。工作流从当前仓库下载种子、验证并恢复。已有 artifact 状态时拒绝初始化。

云端第一次恢复后会关闭数据库内的常驻定时调度，仅由 Actions cron 驱动；请求延时、重试、下降阈值和冷却设置继续保留。需要修改云端设置时，应新增受校验的管理流程，不能在公开网页嵌入写权限令牌。

## 数据保存和浏览器读取

- 每轮从最新有效 `ddt-state-*` artifact 恢复整库到 runner 临时目录。
- 普通 collect / publish 要求备份属于上一轮未跳过的工作流。上一轮没有保存状态时拒绝自动回退。
- 采集返回失败仍会保存失败次数、冷却时间及请求记录；只有成功批次更新价格基准。
- 保存后回下载新 artifact，与本地备份 manifest 完整比对，验证通过才清理旧状态、生成网站。
- 网站 `data/manifest.json` 指向 `data/versions/<version>/` 下的概览、商品、批次、变化及按商品拆分的历史 JSON。网站每分钟检查新 manifest，历史按需读取。
- 浏览器处理筛选、排序与分页。静态数据不包含原始请求日志、配置或访问凭据；失败原因在网页中使用通用提示，详细诊断查 Actions。
- 失败批次沿用最后成功价格，可更新网页中的采集失败提示和历史空档。

## 故障恢复

- **冷却**：下一轮不请求网站，仍保存并发布当前状态。手动 collect 不能绕过。
- **备份过期 / 丢失**：停止；寻找独立备份。不要选择空 initialize 来掩盖历史丢失。
- **上一轮中断 / 状态上传失败**：停止；确认日志及最后备份后，新建 `recover` 操作。恢复最新保留状态，至少冷却 60 分钟，本轮不采集，随后才能新建 collect。
- **备份校验失败**：停止并排查。不要自动尝试更旧版本，以免丢失未知的冷却状态。
- **网页部署失败但状态已经验证保存**：新建 `publish`，不再次请求采集网站。
- **不要点击 Re-run jobs**：旧运行重跑会被拒绝，防止重复采集；改为新建 Run workflow。
- 关闭 `DDT_SCHEDULE_ENABLED` 可停用定时采集，手动操作仍可用。GitHub 60 天无仓库活动可自动停用公开仓库 cron，需要维护时重新启用。

## 保持免费与独立备份

公开仓库标准 runner 的分钟数免费，存储仍有额度。工作流不使用付费 larger runner，也不缓存数据库。

- 单份未压缩数据库上限 100 MiB，最近 3 份滚动保留；上传新版本时会短暂保留第 4 份。
- Pages 目录上限 5 MiB，Pages artifact 保留 1 天。实际压缩占用通常更小，但必须关注账户共享的 500 MB artifact/Packages 额度及其他仓库占用。
- 不启用依赖缓存，避免额外 cache 管理和费用。
- 绑定支付方式的账户应在 Billing → Budgets and alerts 对相关 Actions/存储项设置 0 预算，并开启超额停止使用；单纯通知不会阻止计费。免费额度不足时接受任务暂停。
- 定期下载一个经过校验的状态 artifact 到本地独立保留。artifact 默认最多保留 90 天，删除所属工作流会一并删除。
- 下载后解压，再在新的空数据目录运行 `ddt state-restore --source <bundle>`。恢复命令不会覆盖已有数据库。
- 所有历史仍保留在 SQLite。网站超过 5 MiB 时应优化公开导出方式，而不是删掉数据库中的历史快照。

定时生产采集的用途需符合 GitHub Actions 平台条款；技术部署不代表平台长期可用承诺。网站拦截 GitHub 出口时遵循冷却与停止规则，不切换代理绕过。
