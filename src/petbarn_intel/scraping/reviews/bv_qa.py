"""Bazaarvoice Q&A ("questions.json") for one product. Same BFD host/auth
pattern as reviews; verified live returning a well-formed empty result for
a product with no questions, so absence is a legitimate outcome, not a
failure.
"""

from __future__ import annotations

import orjson

from petbarn_intel.logging import get_logger
from petbarn_intel.models.qa import Question
from petbarn_intel.scraping.discovery.bazaarvoice_config import (
    BazaarvoiceConfigError,
    get_display_code,
)
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

QA_URL = (
    "https://apps.bazaarvoice.com/bfd/v1/clients/petbarn-au/api-products/cv2/"
    "resources/data/questions.json"
)


def _headers(display_code: str) -> dict:
    return {
        "Origin": "https://www.petbarn.com.au",
        "Referer": "https://www.petbarn.com.au/",
        "bv-bfd-token": f"{display_code},main_site,en_AU",
        "Accept": "application/json; version=1",
    }


async def fetch_questions(
    fetcher: TieredFetcher, sku: str, product_id: str, limit: int = 20, retried: bool = False,
) -> list[Question]:
    try:
        display_code = await get_display_code(fetcher)
    except BazaarvoiceConfigError as exc:
        logger.error("bv_config_unavailable", sku=sku, error=str(exc))
        return []

    params = {
        "apiversion": "5.5",
        "displaycode": f"{display_code}-en_au",
        "resource": "questions",
        "action": "QUESTIONS_N_ANSWERS",
        "filter": f"productid:eq:{sku}",
        "filter_questions": "contentlocale:eq:en_AU",
        "stats": "answers",
        "limit": str(limit),
        "offset": "0",
    }
    result = await fetcher.fetch(
        QA_URL, resource_type="reviews", params=params, headers=_headers(display_code),
        respect_robots=False,
    )
    if result.status_code in (401, 403) and not retried:
        await get_display_code(fetcher, force_refresh=True)  # side effect: refreshes the cached config
        return await fetch_questions(fetcher, sku, product_id, limit, retried=True)
    if not result.ok():
        logger.warning("bv_qa_fetch_failed", sku=sku, reason=result.block_reason)
        return []
    payload = orjson.loads(result.content)
    response = payload.get("response", payload)
    if response.get("HasErrors"):
        logger.warning("bv_qa_api_errors", sku=sku, errors=response.get("Errors"))
        return []

    includes = response.get("Includes", {}) or {}
    answers_by_question: dict[str, list[str]] = {}
    for ans in (includes.get("Answers") or {}).values():
        qid = ans.get("QuestionId")
        if qid and ans.get("AnswerText"):
            answers_by_question.setdefault(qid, []).append(ans["AnswerText"])

    questions = []
    for raw in response.get("Results", []):
        qid = str(raw.get("Id"))
        questions.append(
            Question(
                question_id=qid,
                product_id=product_id,
                question_text=raw.get("QuestionSummary") or raw.get("QuestionDetails") or "",
                answer_texts=answers_by_question.get(qid, []),
                submitted_at=raw.get("SubmissionTime"),
            )
        )
    return questions
