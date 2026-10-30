"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tag, status, dates)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar.
"""
from __future__ import annotations

from typing import Optional

from . import ai
from .db import get_conn

SORTS = {
    "newest": "a.created_at DESC",
    "oldest": "a.created_at ASC",
    "name": "a.title ASC",
    "largest": "a.size DESC",
}


def _fts_query(q: str) -> Optional[str]:
    """Turn user text into a safe FTS5 prefix query.

    Returns None when the input has no searchable characters (e.g. only
    punctuation, whitespace, or FTS stopwords), so callers can skip the
    MATCH clause entirely instead of sending an empty string to SQLite.
    """
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    if not terms:
        return None
    return " ".join(f'"{t}"*' for t in terms)


def _normalize_tags(raw_tags: Optional[list[str]]) -> list[str]:
    """Strip, lowercase, dedupe, and drop empty strings from a tag list."""
    if not raw_tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in raw_tags:
        t2 = (t or "").strip().lower()
        if t2 and t2 not in seen:
            seen.add(t2)
            out.append(t2)
    return out


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,          # deprecated, use tags
    tags: Optional[list[str]] = None,   # include: AND semantics
    exclude_tags: Optional[list[str]] = None,  # exclude: any hit = excluded
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    conn = get_conn()

    # Semantic mode ranks the candidate pool by embedding similarity.
    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    # Normalise tag inputs; reconcile old single `tag` with new `tags` list.
    include = _normalize_tags(tags)
    if tag:
        tl = tag.strip().lower()
        if tl and tl not in include:
            include.append(tl)
    exclude = _normalize_tags(exclude_tags)

    # Short-circuit: same tag on both sides -> empty result set.
    overlap = set(include) & set(exclude)
    if overlap:
        return {"total": 0, "items": [], "facets": _facets(status), "mode": mode}

    where = ["a.status = ?"]
    params: list = [status]
    join = ""

    fts_q = _fts_query(q)
    if fts_q is not None:
        join += " JOIN asset_fts f ON f.rowid = a.id"
        where.append("asset_fts MATCH ?")
        params.append(fts_q)
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)

    # Include tags: AND semantics via EXISTS subquery per tag.
    for tname in include:
        where.append(
            "EXISTS (SELECT 1 FROM asset_tags at"
            " JOIN tags t ON t.id = at.tag_id"
            " WHERE at.asset_id = a.id AND t.name = ?)"
        )
        params.append(tname)

    # Exclude tags: any hit excludes the asset (NOT EXISTS each).
    for tname in exclude:
        where.append(
            "NOT EXISTS (SELECT 1 FROM asset_tags at"
            " JOIN tags t ON t.id = at.tag_id"
            " WHERE at.asset_id = a.id AND t.name = ?)"
        )
        params.append(tname)

    if collection_id:
        join += " JOIN collection_assets ca ON ca.asset_id = a.id AND ca.collection_id = ?"
        params.append(collection_id)

    where_sql = " AND ".join(where)
    order = "bm25(asset_fts)" if fts_q is not None else SORTS.get(sort, SORTS["newest"])

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a{join} WHERE {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT a.* FROM assets a{join} WHERE {where_sql}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    return {
        "total": total,
        "items": [_card(dict(r)) for r in rows],
        "facets": _facets(status),
        "mode": mode,
    }


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {"total": len(scored), "items": items, "facets": _facets(status), "mode": "semantic"}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
