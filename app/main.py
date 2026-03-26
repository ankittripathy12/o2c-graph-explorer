
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="SAP O2C Graph Explorer",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


from app.routers import data, graph, query

app.include_router(graph.router, prefix="/api/graph", tags=["graph"])
app.include_router(query.router, prefix="/api/query", tags=["query"])
app.include_router(data.router,  prefix="/api/data",  tags=["data"])


@app.on_event("startup")
async def startup():
    from app.database.neo4j_client import neo4j_client
    try:
        neo4j_client.connect()
        neo4j_client.create_constraints()
        logger.info("✅  Data store ready")
    except Exception as e:
        logger.warning("⚠️  Startup warning: %s", e)
    logger.info("✅  Application started")


@app.on_event("shutdown")
async def shutdown():
    from app.database.neo4j_client import neo4j_client
    neo4j_client.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


FRONTEND_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "frontend", "public")
)


@app.get("/data.js", include_in_schema=False)
async def serve_data_js():
    path = os.path.join(FRONTEND_DIR, "data.js")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="data.js not found")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="application/javascript")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    index = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index):
        return Response(content="Frontend not found", status_code=404)
    return FileResponse(index)