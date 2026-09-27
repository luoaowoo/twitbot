"""梗图模板库（core/memes.py）的离线测试。

合成在浏览器 Canvas 做，所以这里只测**文件名清洗 + 存取**这两件事 ——
那是唯一的安全边界（防路径穿越、防覆盖）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import memes
from web import server as web_server


@pytest.fixture(autouse=True)
def _clean():
    d = memes.memes_dir()
    if d.is_dir():
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
    yield
    d = memes.memes_dir()
    if d.is_dir():
        for p in d.iterdir():
            if p.is_file():
                p.unlink()


# ── 文件名清洗（安全边界）──────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", "   ", "noext", "x.exe", "x.php", ".png", "a<b>.png", "x|y.png",
    "nul\x00.png", "x?.png", "/etc/passwd",  "dir/",  "..",
])
def test_safe_name_rejects_bad(bad):
    assert memes.safe_name(bad) == ""


@pytest.mark.parametrize("raw,expect", [
    ("a.png", "a.png"),
    ("A.JPG", "A.jpg"),
    # 路径一律只取 basename —— 这既挡住穿越，也兼容浏览器上传的
    # "C:\fakepath\x.png"，所以不算「拒绝」，而是「清洗成安全名」
    ("../a.png", "a.png"),
    ("..\\a.png", "a.png"),
    ("/tmp/a.png", "a.png"),
    ("C:\\fakepath\\a.png", "a.png"),
    ("dir/sub/b.webp", "b.webp"),
    ("中文名字.png", "中文名字.png"),
    ("x.jpeg", "x.jpeg"),
])
def test_safe_name_accepts_good(raw, expect):
    assert memes.safe_name(raw) == expect


def test_safe_name_neutralizes_traversal():
    """穿越必须被中和：结果里不能带目录分隔符、不能是 ..。"""
    for raw in ("../evil.png", "..\\..\\evil.png", "/etc/evil.png",
                "C:\\Windows\\evil.png"):
        got = memes.safe_name(raw)
        assert got == "evil.png", (raw, got)
        assert "/" not in got and "\\" not in got and got != ".."


def test_safe_name_truncates_long_stem():
    name = memes.safe_name("x" * 200 + ".png")
    assert name.endswith(".png") and len(name) < 80


# ── 存取 ───────────────────────────────────────────────────

def test_save_and_list_and_delete():
    ok, msg, name = memes.save_template("tpl.png", b"\x89PNG-fake")
    assert ok and name == "tpl.png"
    rows = memes.list_templates()
    assert [r["name"] for r in rows] == ["tpl.png"]
    assert rows[0]["size"] == 9 and rows[0]["url"].endswith("tpl.png")

    ok2, msg2 = memes.delete_template("tpl.png")
    assert ok2 and memes.list_templates() == []


def test_save_rejects_empty_and_bad_name():
    assert memes.save_template("a.png", b"")[0] is False
    assert memes.save_template("a.exe", b"x")[0] is False
    assert memes.save_template("noext", b"x")[0] is False


def test_save_sanitizes_path_like_name():
    """带路径的名字会被洗成 basename，且落在 memes 目录内。"""
    ok, _, name = memes.save_template("../evil.png", b"x")
    assert ok and name == "evil.png"
    p = memes.template_path(name)
    assert p is not None and p.parent == memes.memes_dir()


def test_save_same_name_does_not_overwrite():
    _, _, n1 = memes.save_template("dup.png", b"one")
    ok, _, n2 = memes.save_template("dup.png", b"two")
    assert ok and n1 != n2
    assert len(memes.list_templates()) == 2


def test_delete_missing_and_bad_name():
    assert memes.delete_template("nope.png")[0] is False
    assert memes.delete_template("../x.png")[0] is False


def test_template_path_guards_traversal():
    memes.save_template("ok.png", b"x")
    assert memes.template_path("ok.png") is not None
    # 带路径的会被洗成 basename，仍然指向 memes 目录内的文件
    got = memes.template_path("../ok.png")
    assert got is None or got.parent == memes.memes_dir()
    assert memes.template_path("../../etc/passwd") is None


def test_list_templates_ignores_non_images():
    d = memes.memes_dir(); d.mkdir(parents=True, exist_ok=True)
    (d / "a.png").write_bytes(b"x")
    (d / "note.txt").write_text("nope", encoding="utf-8")
    (d / "noext").write_bytes(b"x")
    assert [r["name"] for r in memes.list_templates()] == ["a.png"]


# ── 控制台接口 ─────────────────────────────────────────────

def test_search_by_name():
    for n in ("cat.png", "CAT-2.png", "dog.jpg"):
        memes.save_template(n, b"x" * 8)
    assert len(memes.search("")) == 3
    assert {r["name"] for r in memes.search("cat")} == {"cat.png", "CAT-2.png"}
    assert [r["name"] for r in memes.search("dog")] == ["dog.jpg"]
    assert memes.search("nope") == []


def test_stats_counts_and_bytes():
    memes.save_template("a.png", b"x" * 100)
    memes.save_template("b.png", b"y" * 50)
    st = memes.stats()
    assert st["count"] == 2 and st["bytes"] == 150


def test_search_on_empty_library():
    assert memes.search("") == [] and memes.stats() == {"count": 0, "bytes": 0}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "-")     # 关掉服务器版的密码门
    return TestClient(web_server.create_app())


def test_api_list_memes_empty(client):
    r = client.get("/api/memes")
    assert r.status_code == 200
    assert r.json()["templates"] == []
    assert r.json()["stats"] == {"count": 0, "bytes": 0}


def test_api_memes_supports_search(client):
    for n in ("cat.png", "dog.jpg"):
        client.post("/api/memes/upload", files={"file": (n, b"x" * 8, "image/png")})
    allr = client.get("/api/memes").json()
    assert allr["stats"]["count"] == 2 and len(allr["templates"]) == 2
    hit = client.get("/api/memes", params={"q": "cat"}).json()
    assert [t["name"] for t in hit["templates"]] == ["cat.png"]
    assert hit["stats"]["count"] == 2          # stats 始终是总量，不受筛选影响


def test_api_upload_then_list_then_fetch(client):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    r = client.post("/api/memes/upload",
                    files={"file": ("tpl.png", png, "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["name"] == "tpl.png"

    listed = client.get("/api/memes").json()["templates"]
    assert [x["name"] for x in listed] == ["tpl.png"]

    got = client.get("/api/memes/file", params={"name": "tpl.png"})
    assert got.status_code == 200 and got.content == png


def test_api_upload_rejects_bad_extension(client):
    r = client.post("/api/memes/upload",
                    files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_api_upload_rejects_empty(client):
    r = client.post("/api/memes/upload",
                    files={"file": ("a.png", b"", "image/png")})
    assert r.status_code == 400


def test_api_file_404_for_missing(client):
    assert client.get("/api/memes/file", params={"name": "nope.png"}).status_code == 404


def test_api_file_rejects_traversal(client):
    r = client.get("/api/memes/file", params={"name": "../../etc/passwd"})
    assert r.status_code == 404


def test_api_delete(client):
    client.post("/api/memes/upload", files={"file": ("d.png", b"x" * 8, "image/png")})
    r = client.post("/api/memes/delete", json={"name": "d.png"})
    assert r.status_code == 200 and client.get("/api/memes").json()["templates"] == []


def test_api_delete_missing_is_400(client):
    assert client.post("/api/memes/delete", json={"name": "nope.png"}).status_code == 400
