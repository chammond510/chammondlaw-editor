import os
import re
from collections import Counter, OrderedDict

from django.db import connections


EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
RESEARCH_ANSWER_MODEL = os.environ.get("OPENAI_RESEARCH_MODEL", "gpt-4.1-mini")
_SEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "no",
    "not",
    "of",
    "on",
    "or",
    "such",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "to",
    "was",
    "were",
    "will",
    "with",
}
_LEGAL_KEY_TERMS = {
    "asylum",
    "withholding",
    "cat",
    "torture",
    "hardship",
    "waiver",
    "nexus",
    "persecution",
    "particular",
    "social",
    "group",
    "psg",
    "credibility",
    "corroboration",
    "inadmissibility",
    "removability",
    "cancellation",
    "deportation",
    "adjustment",
    "status",
    "uscis",
    "bia",
    "circuit",
    "precedent",
    "precedential",
    "overruled",
    "good",
    "law",
    "lpr",
    "nonlpr",
    "i130",
    "i485",
    "i589",
    "i601",
    "i601a",
    "i751",
    "n400",
    "ina",
    "usc",
    "cfr",
}
_LEGAL_PHRASES = (
    "particular social group",
    "well-founded fear",
    "unable or unwilling",
    "government unable or unwilling",
    "one central reason",
    "nexus to a protected ground",
    "clear probability",
    "more likely than not",
    "exceptional and extremely unusual hardship",
    "extreme hardship",
    "good moral character",
    "continuous physical presence",
    "changed circumstances",
    "firm resettlement",
    "past persecution",
    "future persecution",
    "material support",
    "crime involving moral turpitude",
    "particularly serious crime",
)
_VALIDITY_SCORE_BONUS = {
    "good_law": 0.08,
    "questioned": -0.05,
    "overruled": -0.35,
    "unknown": -0.02,
}
_CIRCUIT_HINT_PATTERNS = {
    "1st": (r"\b1st\s*circuit\b", r"\bfirst\s*circuit\b"),
    "2nd": (r"\b2nd\s*circuit\b", r"\bsecond\s*circuit\b"),
    "3rd": (r"\b3rd\s*circuit\b", r"\bthird\s*circuit\b"),
    "4th": (r"\b4th\s*circuit\b", r"\bfourth\s*circuit\b"),
    "5th": (r"\b5th\s*circuit\b", r"\bfifth\s*circuit\b"),
    "6th": (r"\b6th\s*circuit\b", r"\bsixth\s*circuit\b"),
    "7th": (r"\b7th\s*circuit\b", r"\bseventh\s*circuit\b"),
    "8th": (r"\b8th\s*circuit\b", r"\beighth\s*circuit\b"),
    "9th": (r"\b9th\s*circuit\b", r"\bninth\s*circuit\b"),
    "10th": (r"\b10th\s*circuit\b", r"\btenth\s*circuit\b"),
    "11th": (r"\b11th\s*circuit\b", r"\beleventh\s*circuit\b"),
    "dc": (r"\bd\.?\s*c\.?\s*circuit\b", r"\bdc\s*circuit\b"),
}
_CIRCUIT_LIKE_PATTERNS = {
    "1st": ("%1st%", "%first%"),
    "2nd": ("%2nd%", "%second%"),
    "3rd": ("%3rd%", "%third%"),
    "4th": ("%4th%", "%fourth%"),
    "5th": ("%5th%", "%fifth%"),
    "6th": ("%6th%", "%sixth%"),
    "7th": ("%7th%", "%seventh%"),
    "8th": ("%8th%", "%eighth%"),
    "9th": ("%9th%", "%ninth%"),
    "10th": ("%10th%", "%tenth%"),
    "11th": ("%11th%", "%eleventh%"),
    "dc": ("%d.c.%", "%dc circuit%", "%district of columbia%"),
}


def _openai_client(required=True):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        if required:
            raise ValueError("OPENAI_API_KEY is not configured")
        return None
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _embedding_to_vector(embedding):
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _normalize_ws(value):
    return " ".join((value or "").split()).strip()


def _extract_search_phrases(text, limit=6):
    lowered = (text or "").lower()
    phrases = []
    seen = set()

    for match in re.finditer(r'"([^"]{4,120})"', text or ""):
        phrase = _normalize_ws(match.group(1)).lower()
        if len(phrase.split()) < 2:
            continue
        if phrase in seen:
            continue
        seen.add(phrase)
        phrases.append(phrase)
        if len(phrases) >= limit:
            return phrases

    for regex in (
        r"\bina\s*§?\s*\d+[a-z0-9()\-]*\b",
        r"\b8\s*u\.?\s*s\.?\s*c\.?\s*§?\s*\d+[a-z0-9()\-]*\b",
        r"\b8\s*c\.?\s*f\.?\s*r\.?\s*§?\s*\d+(?:\.\d+)+(?:\([a-z0-9]+\))*\b",
    ):
        for match in re.finditer(regex, lowered):
            phrase = _normalize_ws(match.group(0))
            if phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
            if len(phrases) >= limit:
                return phrases

    for phrase in _LEGAL_PHRASES:
        if phrase in lowered and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)
            if len(phrases) >= limit:
                break

    return phrases


def _extract_keyword_terms(text, limit=14):
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]*", (text or "").lower())
    if not tokens:
        return []

    counts = Counter(tokens)
    first_pos = {}
    for idx, token in enumerate(tokens):
        if token not in first_pos:
            first_pos[token] = idx

    scored = []
    for token, count in counts.items():
        if token in _SEARCH_STOPWORDS:
            continue
        if len(token) < 3 and not any(ch.isdigit() for ch in token):
            continue

        score = 0.0
        if token in _LEGAL_KEY_TERMS:
            score += 3.0
        if any(ch.isdigit() for ch in token):
            score += 2.0
        if "-" in token or "_" in token:
            score += 1.0
        if len(token) >= 8:
            score += 0.8
        score += min(2.0, count * 0.35)
        score -= min(0.6, first_pos[token] * 0.002)
        scored.append((score, first_pos[token], token))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [token for _, _, token in scored[:limit]]


def _build_search_queries(text):
    terms = _extract_keyword_terms(text, limit=14)
    if not terms:
        fallback_tokens = re.findall(r"[a-z0-9][a-z0-9_-]*", (text or "").lower())[:8]
        terms = [t for t in fallback_tokens if len(t) >= 2]
    keyword_terms = terms[:12]
    keyword_query = " OR ".join(keyword_terms)

    phrases = _extract_search_phrases(text, limit=6)
    quoted_phrases = [
        '"' + phrase.replace('"', "") + '"'
        for phrase in phrases
        if len(phrase.split()) >= 2
    ]
    phrase_query = " OR ".join(quoted_phrases[:4] + keyword_terms[:6]) if quoted_phrases else keyword_query

    return keyword_query, (phrase_query or keyword_query), keyword_terms, phrases


def _infer_circuit_hint(text):
    lowered = (text or "").lower()
    for label, patterns in _CIRCUIT_HINT_PATTERNS.items():
        if any(re.search(pattern, lowered) for pattern in patterns):
            return label
    return None


def _circuit_filter_clause(circuit_hint):
    if not circuit_hint:
        return "", []

    circuit_patterns = list(_CIRCUIT_LIKE_PATTERNS.get(circuit_hint, []))
    if not circuit_patterns:
        return "", []

    allowed_patterns = circuit_patterns + ["%bia%", "%attorney general%", "%ag%"]
    where_parts = ["d.court ILIKE %s" for _ in allowed_patterns]
    return " AND (" + " OR ".join(where_parts) + ")", allowed_patterns


def _infer_category_ids(cursor, keyword_terms, phrases, limit=5):
    candidates = []
    for term in keyword_terms[:8]:
        if len(term) >= 4:
            candidates.append(term)
    for phrase in phrases[:4]:
        normalized = _normalize_ws(phrase).lower()
        if len(normalized) >= 6:
            candidates.append(normalized)

    deduped = []
    seen = set()
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
        if len(deduped) >= 6:
            break

    if not deduped:
        return []

    or_parts = []
    params = []
    for value in deduped:
        like_pattern = f"%{value}%"
        or_parts.extend(["lower(c.name) LIKE %s", "lower(c.slug) LIKE %s"])
        params.extend([like_pattern, like_pattern])

    sql = f"""
        SELECT c.id
        FROM categories c
        WHERE c.enabled = true
          AND ({' OR '.join(or_parts)})
        ORDER BY c.display_order ASC
        LIMIT %s;
    """
    params.append(limit)
    cursor.execute(sql, params)
    return [int(row[0]) for row in cursor.fetchall()]


def _fetch_category_matched_docs(cursor, doc_ids, category_ids):
    if not doc_ids or not category_ids:
        return set()

    doc_placeholders = ",".join(["%s"] * len(doc_ids))
    category_placeholders = ",".join(["%s"] * len(category_ids))
    sql = f"""
        SELECT DISTINCT dc.document_id
        FROM document_categories dc
        WHERE dc.document_id IN ({doc_placeholders})
          AND dc.category_id IN ({category_placeholders});
    """
    cursor.execute(sql, [*doc_ids, *category_ids])
    return {int(row[0]) for row in cursor.fetchall()}


def _validity_bonus(status):
    key = (status or "unknown").lower()
    return _VALIDITY_SCORE_BONUS.get(key, _VALIDITY_SCORE_BONUS["unknown"])


def _court_matches_hint(court_value, circuit_hint):
    if not circuit_hint:
        return False
    lowered = (court_value or "").lower()
    patterns = _CIRCUIT_LIKE_PATTERNS.get(circuit_hint, ())
    for pattern in patterns:
        probe = pattern.replace("%", "").strip()
        if probe and probe in lowered:
            return True
    return False


def generate_query_embedding(text):
    client = _openai_client(required=True)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def suggest_case_law(text):
    text = (text or "").strip()
    if not text:
        return []
    keyword_query, phrase_query, keyword_terms, phrases = _build_search_queries(text)
    if not keyword_query:
        return []

    circuit_hint = _infer_circuit_hint(text)
    circuit_filter_sql, circuit_filter_params = _circuit_filter_clause(circuit_hint)

    vector = None
    try:
        embedding = generate_query_embedding(text)
        vector = _embedding_to_vector(embedding)
    except Exception:
        # Fallback to keyword-only search when embeddings are unavailable.
        vector = None

    def _run_retrieval(cursor, use_circuit_filter):
        active_filter_sql = circuit_filter_sql if use_circuit_filter else ""
        active_filter_params = circuit_filter_params if use_circuit_filter else []
        semantic_sql = f"""
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
            {active_filter_sql}
            ORDER BY similarity DESC
            LIMIT 20;
        """

        keyword_sql = f"""
            SELECT d.id as document_id, d.case_name, d.citation, d.decision_date, d.court,
                   d.precedential_status, d.cited_by_count,
                   cv.status as validity_status,
                   dt.tsv_rank, dt.keyword_rank, dt.phrase_rank
            FROM (
                SELECT dt.document_id,
                       ts_rank(dt.search_vector, websearch_to_tsquery('english', %s)) as keyword_rank,
                       ts_rank(dt.search_vector, websearch_to_tsquery('english', %s)) as phrase_rank,
                       (
                           ts_rank(dt.search_vector, websearch_to_tsquery('english', %s)) * 0.55 +
                           ts_rank(dt.search_vector, websearch_to_tsquery('english', %s)) * 0.45
                       ) as tsv_rank
                FROM document_texts dt
                JOIN documents d ON d.id = dt.document_id
                WHERE (
                    dt.search_vector @@ websearch_to_tsquery('english', %s)
                    OR dt.search_vector @@ websearch_to_tsquery('english', %s)
                )
                {active_filter_sql}
                ORDER BY tsv_rank DESC
                LIMIT 30
            ) dt
            JOIN documents d ON d.id = dt.document_id
            LEFT JOIN citation_validity cv ON cv.document_id = d.id
            ORDER BY dt.tsv_rank DESC
            LIMIT 25;
        """

        merged = OrderedDict()
        if vector:
            cursor.execute(semantic_sql, [vector, vector, *active_filter_params])
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
                    "phrase_score": 0.0,
                    "circuit_filtered": use_circuit_filter,
                }

        keyword_params = [
            keyword_query,
            phrase_query,
            keyword_query,
            phrase_query,
            keyword_query,
            phrase_query,
            *active_filter_params,
        ]
        cursor.execute(keyword_sql, keyword_params)
        keyword_rows = cursor.fetchall()

        for row in keyword_rows:
            doc_id = row[0]
            keyword_score = float(row[8] or 0)
            phrase_score = float(row[10] or 0)
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
                    "keyword_score": keyword_score,
                    "phrase_score": phrase_score,
                    "circuit_filtered": use_circuit_filter,
                }
            else:
                merged[doc_id]["keyword_score"] = max(
                    merged[doc_id]["keyword_score"], keyword_score
                )
                merged[doc_id]["phrase_score"] = max(
                    merged[doc_id]["phrase_score"], phrase_score
                )

        return merged

    with connections["biaedge"].cursor() as cursor:
        inferred_category_ids = _infer_category_ids(cursor, keyword_terms, phrases, limit=5)
        merged = _run_retrieval(cursor, use_circuit_filter=bool(circuit_hint))
        if circuit_hint and not merged:
            # Fallback to unconstrained retrieval if strict circuit filtering yields nothing.
            merged = _run_retrieval(cursor, use_circuit_filter=False)

        category_matched_docs = _fetch_category_matched_docs(
            cursor,
            list(merged.keys()),
            inferred_category_ids,
        )

    results = list(merged.values())
    for item in results:
        if item["semantic_score"] > 0 and item["keyword_score"] > 0:
            base_score = (item["semantic_score"] * 0.65) + (item["keyword_score"] * 0.35)
        else:
            base_score = max(item["semantic_score"], item["keyword_score"])

        phrase_boost = min(0.18, item.get("phrase_score", 0.0) * 0.20)
        validity_boost = _validity_bonus(item.get("validity_status"))
        category_boost = 0.10 if item["document_id"] in category_matched_docs else 0.0
        circuit_boost = 0.0
        if circuit_hint:
            if _court_matches_hint(item.get("court"), circuit_hint):
                circuit_boost = 0.08
            elif item.get("court") and item["circuit_filtered"]:
                circuit_boost = -0.06

        item["combined_score"] = (
            base_score + phrase_boost + validity_boost + category_boost + circuit_boost
        )
        item["category_match"] = item["document_id"] in category_matched_docs
        item["circuit_hint"] = circuit_hint
        item["validity_adjustment"] = validity_boost

        if item.get("validity_status") == "overruled":
            item["combined_score"] -= 0.15

    results.sort(
        key=lambda x: (
            x["combined_score"],
            x.get("phrase_score", 0.0),
            x.get("cited_by_count", 0),
        ),
        reverse=True,
    )
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
