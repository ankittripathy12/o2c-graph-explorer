"""
/api/data  — ingestion trigger + stats
Works with both Neo4j and in-memory backends.
"""

import logging
from app.database.neo4j_client import neo4j_client
from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/ingest")
async def ingest():
    """Load all data — works with in-memory or Neo4j backend."""
    try:
        
        neo4j_client._loaded = False
        neo4j_client._load_all()
        counts = neo4j_client.node_counts()
        rels   = neo4j_client.relationship_counts()
        rel_count = sum(rels.values())
        return {
            "message": "Data loaded successfully",
            "nodes_created": counts,
            "relationships_created": rel_count,
        }
    except Exception as exc:
        logger.exception("Ingestion error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/stats")
async def stats():
    try:
        return {
            "nodes":         neo4j_client.node_counts(),
            "relationships": neo4j_client.relationship_counts(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/clear")
async def clear():
    try:
        neo4j_client.clear_all()
        return {"message": "Cleared"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))