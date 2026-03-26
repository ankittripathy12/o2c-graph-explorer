"""
/api/query  —  NL → Cypher → answer
Features:
  - Conversation memory  (per session_id)
  - Streaming response   (POST /api/query/stream)
  - Standard response    (POST /api/query/natural)
"""

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.database.neo4j_client import neo4j_client
from app.models.schemas import QueryRequest, QueryResponse
from app.services.conversation_memory import memory
from app.services.llm_service import LLMService

router = APIRouter()
logger = logging.getLogger(__name__)
llm    = LLMService()


async def _run_pipeline(req: QueryRequest):
    """
    Shared logic used by both /natural and /stream.
    Returns (answer, cypher, results, provider_used, is_off_topic)
    """

    if llm.is_off_topic(req.query):
        return (
            "This system is restricted to queries about "
            "the Order to Cash dataset only.",
            None, None, "none", True
        )


    session_id = req.session_id or "default"
    ctx_summary = memory.get_context_summary(session_id)
    enriched_query = req.query
    if ctx_summary:
        enriched_query = (
            f"Previous conversation context:\n{ctx_summary}\n\n"
            f"Current question: {req.query}"
        )

  
    cypher        = llm.get_prebuilt_cypher(req.query)
    provider_used = "none"


    if not cypher:
        cypher, provider_used = await llm.translate_to_cypher(
            enriched_query, provider=req.provider
        )

    if not cypher:
        return ("Sorry, I could not translate that question into a query.",
                None, None, provider_used, False)

    logger.info("Cypher: %s", cypher[:120])


    results = neo4j_client.query(cypher)

  
    answer, provider_used = await llm.generate_answer(
        req.query, cypher, results, provider=req.provider
    )

    memory.add(session_id, "user",      req.query)
    memory.add(session_id, "assistant", answer)

    return answer, cypher, results, provider_used, False

@router.post("/natural", response_model=QueryResponse)
async def natural_language_query(req: QueryRequest):
    try:
        answer, cypher, results, provider_used, is_off_topic = \
            await _run_pipeline(req)
        return QueryResponse(
            answer=answer,
            cypher=cypher,
            results=results,
            provider_used=provider_used,
            is_off_topic=is_off_topic,
        )
    except Exception as exc:
        logger.exception("Query error")
        raise HTTPException(status_code=500, detail=str(exc))

async def _stream_generator(req: QueryRequest) -> AsyncGenerator[str, None]:
    """
    Yields SSE-formatted chunks so the frontend can display
    tokens as they arrive instead of waiting for the full answer.

    SSE format:  data: <json>\n\n
    Event types:
      {"type": "status",   "text": "..."}   ← progress messages
      {"type": "token",    "text": "..."}   ← answer tokens (simulated)
      {"type": "cypher",   "text": "..."}   ← the Cypher used
      {"type": "done",     "results": [...]}← final results + end signal
      {"type": "error",    "text": "..."}   ← error
    """
    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    try:
        # guardrail
        if llm.is_off_topic(req.query):
            yield sse({"type": "token",
                       "text": "This system is restricted to queries about "
                               "the Order to Cash dataset only."})
            yield sse({"type": "done", "results": [], "cypher": None})
            return

        session_id  = req.session_id or "default"
        ctx_summary = memory.get_context_summary(session_id)

        yield sse({"type": "status", "text": "Analysing your question…"})
        await asyncio.sleep(0.05)

        cypher        = llm.get_prebuilt_cypher(req.query)
        provider_used = "none"

        if not cypher:
            yield sse({"type": "status", "text": "Generating Cypher query…"})
            enriched = req.query
            if ctx_summary:
                enriched = f"Context:\n{ctx_summary}\n\nQuestion: {req.query}"
            cypher, provider_used = await llm.translate_to_cypher(
                enriched, provider=req.provider
            )

        if not cypher:
            yield sse({"type": "token",  "text": "Could not generate a query."})
            yield sse({"type": "done",   "results": [], "cypher": None})
            return

        yield sse({"type": "cypher", "text": cypher})
        yield sse({"type": "status", "text": "Querying the graph…"})
        await asyncio.sleep(0.05)

        results = neo4j_client.query(cypher)

        yield sse({"type": "status", "text": "Generating answer…"})
        await asyncio.sleep(0.05)

        answer, provider_used = await llm.generate_answer(
            req.query, cypher, results, provider=req.provider
        )

        words = answer.split(" ")
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            yield sse({"type": "token", "text": chunk})
            await asyncio.sleep(0.025)  

        memory.add(session_id, "user",      req.query)
        memory.add(session_id, "assistant", answer)

        yield sse({
            "type":         "done",
            "results":      results,
            "cypher":       cypher,
            "provider_used": provider_used,
        })

    except Exception as exc:
        logger.exception("Stream error")
        yield sse({"type": "error", "text": str(exc)})


@router.post("/stream")
async def stream_query(req: QueryRequest):
    """
    Streaming version — returns Server-Sent Events.
    Frontend connects and receives tokens as they arrive.
    """
    return StreamingResponse(
        _stream_generator(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )

@router.delete("/memory/{session_id}")
async def clear_memory(session_id: str):
    """Clear conversation history for a session."""
    memory.clear(session_id)
    return {"message": f"Memory cleared for session {session_id}"}


@router.get("/memory/{session_id}")
async def get_memory(session_id: str):
    """Get conversation history for a session."""
    return memory.session_info(session_id)

@router.get("/provider")
async def get_provider_status():
    return llm.provider_status()


@router.get("/suggestions")
async def suggestions():
    return {
        "suggestions": [
            "Which products are associated with the highest number of billing documents?",
            "Trace the full flow of billing document 90504274",
            "Find sales orders that have been delivered but not billed",
            "Show the end-to-end chain: Sales Order → Delivery → Billing",
            "Show revenue by customer",
            "How many billing documents were cancelled?",
            "Summarise the delivery status breakdown",
            "Show payment summary",
            "Find orders billed but not yet paid",
            "Give me a dataset overview",
        ]
    }