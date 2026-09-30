from pathlib import Path

site = Path("frontend/dist")
if sum(path.stat().st_size for path in site.rglob("*") if path.is_file()) > 5 * 1024 * 1024:
    raise SystemExit("只读看板超过 5 MiB，停止发布；请优化数据体积，避免超出免费存储")
if list(site.rglob("*.db")) or not (site / "data/manifest.json").is_file():
    raise SystemExit("看板包含数据库或缺少数据 manifest")
print("Pages 文件校验通过")
