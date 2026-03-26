"""
/api/graph  — serves graph data from in-memory store
"""

import logging
from fastapi import APIRouter, HTTPException, Query
from app.database.neo4j_client import neo4j_client

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/data")
async def get_graph_data(limit: int = Query(default=150, le=500)):
    """Return nodes + relationships for the D3 force graph."""
    try:
        return neo4j_client.get_graph_sample(limit)
    except Exception as exc:
        logger.exception("graph/data error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/node/{node_id}")
async def get_node(node_id: str):
    """Return a single node and its immediate neighbours."""
    try:
        detail = neo4j_client.get_node_detail(node_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Node not found")
        return detail
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("graph/node error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/search")
async def search_nodes(
    q: str = Query(..., min_length=1),
    label: str = Query(default=None),
):
    """Search nodes by id, name, or description."""
    try:
        neo4j_client._load_all()
        ql = q.lower()
        graph = neo4j_client.get_graph_sample(500)
        results = []
        for node in graph["nodes"]:
            
            match = any(
                ql in str(v).lower()
                for v in node.values()
                if isinstance(v, (str, int, float))
            )
            if match:
                if label is None or node.get("_label") == label:
                    results.append({
                        "node":  node,
                        "label": node.get("_label", "")
                    })
        return results[:30]
    except Exception as exc:
        logger.exception("graph/search error")
        raise HTTPException(status_code=500, detail=str(exc))