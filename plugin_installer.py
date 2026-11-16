"""把 dsh-go 写好的插件包安装到 AstrBot 的插件目录。

跨容器场景下 AstrBot 拿不到 dsh-go 那边写的文件，插件会先把这个 zip
下载回来，再由这里解压、校验、落到 ``data/plugins/<name>/``。

安全要点：
- 拒绝绝对路径和 ``..``（防 zip slip）
- 必须含 ``metadata.yaml``，否则不装
- 插件名只允许字母数字下划线横线，避免写到奇怪的位置
- 已存在同目录时不覆盖（除非显式允许）
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class InstallError(Exception):
    """插件包无法安装。"""


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """过滤掉危险条目：绝对路径、包含 .. 的路径、符号链接。"""
    safe: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or name.startswith("../") or "/../" in name:
            raise InstallError(f"压缩包里有非法路径：{info.filename}")
        # 只取普通文件与目录，忽略符号链接等特殊条目
        mode = (info.external_attr >> 16) & 0o170000
        if mode and mode not in (0o040000, 0o100000):
            continue
        safe.append(info)
    return safe


def _find_plugin_root(base: Path) -> Path:
    """在解压结果里找到含 metadata.yaml 的那一层。"""
    if (base / "metadata.yaml").is_file():
        return base
    candidates = [p.parent for p in base.rglob("metadata.yaml")]
    if not candidates:
        raise InstallError("压缩包里没有找到 metadata.yaml，不像一个 AstrBot 插件")
    # 取层级最浅的那个
    candidates.sort(key=lambda p: len(p.relative_to(base).parts))
    return candidates[0]


def _read_plugin_name(root: Path) -> str:
    """从 metadata.yaml 里读 name，读不到就用目录名。"""
    name = ""
    meta = root / "metadata.yaml"
    try:
        text = meta.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
            break
    if not name:
        name = root.name
    name = name.strip()
    if not _NAME_RE.match(name):
        raise InstallError(f"插件名不合法（只允许字母数字和 _.-）：{name!r}")
    return name


def install_plugin_zip(
    data: bytes,
    plugins_dir: str,
    overwrite: bool = False,
) -> str:
    """把 zip 字节装进 plugins_dir，返回装好的插件名。

    Args:
        data: zip 文件内容。
        plugins_dir: AstrBot 的插件目录（``data/plugins``）。
        overwrite: 已存在同名目录时是否覆盖。

    Raises:
        InstallError: 包损坏、缺 metadata.yaml、名字非法或目录已存在。
    """
    if not data:
        raise InstallError("插件包是空的")
    plugins_path = Path(plugins_dir)
    plugins_path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dsh-go-plugin-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "plugin.zip"
        archive.write_bytes(data)

        try:
            with zipfile.ZipFile(archive) as zf:
                members = _safe_members(zf)
                if not members:
                    raise InstallError("压缩包里没有任何文件")
                for info in members:
                    zf.extract(info, tmp_path / "extracted")
        except zipfile.BadZipFile as e:
            raise InstallError(f"不是有效的 zip 包：{e}") from e

        extracted = tmp_path / "extracted"
        root = _find_plugin_root(extracted)
        name = _read_plugin_name(root)

        dest = plugins_path / name
        if dest.exists():
            if not overwrite:
                raise InstallError(f"插件目录已存在：{dest}（如需覆盖请带「覆盖」字样）")
            shutil.rmtree(dest)

        shutil.copytree(root, dest)
        return name


def list_plugin_dirs(plugins_dir: str) -> list[str]:
    """列出当前已安装的插件目录名，用于提示。"""
    try:
        return sorted(
            p.name
            for p in Path(plugins_dir).iterdir()
            if p.is_dir() and (p / "metadata.yaml").is_file()
        )
    except OSError:
        return []


def default_plugins_dir() -> str:
    """AstrBot 的插件目录。取不到时返回空串，由调用方报错。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

        return get_astrbot_plugin_path()
    except Exception:
        return ""


def coerce_zip(data: bytes) -> bytes:
    """确保拿到的是字节串（桥接层的健壮性兜底）。"""
    if isinstance(data, bytes):
        return data
    return bytes(data or b"")


def is_zip(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def summarise_plugin(root: Path) -> str:
    """给用户看的简短摘要：文件数、体积。"""
    files = [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    return f"{len(files)} 个文件，{total / 1024:.0f} KB"


__all__ = [
    "InstallError",
    "coerce_zip",
    "default_plugins_dir",
    "install_plugin_zip",
    "is_zip",
    "list_plugin_dirs",
    "summarise_plugin",
]
