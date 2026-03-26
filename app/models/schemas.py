"""
Pydantic schemas used by the FastAPI routers.
"""

from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class QueryRequest(BaseModel):
    query:    str
    provider: Optional[str] = None   
    session_id: Optional[str] = None


class QueryResponse(BaseModel):
    answer:       str
    cypher:       Optional[str] = None
    results:      Optional[List[Dict[str, Any]]] = None
    is_off_topic: bool = False
    provider_used: Optional[str] = None   


class GraphNode(BaseModel):
    id: str
    label: str
    properties: Dict[str, Any]


class GraphEdge(BaseModel):
    source: str
    target: str
    relationship: str
    properties: Optional[Dict[str, Any]] = None


class GraphResponse(BaseModel):
    nodes: List[Dict[str, Any]]
    relationships: List[Dict[str, Any]]


class IngestResponse(BaseModel):
    message: str
    nodes_created: Dict[str, int]
    relationships_created: int


class StatsResponse(BaseModel):
    nodes: Dict[str, int]
    relationships: Dict[str, int]
