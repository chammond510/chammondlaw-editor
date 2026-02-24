import os
from collections import OrderedDict

from django.db import connections
from openai import OpenAI


EMBEDDING_MODEL = "text-embedding-3-small"


def _openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=api_key)


def _embedding_to_vector(embedding):
    return "[" + ",".join(str(x) for x in embedding) + "]"


def generate_query_embedding(text):
    client = _openai_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def suggest_case_law(text):
    embedding = generate_query_embedding(text)
    vector = _embedding_to_vector(embedding)

    semantic_sql = """
        SELECT h.document_id, h.legal_issue, h.rule, h.is_primary,
               d.case_name, d.citation, d.decision_date, d.court, d.precedential_status,
               d.cited_by_count,
               cv.status as validity_status,
               1 - (he.embedding <=> %s::vector) as similarity
        FROM holding_embeddings he
        JOIN holdings h ON h.id = he.holding_id
        JOIN documents d ON d.id = h.document_id
        LEFT JOIN citation_validity cv ON cv.document_id = d.id
        WHERE 1 - (he.embedding <=> %s::vector) > 0.55
        ORDER BY similarity DESC
        LIMIT 15;
    """

    keyword_sql = """
        SELECT d.id as document_id, d.case_name, d.citation, d.decision_date, d.court,
               d.precedential_status, d.cited_by_count,
               cv.status as validity_status,
               ts_rank(dt.search_vector, plainto_tsquery('english', %s)) as rank
        FROM document_texts dt
        JOIN documents d ON d.id = dt.document_id
        LEFT JOIN citation_validity cv ON cv.document_id = d.id
        WHERE dt.search_vector @@ plainto_tsquery('english', %s)
        ORDER BY rank DESC
        LIMIT 15;
    """

    merged = OrderedDict()

    with connections["biaedge"].cursor() as cursor:
        cursor.execute(semantic_sql, [vector, vector])
        semantic_rows = cursor.fetchall()

        for row in semantic_rows:
            doc_id = row[0]
            merged[doc_id] = {
                "document_id": doc_id,
                "legal_issue": row[1],
                "holding": row[2],
                "is_primary": row[3],
                "case_name": row[4],
                "citation": row[5],
                "decision_date": row[6].isoformat() if row[6] else None,
                "court": row[7],
                "precedential_status": row[8],
                "cited_by_count": row[9] or 0,
                "validity_status": row[10],
                "similarity": float(row[11] or 0),
                "semantic_score": float(row[11] or 0),
                "keyword_score": 0.0,
            }

        cursor.execute(keyword_sql, [text, text])
        keyword_rows = cursor.fetchall()

        for row in keyword_rows:
            doc_id = row[0]
            if doc_id not in merged:
                merged[doc_id] = {
                    "document_id": doc_id,
                    "legal_issue": "",
                    "holding": "",
                    "is_primary": False,
                    "case_name": row[1],
                    "citation": row[2],
                    "decision_date": row[3].isoformat() if row[3] else None,
                    "court": row[4],
                    "precedential_status": row[5],
                    "cited_by_count": row[6] or 0,
                    "validity_status": row[7],
                    "similarity": 0.0,
                    "semantic_score": 0.0,
                    "keyword_score": float(row[8] or 0),
                }
            else:
                merged[doc_id]["keyword_score"] = max(merged[doc_id]["keyword_score"], float(row[8] or 0))

    results = list(merged.values())
    for item in results:
        item["combined_score"] = (item["semantic_score"] * 0.75) + (item["keyword_score"] * 0.25)

    results.sort(key=lambda x: (x["combined_score"], x["cited_by_count"]), reverse=True)
    return results[:20]


def category_cases(category_id, page=1, page_size=20):
    offset = (page - 1) * page_size
    sql = """
        SELECT d.id, d.case_name, d.citation, d.decision_date, d.court,
               d.precedential_status, d.cited_by_count, d.summary,
               cv.status as validity_status
        FROM document_categories dc
        JOIN documents d ON d.id = dc.document_id
        LEFT JOIN citation_validity cv ON cv.document_id = d.id
        WHERE dc.category_id = %s
        ORDER BY d.cited_by_count DESC NULLS LAST
        LIMIT %s OFFSET %s;
    """
    with connections["biaedge"].cursor() as cursor:
        cursor.execute(sql, [category_id, page_size, offset])
        rows = cursor.fetchall()

    results = []
    for row in rows:
        results.append({
            "document_id": row[0],
            "case_name": row[1],
            "citation": row[2],
            "decision_date": row[3].isoformat() if row[3] else None,
            "court": row[4],
            "precedential_status": row[5],
            "cited_by_count": row[6] or 0,
            "summary": row[7] or "",
            "validity_status": row[8],
        })
    return results


def case_detail(doc_id):
    with connections["biaedge"].cursor() as cursor:
        cursor.execute(
            """
            SELECT d.id, d.case_name, d.citation, d.decision_date, d.court,
                   d.precedential_status, d.summary, d.cited_by_count
            FROM documents d
            WHERE d.id = %s
            """,
            [doc_id],
        )
        doc = cursor.fetchone()
        if not doc:
            return None

        cursor.execute(
            """
            SELECT status, positive_citations, negative_citations, overruling_citations, status_reason
            FROM citation_validity
            WHERE document_id = %s
            """,
            [doc_id],
        )
        validity = cursor.fetchone()

        cursor.execute(
            """
            SELECT legal_issue, rule, is_primary
            FROM holdings
            WHERE document_id = %s
            ORDER BY is_primary DESC, sequence ASC
            """,
            [doc_id],
        )
        holdings = cursor.fetchall()

        cursor.execute(
            """
            SELECT title, text, topic_code, is_primary
            FROM headnotes
            WHERE document_id = %s
            ORDER BY sequence ASC
            """,
            [doc_id],
        )
        headnotes = cursor.fetchall()

        cursor.execute(
            """
            SELECT full_text
            FROM document_texts
            WHERE document_id = %s
            """,
            [doc_id],
        )
        full_text = cursor.fetchone()

    return {
        "id": doc[0],
        "case_name": doc[1],
        "citation": doc[2],
        "decision_date": doc[3].isoformat() if doc[3] else None,
        "court": doc[4],
        "precedential_status": doc[5],
        "summary": doc[6],
        "cited_by_count": doc[7] or 0,
        "validity": {
            "status": validity[0] if validity else None,
            "positive_citations": validity[1] if validity else 0,
            "negative_citations": validity[2] if validity else 0,
            "overruling_citations": validity[3] if validity else 0,
            "status_reason": validity[4] if validity else "",
        },
        "holdings": [
            {"legal_issue": h[0], "rule": h[1], "is_primary": h[2]} for h in holdings
        ],
        "headnotes": [
            {"title": h[0], "text": h[1], "topic_code": h[2], "is_primary": h[3]} for h in headnotes
        ],
        "full_text": full_text[0] if full_text else "",
    }


def similar_cases(doc_id):
    sql = """
        SELECT DISTINCT ON (h2.document_id)
               h2.document_id, d.case_name, d.citation, d.decision_date,
               h2.legal_issue, h2.rule,
               1 - (he2.embedding <=> he1.embedding) as similarity
        FROM holding_embeddings he1
        JOIN holdings h1 ON h1.id = he1.holding_id
        JOIN holding_embeddings he2 ON he2.id != he1.id
        JOIN holdings h2 ON h2.id = he2.holding_id
        JOIN documents d ON d.id = h2.document_id
        WHERE h1.document_id = %s
          AND h1.is_primary = true
          AND h2.document_id != %s
          AND 1 - (he2.embedding <=> he1.embedding) > 0.6
        ORDER BY h2.document_id, similarity DESC
        LIMIT 10;
    """
    with connections["biaedge"].cursor() as cursor:
        cursor.execute(sql, [doc_id, doc_id])
        rows = cursor.fetchall()

    return [
        {
            "document_id": row[0],
            "case_name": row[1],
            "citation": row[2],
            "decision_date": row[3].isoformat() if row[3] else None,
            "legal_issue": row[4],
            "holding": row[5],
            "similarity": float(row[6] or 0),
        }
        for row in rows
    ]
