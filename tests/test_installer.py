"""插件包安装：zip slip 防护、metadata 校验、覆盖策略。"""
import io
import zipfile

import pytest

from astrbot_plugin_dsh_go.plugin_installer import InstallError, install_plugin_zip, is_zip, list_plugin_dirs

META = "name: astrbot_plugin_demo\nversion: 1.0.0\nauthor: t\n"


def make_zip(files: dict[str, str], prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(prefix + name, content)
    return buf.getvalue()


def test_install_flat(tmp_path):
    data = make_zip({"metadata.yaml": META, "main.py": "x = 1\n"})
    name = install_plugin_zip(data, str(tmp_path))
    assert name == "astrbot_plugin_demo"
    assert (tmp_path / name / "main.py").exists()


def test_install_nested(tmp_path):
    data = make_zip({"metadata.yaml": META, "main.py": "x"}, prefix="astrbot_plugin_demo/")
    assert install_plugin_zip(data, str(tmp_path)) == "astrbot_plugin_demo"


def test_missing_metadata(tmp_path):
    with pytest.raises(InstallError):
        install_plugin_zip(make_zip({"main.py": "x"}), str(tmp_path))


def test_reject_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.yaml", META)
        zf.writestr("../../evil.txt", "bad")
    with pytest.raises(InstallError):
        install_plugin_zip(buf.getvalue(), str(tmp_path))
    assert not (tmp_path.parent / "evil.txt").exists()


def test_reject_bad_name(tmp_path):
    data = make_zip({"metadata.yaml": "name: ../../etc/passwd\n", "main.py": "x"})
    with pytest.raises(InstallError):
        install_plugin_zip(data, str(tmp_path))


def test_existing_needs_overwrite(tmp_path):
    data = make_zip({"metadata.yaml": META, "main.py": "x"})
    install_plugin_zip(data, str(tmp_path))
    with pytest.raises(InstallError):
        install_plugin_zip(data, str(tmp_path))
    assert install_plugin_zip(data, str(tmp_path), overwrite=True) == "astrbot_plugin_demo"


def test_bad_zip(tmp_path):
    with pytest.raises(InstallError):
        install_plugin_zip(b"not a zip", str(tmp_path))


def test_helpers(tmp_path):
    assert is_zip(make_zip({"a": "b"})) is True
    assert is_zip(b"hello") is False
    assert list_plugin_dirs(str(tmp_path)) == []
