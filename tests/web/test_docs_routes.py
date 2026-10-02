"""Web route tests for the Docs feature.

Guards the /api/docs namespace against the Swagger-UI path collision (FastAPI's
docs_url once squatted on /api/docs and returned HTML there) and exercises a
create → edit round-trip with content sqlite must store verbatim.
"""

from __future__ import annotations


def test_list_docs_returns_json_not_swagger(open_client):
    """GET /api/docs must be our JSON list, never the Swagger UI HTML."""
    r = open_client.get("/api/docs")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    assert r.json() == {"docs": []}


def test_swagger_ui_relocated(open_client):
    assert open_client.get("/api/_docs").status_code == 200  # Swagger moved here


def test_create_edit_roundtrip_with_special_chars(open_client):
    # Emoji, quotes, backslashes, newlines, unicode — sqlite stores text as-is;
    # the JSON transport must carry it intact both ways.
    body = 'Line "one" 😀\n\n## Hé\\ading\n\n- x\n- y\n\n[link](https://e.com)'
    r = open_client.post(
        "/api/docs", json={"doc_key": "Rules FAQ!", "title": "Rules", "body_md": body}
    )
    assert r.status_code == 200, r.text
    assert r.json()["doc_key"] == "rules-faq"  # slugified

    r = open_client.put(
        "/api/docs/rules-faq",
        json={"title": "Rules", "body_md": body, "accent": "#E6B84C"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["doc"]["body_md"] == body  # round-tripped verbatim

    got = open_client.get("/api/docs/rules-faq")
    assert got.json()["body_md"] == body


def test_duplicate_key_conflicts(open_client):
    open_client.post("/api/docs", json={"doc_key": "rules", "body_md": ""})
    r = open_client.post("/api/docs", json={"doc_key": "rules", "body_md": ""})
    assert r.status_code == 409


def _png(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_upload_pads_a_square_image_and_keeps_a_portrait_verbatim(
    open_client, tmp_path, monkeypatch
):
    from PIL import Image

    from web_server.routes import docs as docs_routes

    tmp_path = tmp_path / "doc-images"  # the client fixture keeps its own files in tmp_path
    monkeypatch.setattr(docs_routes, "_IMAGE_DIR", tmp_path)

    r = open_client.post("/api/docs/images", files={"file": ("sq.png", _png(200, 200))})
    assert r.status_code == 200, r.text
    assert r.json()["padded"] is True
    (saved,) = tmp_path.iterdir()
    with Image.open(saved) as img:
        assert img.size == (200, 267)
    saved.unlink()

    portrait = _png(300, 450)
    r = open_client.post("/api/docs/images", files={"file": ("p.png", portrait)})
    assert r.status_code == 200, r.text
    assert r.json()["padded"] is False
    (saved,) = tmp_path.iterdir()
    assert saved.read_bytes() == portrait
    assert r.json()["markdown"].endswith(f"{saved.name})")


def test_preview_returns_image_url(open_client):
    r = open_client.post(
        "/api/docs/preview",
        json={"title": "", "body_md": "![b](https://cdn/x.png)\n\nhi"},
    )
    assert r.status_code == 200
    assert r.json()["embeds"][0]["image_url"] == "https://cdn/x.png"
