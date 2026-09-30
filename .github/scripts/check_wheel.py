from pathlib import Path
from zipfile import ZipFile

wheel = max(Path("dist").glob("*.whl"), key=lambda path: path.stat().st_mtime)
with ZipFile(wheel) as archive:
    names = set(archive.namelist())
    required = {"ddt/static/index.html", "ddt/sql/schema.sql", "ddt/sql/products.sql"}
    if not required <= names or not any(name.startswith("ddt/static/assets/") for name in names):
        raise SystemExit("wheel 缺少前端构建资源或 SQL")
print("wheel 静态资源与 SQL 校验通过")
