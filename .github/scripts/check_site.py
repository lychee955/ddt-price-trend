import os
from pathlib import Path

site = Path("frontend/dist")
try:
    limit_mib = int(os.environ.get("DDT_PAGES_MAX_MIB", "20"))
except ValueError:
    raise SystemExit("DDT_PAGES_MAX_MIB 必须是正整数（单位 MiB）") from None
if limit_mib <= 0:
    raise SystemExit("DDT_PAGES_MAX_MIB 必须是正整数（单位 MiB）")

files = [(path, path.stat().st_size) for path in site.rglob("*") if path.is_file()]
total = sum(size for _, size in files)
data_size = sum(size for path, size in files if path.is_relative_to(site / "data"))
print(f"Pages 未压缩大小：{total / 1024**2:.2f} MiB（{total:,} 字节），上限 {limit_mib} MiB", flush=True)
print(f"公开数据：{data_size / 1024**2:.2f} MiB；网页资源：{(total - data_size) / 1024**2:.2f} MiB", flush=True)
for path, size in sorted(files, key=lambda entry: entry[1], reverse=True)[:5]:
    print(f"  {size / 1024**2:.2f} MiB  {path.relative_to(site)}", flush=True)
if total > limit_mib * 1024 * 1024:
    raise SystemExit(
        f"只读看板超过 {limit_mib} MiB，停止发布；请优化公开数据，"
        "或确认账户存储余量后调整仓库变量 DDT_PAGES_MAX_MIB"
    )
if list(site.rglob("*.db")) or not (site / "data/manifest.json").is_file():
    raise SystemExit("看板包含数据库或缺少数据 manifest")
print("Pages 文件校验通过")
