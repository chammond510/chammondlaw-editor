from .exemplar_service import cosine_similarity, generate_embedding


def serialize_client_file(client_file):
    text = client_file.extracted_text or ""
    metadata = client_file.metadata or {}
    return {
        "id": client_file.id,
        "title": client_file.title,
        "file_url": client_file.original_file.url if client_file.original_file else "",
        "filename": metadata.get("filename") or "",
        "extension": metadata.get("extension") or "",
        "char_count": len(text),
        "snippet": text[:500],
        "updated_at": client_file.updated_at.isoformat(),
    }


def rank_client_files(query, client_files):
    query = (query or "").strip()
    if not query:
        for item in client_files:
            item["score"] = 0.0
        return client_files

    query_embedding = generate_embedding(query)
    lowered = query.lower()

    for item in client_files:
        score = 0.0
        if query_embedding and item.get("embedding"):
            score += cosine_similarity(query_embedding, item.get("embedding") or [])
        title = (item.get("title") or "").lower()
        text = (item.get("extracted_text") or "").lower()
        if lowered in title:
            score += 0.25
        if lowered in text:
            score += 0.15
        item["score"] = score

    client_files.sort(key=lambda item: (item.get("score") or 0.0, item.get("updated_at") or ""), reverse=True)
    return client_files
