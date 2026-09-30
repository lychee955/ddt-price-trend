"""Authenticated encryption of the entire verified state, including its manifest."""

import io
import os
import tempfile
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .state import MAX_STATE_BYTES, pack_state, restore_state, verify_state

MAX_ENCRYPTED_BYTES = 100 * 1024 * 1024


def cipher(key_file=None):
    key = Path(key_file).read_bytes().strip() if key_file else os.environ.get("DDT_BACKUP_KEY", "").encode()
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ValueError("备份密钥缺失或格式无效；需要 Fernet 随机密钥") from None


def unpack_plain(content, target):
    if len(content) > MAX_STATE_BYTES + 1024 * 1024:
        raise ValueError("状态压缩包超过上限")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sorted(archive.namelist()) != ["manifest.json", "tracker.db"]:
            raise ValueError("状态压缩包必须仅包含 tracker.db 和 manifest.json")
        for name, limit in (("tracker.db", MAX_STATE_BYTES), ("manifest.json", 16 * 1024)):
            if archive.getinfo(name).file_size > limit:
                raise ValueError("状态压缩包成员超过上限")
            Path(target, name).write_bytes(archive.read(name))


def decrypt_bundle(content, target, key_file=None):
    if len(content) > MAX_ENCRYPTED_BYTES:
        raise ValueError("加密备份超过上限")
    try:
        plain = cipher(key_file).decrypt(content)
    except InvalidToken:
        raise ValueError("加密备份认证失败：密钥错误或文件损坏；拒绝恢复") from None
    unpack_plain(plain, target)


def pack_encrypted(output, metadata=None, key_file=None):
    encryption = cipher(key_file)  # Fail before touching the database if no key is configured.
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        manifest = pack_state(temporary, metadata)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("manifest.json", "tracker.db"):
                archive.write(Path(temporary, name), name)
        content = encryption.encrypt(buffer.getvalue())
        if len(content) > MAX_ENCRYPTED_BYTES:
            raise ValueError("加密备份超过上限")
        with output.open("xb") as handle:
            handle.write(content)
    return manifest


def read_encrypted(source, expected_run_id=None, key_file=None, restore=False):
    source = Path(source)
    if source.stat().st_size > MAX_ENCRYPTED_BYTES:
        raise ValueError("加密备份超过上限")
    with tempfile.TemporaryDirectory() as temporary:
        decrypt_bundle(source.read_bytes(), temporary, key_file)
        operation = restore_state if restore else verify_state
        return operation(temporary, expected_run_id)
