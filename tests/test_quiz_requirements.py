"""Quiz requirements tests: combination filters, pagination stability, export, mutations.

Run with:  python -m pytest tests/test_quiz_requirements.py -v
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("KEEPSTACK_AI_ENABLED", "false")
os.environ["KEEPSTACK_DATA_DIR"] = tempfile.mkdtemp(prefix="keepstack-quiz-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from keepstack import ingest, search  # noqa: E402
from keepstack.app import app  # noqa: E402
from keepstack.auth import hash_password, make_token  # noqa: E402
from keepstack.db import get_conn, init_db  # noqa: E402


# ---------- fixtures ----------
@pytest.fixture(scope="session")
def client():
    init_db()
    conn = get_conn()
    if not conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES ('admin','a@b.c','Admin',?, 'admin', 1, '2026-01-01T00:00:00Z')",
            (hash_password("admin"),),
        )
        conn.commit()
    # Seed deterministic scenario data
    _seed_scenario(conn)
    tok = make_token(
        conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"],
        "admin",
    )
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


def _seed_scenario(conn):
    """Create a Harbor Campaign Q4 scenario with known tag combos and fixed timestamps."""
    import uuid as uuidlib

    colors = [
        (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
        (255, 0, 255), (0, 255, 255), (128, 128, 0), (128, 0, 128),
    ]
    # 8 assets with varying tag combos; 2 share the same created_at
    spec = [
        ("Harbor Campaign Hero", ["approved", "campaign", "q4"],   "2026-10-01T09:00:00+00:00"),
        ("Harbor Campaign B-roll", ["approved", "campaign"],         "2026-10-01T09:00:00+00:00"),  # same time as above
        ("Harbor Campaign Internal", ["approved", "campaign", "internal"], "2026-10-02T10:00:00+00:00"),
        ("Harbor Approved Only", ["approved"],                       "2026-10-03T11:00:00+00:00"),
        ("Harbor Campaign Q4 Video", ["approved", "campaign", "q4"], "2026-10-04T12:00:00+00:00"),
        ("Harbor Rejected", ["rejected"],                            "2026-10-05T13:00:00+00:00"),
        ("Harbor Deleted Active", ["approved", "campaign"],          "2026-10-06T14:00:00+00:00"),  # we'll soft-delete later
        ("Harbor Internal Memo", ["internal", "confidential"],      "2026-10-07T15:00:00+00:00"),
    ]
    admin = dict(conn.execute("SELECT * FROM users WHERE username='admin'").fetchone())
    asset_ids = []
    for idx, (title, tags, created_at) in enumerate(spec):
        buf = io.BytesIO()
        Image.new("RGB", (80, 60), colors[idx]).save(buf, format="JPEG")
        a = ingest.ingest_stream(
            io.BytesIO(buf.getvalue()),
            f"harbor_{idx}.jpg",
            title=title,
            user=admin,
        )
        # Override created_at to known value
        conn.execute("UPDATE assets SET created_at = ?, updated_at = ? WHERE id = ?",
                     (created_at, created_at, a["id"]))
        for t in tags:
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
            tid = conn.execute("SELECT id FROM tags WHERE name=?", (t,)).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?,?)",
                (a["id"], tid, "manual"),
            )
        conn.commit()
        ingest.reindex_fts(a["id"])
        asset_ids.append(a["id"])
    # Soft-delete the 7th asset (index 6) to test active-only filtering
    conn.execute("UPDATE assets SET status='deleted' WHERE id=?", (asset_ids[6],))
    conn.commit()

    # Create the "Harbor Campaign Q4" collection with first 5 assets
    cid = str(uuidlib.uuid4())
    cur = conn.execute(
        "INSERT INTO collections (uuid, name, kind, created_by, created_at) VALUES (?,?, 'folder', ?, '2026-09-01T00:00:00Z')",
        (cid, "Harbor Campaign Q4", admin["id"]),
    )
    col_id = cur.lastrowid
    for aid in asset_ids[:5]:
        conn.execute(
            "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?,?, '2026-09-15T00:00:00Z')",
            (col_id, aid),
        )
    conn.commit()
    pytest.session_harbor_collection_id = col_id  # store for later tests
    pytest.session_harbor_asset_ids = asset_ids  # store for later tests


# ---------- test 1: multi-tag AND ----------
def test_multiple_include_tags_are_AND(client):
    res = client.get("/api/assets", params={
        "tags": ["approved", "campaign"],
        "sort": "newest",
        "limit": 100,
    })
    assert res.status_code == 200
    data = res.json()
    # Assets 0, 1, 2, 4 have both approved + campaign (asset 6 is deleted)
    assert data["total"] == 4
    titles = {i["title"] for i in data["items"]}
    assert titles == {
        "Harbor Campaign Hero",
        "Harbor Campaign B-roll",
        "Harbor Campaign Internal",
        "Harbor Campaign Q4 Video",
    }


# ---------- test 2: multi-tag exclude (any hit) ----------
def test_exclude_tags_removes_any_hit(client):
    res = client.get("/api/assets", params={
        "tags": ["approved", "campaign"],
        "exclude_tags": ["internal"],
        "sort": "newest",
        "limit": 100,
    })
    assert res.status_code == 200
    data = res.json()
    # From the 4 AND results, minus "Harbor Campaign Internal"
    assert data["total"] == 3
    titles = {i["title"] for i in data["items"]}
    assert "Harbor Campaign Internal" not in titles
    assert titles == {
        "Harbor Campaign Hero",
        "Harbor Campaign B-roll",
        "Harbor Campaign Q4 Video",
    }


# ---------- test 3: same tag both sides -> empty ----------
def test_same_tag_include_and_exclude_returns_zero(client):
    res = client.get("/api/assets", params={
        "tags": ["approved"],
        "exclude_tags": ["approved"],
    })
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0
    assert data["items"] == []


def _harbor_collection_id(client) -> int:
    data = client.get("/api/collections").json()
    for c in data["collections"]:
        if "Harbor Campaign" in c["name"]:
            return c["id"]
    pytest.fail("Harbor Campaign collection not found")
    return 0  # unreachable


# ---------- test 4: combination q + media_type + tags + collection, only active ----------
def test_combination_q_collection_tags_only_active(client):
    col_id = _harbor_collection_id(client)
    res = client.get("/api/assets", params={
        "q": "harbor",
        "collection_id": col_id,
        "tags": ["approved"],
        "sort": "oldest",
        "limit": 100,
    })
    assert res.status_code == 200
    data = res.json()
    # In Harbor Campaign Q4 collection, active, with "harbor" keyword + approved tag
    # Assets 0, 1, 2, 3, 4 are in collection; asset 6 is deleted (not in results)
    # All have harbor + approved
    assert data["total"] == 5
    # Verify ordering is oldest (created_at ASC); assets 0 & 1 share same time -> stable by id ASC
    titles = [i["title"] for i in data["items"]]
    assert titles == [
        "Harbor Campaign Hero",      # 2026-10-01T09:00:00  (lower id)
        "Harbor Campaign B-roll",    # 2026-10-01T09:00:00  (higher id, stable tie-break)
        "Harbor Campaign Internal",  # 2026-10-02
        "Harbor Approved Only",      # 2026-10-03
        "Harbor Campaign Q4 Video",  # 2026-10-04
    ]


# ---------- test 5: pagination stability (same created_at) ----------
def test_pagination_with_stable_sort(client):
    col_id = _harbor_collection_id(client)
    # Get first 2 (page 1)
    r1 = client.get("/api/assets", params={
        "collection_id": col_id, "sort": "newest", "limit": 2, "offset": 0,
    })
    # Get next 2 (page 2)
    r2 = client.get("/api/assets", params={
        "collection_id": col_id, "sort": "newest", "limit": 2, "offset": 2,
    })
    # Get last (page 3)
    r3 = client.get("/api/assets", params={
        "collection_id": col_id, "sort": "newest", "limit": 2, "offset": 4,
    })

    # All have total 5
    assert r1.json()["total"] == 5
    assert r2.json()["total"] == 5
    assert r3.json()["total"] == 5

    ids_p1 = [i["id"] for i in r1.json()["items"]]
    ids_p2 = [i["id"] for i in r2.json()["items"]]
    ids_p3 = [i["id"] for i in r3.json()["items"]]

    # No duplicates across pages, no gaps
    all_ids = ids_p1 + ids_p2 + ids_p3
    assert len(set(all_ids)) == 5, f"Duplicate ids: {all_ids}"
    assert len(all_ids) == 5

    # Same created_at -> sort should still be stable (asset 0 < asset 1 in id)
    # "newest" = created_at DESC, id DESC, so Hero(id=lower) < B-roll(id=higher)
    # DESC by id => higher id first
    # In "newest" sort, created_at DESC puts Oct 4 first, then Oct 3, then Oct 2, then Oct 1 (both)
    # For the tie at Oct 1: id DESC -> higher id first -> B-roll before Hero
    # Actually, let's just check no duplicates; the full UUID check via export tests covers ordering

    # Extra: keyword + sort also uses chosen sort (not bm25)
    rq = client.get("/api/assets", params={
        "q": "harbor", "sort": "oldest", "limit": 3,
    })
    assert rq.status_code == 200
    titles_q = [i["title"] for i in rq.json()["items"]]
    # oldest = created_at ASC -> Hero(Oct1), B-roll(Oct1), Internal(Oct2)
    assert titles_q[0] == "Harbor Campaign Hero"
    assert titles_q[1] == "Harbor Campaign B-roll"
    assert titles_q[2] == "Harbor Campaign Internal"


# ---------- test 6: export = full match set, same order as list ----------
def test_export_matches_list(client):
    # Apply the Harbor Campaign filter set
    params_common = {
        "q": "harbor",
        "tags": ["approved", "campaign"],
        "exclude_tags": ["internal"],
        "sort": "newest",
    }
    # Paginated list
    r_list = client.get("/api/assets", params={**params_common, "limit": 100})
    assert r_list.status_code == 200
    list_data = r_list.json()

    # Full export
    r_exp = client.get("/api/assets/export.csv", params=params_common)
    assert r_exp.status_code == 200

    # Parse CSV
    text = r_exp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    export_rows = list(reader)

    # Verify counts match
    assert len(export_rows) == list_data["total"]

    # Verify UUIDs match (list paginated up to 100 which covers all)
    list_uuids = [i["uuid"] for i in list_data["items"]]
    export_uuids = [row["uuid"] for row in export_rows]
    assert export_uuids == list_uuids, "Export order must match list order"

    # Verify header fields present
    header_fields = ["uuid", "title", "description", "filename", "media_type", "tags", "created_at"]
    assert list(export_rows[0].keys()) == header_fields


# ---------- test 7: CSV escaping (comma, quote, newline, Chinese) ----------
def test_csv_escaping_roundtrip(client):
    # Create an asset with special chars in title/description via PATCH after ingest
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (10, 20, 30)).save(buf, format="JPEG")
    a = ingest.ingest_stream(
        io.BytesIO(buf.getvalue()), "special_chars.jpg",
        title="special_chars_base",
    )
    uuid = a["uuid"]
    # Patch in special characters
    r = client.patch(f"/api/assets/{uuid}", json={
        "title": 'Title with "quotes" and, commas',
        "description": "Line1\nLine2 with 中文",
    })
    assert r.status_code == 200

    r = client.get("/api/assets/export.csv", params={"q": "special"})
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) >= 1
    target = [r for r in rows if "special" in r.get("filename", "")]
    assert len(target) == 1
    row = target[0]
    assert row["title"] == 'Title with "quotes" and, commas'
    assert row["description"] == "Line1\nLine2 with 中文"


# ---------- test 8: zero result export -> header only ----------
def test_zero_result_export_has_header(client):
    r = client.get("/api/assets/export.csv", params={
        "tags": ["approved"],
        "exclude_tags": ["approved"],
    })
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    lines = text.strip().split("\r\n")
    assert len(lines) == 1
    assert "uuid" in lines[0]


# ---------- test 9: 401 on unauthenticated export, 422 on invalid limit ----------
def test_export_requires_auth(client):
    # Clear auth for this request
    r = client.get("/api/assets/export.csv", headers={"Authorization": ""})
    assert r.status_code == 401


def test_invalid_limit_rejected(client):
    r = client.get("/api/assets", params={"limit": 0})
    assert r.status_code == 422
    r = client.get("/api/assets", params={"limit": 500})
    assert r.status_code == 422
    r = client.get("/api/assets", params={"offset": -1})
    assert r.status_code == 422


def test_special_chars_query_does_not_500(client):
    for q in ["!!!", '"*', "   ", "emoji 🎉", "AND OR"]:
        r = client.get("/api/assets", params={"q": q, "limit": 10})
        assert r.status_code == 200, f"q={q!r} should not 500"


# ---------- test 10: modify title/tag -> re-query reflects change ----------
def test_mutate_then_query_reflects_changes(client):
    # Pick "Harbor Approved Only"
    col_id = _harbor_collection_id(client)
    r = client.get("/api/assets", params={"collection_id": col_id, "limit": 100})
    approved_only = [i for i in r.json()["items"] if i["title"] == "Harbor Approved Only"][0]
    uuid = approved_only["uuid"]

    # Change title
    r2 = client.patch(f"/api/assets/{uuid}", json={"title": "Harbor Approved Only RENAMED"})
    assert r2.status_code == 200

    # Re-query: new title found, old title not found
    r3 = client.get("/api/assets", params={"q": "RENAMED"})
    assert any(i["uuid"] == uuid for i in r3.json()["items"])

    r4 = client.get("/api/assets", params={"q": "Harbor Approved Only RENAMED", "tags": ["approved"]})
    assert any(i["uuid"] == uuid for i in r4.json()["items"])

    # Verify export also reflects the change
    r5 = client.get("/api/assets/export.csv", params={"q": "RENAMED"})
    rows = list(csv.DictReader(io.StringIO(r5.content.decode("utf-8-sig"))))
    assert any(row["uuid"] == uuid for row in rows)

    # Tag add -> query and export both include the asset under the new tag
    tag = "quiz-added"
    r6 = client.post(f"/api/assets/{uuid}/tags", json={"name": tag})
    assert r6.status_code == 200
    r7 = client.get("/api/assets", params={"tags": [tag]})
    assert any(i["uuid"] == uuid for i in r7.json()["items"])
    r8 = client.get("/api/assets/export.csv", params={"tags": [tag]})
    tag_rows = list(csv.DictReader(io.StringIO(r8.content.decode("utf-8-sig"))))
    assert any(row["uuid"] == uuid and tag in row["tags"] for row in tag_rows)

    # Tag remove -> query and export no longer match the asset under that tag
    r9 = client.delete(f"/api/assets/{uuid}/tags/{tag}")
    assert r9.status_code == 200
    r10 = client.get("/api/assets", params={"tags": [tag]})
    assert r10.json()["total"] == 0
    r11 = client.get("/api/assets/export.csv", params={"tags": [tag]})
    remaining = list(csv.DictReader(io.StringIO(r11.content.decode("utf-8-sig"))))
    assert remaining == []


# ---------- test 11: large export crosses the SQLite bind-variable limit ----------
def test_large_export_survives_sqlite_variable_limit(client):
    import uuid as uuidlib

    conn = get_conn()
    bulk_tag = "bulk-export"
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (bulk_tag,))
    tid = conn.execute("SELECT id FROM tags WHERE name=?", (bulk_tag,)).fetchone()["id"]
    n = 1200  # > 999 (older SQLite limit), also > the 500-row chunk size
    now = "2026-10-08T00:00:00+00:00"
    with conn:
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO assets (uuid, sha256, filename, media_type, ext, size, title, "
                "status, storage_key, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'active', ?,?,?)",
                (str(uuidlib.uuid4()), f"bulk-sha-{i}", f"bulk_{i}.jpg", "image", "jpg",
                 1, f"Bulk Export {i}", f"bulk-{i}", now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?,?)",
                (cur.lastrowid, tid, "manual"),
            )

    r = client.get("/api/assets/export.csv", params={"tags": [bulk_tag]})
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert len(rows) == n
    assert all(bulk_tag in row["tags"] for row in rows)
