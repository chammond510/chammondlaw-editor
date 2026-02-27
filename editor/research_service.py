import os
from collections import OrderedDict

from django.db import connections
from openai import OpenAI


EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
RESEARCH_ANSWER_MODEL = os.environ.get("OPENAI_RESEARCH_MODEL", "gpt-4.1-mini")


def _openai_client(required=True):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        if required:
            raise ValueError("OPENAI_API_KEY is not configured")
        return None
    return OpenAI(api_key=api_key)


def _embedding_to_vector(embedding):
    return "[" + ",".join(str(x) for x in embedding) + "]"


def generate_query_embedding(text):
    client = _openai_client(required=True)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def suggest_case_law(text):
    text = (text or "").strip()
    if not text:
        return []

    vector = None
    try:
        embedding = generate_query_embedding(text)
        vector = _embedding_to_vector(embedding)
    except Exception:
        # Fallback to keyword-only search when embeddings are unavailable.
        vector = None

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
               dt.tsv_rank
        FROM (
            SELECT document_id, ts_rank(search_vector, plainto_tsquery('english', %s)) as tsv_rank
            FROM document_texts
            WHERE search_vector @@ plainto_tsquery('english', %s)
            ORDER BY tsv_rank DESC
            LIMIT 15
        ) dt
        JOIN documents d ON d.id = dt.document_id
        LEFT JOIN citation_validity cv ON cv.document_id = d.id
        ORDER BY dt.tsv_rank DESC;
    """

    merged = OrderedDict()

    with connections["biaedge"].cursor() as cursor:
        if vector:
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
                merged[doc_id]["keyword_score"] = max(
                    merged[doc_id]["keyword_score"], float(row[8] or 0)
                )

    results = list(merged.values())
    for item in results:
        if item["semantic_score"] > 0 and item["keyword_score"] > 0:
            item["combined_score"] = (item["semantic_score"] * 0.75) + (
                item["keyword_score"] * 0.25
            )
        else:
            item["combined_score"] = max(item["semantic_score"], item["keyword_score"])

    results.sort(key=lambda x: (x["combined_score"], x["cited_by_count"]), reverse=True)
    return results[:20]


def ask_question(question, limit=8):
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")

    suggested = suggest_case_law(question)[:limit]
    citations = [
        {
            "document_id": item["document_id"],
            "case_name": item["case_name"],
            "citation": item["citation"],
            "court": item["court"],
            "decision_date": item["decision_date"],
            "validity_status": item.get("validity_status"),
            "holding": item.get("holding") or "",
            "legal_issue": item.get("legal_issue") or "",
        }
        for item in suggested
    ]

    if not citations:
        return {
            "answer": (
                "No authorities were returned for this query. Try adding more "
                "facts, legal standards, or specific issues."
            ),
            "citations": [],
        }

    context_lines = []
    for idx, item in enumerate(citations, 1):
        year = ""
        if item["decision_date"]:
            year = item["decision_date"][:4]
        cite = f"{item['case_name']}, {item['citation'] or 'No citation'}"
        if item["court"] or year:
            court_year = " ".join(part for part in [item.get("court"), year] if part)
            cite += f" ({court_year})"
        context_lines.append(
            "\n".join(
                [
                    f"[{idx}] {cite}",
                    f"Validity: {item.get('validity_status') or 'unknown'}",
                    f"Issue: {item.get('legal_issue') or 'N/A'}",
                    f"Holding: {item.get('holding') or 'N/A'}",
                ]
            )
        )

    system_prompt = (
        "You are an immigration legal research assistant. Answer with concise, "
        "cautious legal analysis. Do not invent authorities. If the provided cases "
        "are weak or conflicting, say so explicitly."
    )
    user_prompt = (
        f"Question:\n{question}\n\n"
        "Authorities:\n"
        + "\n\n".join(context_lines)
        + "\n\n"
        "Provide: (1) short answer, (2) key rules, (3) caveats/risks. "
        "Reference authorities by bracket number [1], [2], etc."
    )

    client = _openai_client(required=False)
    if not client:
        return {
            "answer": _fallback_answer_without_llm(citations),
            "citations": citations,
        }

    try:
        resp = client.responses.create(
            model=RESEARCH_ANSWER_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_output_tokens=800,
        )
        answer = (getattr(resp, "output_text", "") or "").strip()
    except Exception:
        answer = ""

    if not answer:
        answer = _fallback_answer_without_llm(citations)

    return {"answer": answer, "citations": citations}


def _fallback_answer_without_llm(citations):
    top = citations[:5]
    lines = [
        "OPENAI_API_KEY is not configured, so this is a citation shortlist instead of an AI synthesis.",
        "",
        "Most relevant authorities found:",
    ]
    for idx, item in enumerate(top, 1):
        year = item["decision_date"][:4] if item.get("decision_date") else ""
        court = item.get("court") or ""
        parenthetical = f" ({court} {year})".strip() if court or year else ""
        lines.append(
            f"{idx}. {item.get('case_name') or 'Unknown case'}, "
            f"{item.get('citation') or 'No citation'}{parenthetical}"
        )
    return "\n".join(lines)


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
        results.append(
            {
                "document_id": row[0],
                "case_name": row[1],
                "citation": row[2],
                "decision_date": row[3].isoformat() if row[3] else None,
                "court": row[4],
                "precedential_status": row[5],
                "cited_by_count": row[6] or 0,
                "summary": row[7] or "",
                "validity_status": row[8],
            }
        )
    return results


def immcite_status(doc_id):
    with connections["biaedge"].cursor() as cursor:
        cursor.execute(
            """
            SELECT status, positive_citations, negative_citations,
                   overruling_citations, status_reason
            FROM citation_validity
            WHERE document_id = %s
            """,
            [doc_id],
        )
        row = cursor.fetchone()

    if not row:
        return {
            "document_id": doc_id,
            "status": None,
            "positive_citations": 0,
            "negative_citations": 0,
            "overruling_citations": 0,
            "status_reason": "",
        }

    return {
        "document_id": doc_id,
        "status": row[0],
        "positive_citations": row[1] or 0,
        "negative_citations": row[2] or 0,
        "overruling_citations": row[3] or 0,
        "status_reason": row[4] or "",
    }


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
            {"title": h[0], "text": h[1], "topic_code": h[2], "is_primary": h[3]}
            for h in headnotes
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
