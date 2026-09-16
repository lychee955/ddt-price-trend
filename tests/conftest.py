import pytest

from ddt.db import init_db


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DDT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DDT_API_TOKEN", raising=False)
    init_db()
