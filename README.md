# SAP O2C Graph Explorer

A context graph system with an LLM-powered conversational query interface for exploring SAP Order-to-Cash data.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Graph Model](#graph-model)
- [Database Choice](#database-choice)
- [LLM Integration & Prompting Strategy](#llm-integration--prompting-strategy)
- [Guardrails](#guardrails)
- [Project Structure](#project-structure)
- [Setup & Running Locally](#setup--running-locally)
- [Environment Variables](#environment-variables)
- [API Endpoints](#api-endpoints)
- [Example Queries](#example-queries)
- [Bonus Features Implemented](#bonus-features-implemented)

---

## Overview

This system ingests a SAP O2C (Order-to-Cash) dataset and unifies it into a property graph. Users can:

- **Visually explore** the graph — expand nodes, inspect metadata, trace relationships
- **Ask natural language questions** — the system translates these into Cypher queries, executes them, and returns data-backed answers
- **Follow the full O2C chain** — from Sales Order through Delivery, Billing, Journal Entry, and Payment

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                      Frontend                       │
│   Graph Visualiser (vis.js) + Chat Interface        │
│         Served as static HTML from FastAPI          │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP / SSE
┌──────────────────────▼──────────────────────────────┐
│                  FastAPI Backend                    │
│                                                     │
│  /api/graph   →  Graph sample & node detail         │
│  /api/query   →  NL → Cypher → Answer pipeline      │
│  /api/data    →  Ingestion & stats endpoints        │
└────────────┬──────────────────────┬─────────────────┘
             │                      │
┌────────────▼──────────┐  ┌────────▼────────────────┐
│     Neo4j (primary)   │  │  InMemory Fallback       │
│  bolt://...  or Aura  │  │  Pure-Python, no DB      │
│                       │  │  needed to run locally   │
└───────────────────────┘  └─────────────────────────┘
             │
┌────────────▼──────────────────────────────────────┐
│               Groq API (LLaMA 3.3 70B)            │
│   NL → Cypher translation + Answer generation     │
└───────────────────────────────────────────────────┘
```

### Key Design Decisions

**Smart DB fallback:** The app attempts to connect to Neo4j on startup. If Neo4j is unavailable (missing credentials, no driver installed, unreachable host), it silently falls back to a pure-Python `InMemoryClient` that reads the JSONL files directly. This means the app works out of the box without any external database.

**Two-stage LLM pipeline:** Natural language queries go through two separate LLM calls — one to generate Cypher, one to generate a human-readable answer from the query results. This separation keeps prompts focused and results grounded in real data.

**Pre-built Cypher fast path:** For the most common queries (top products, trace billing doc, incomplete flows, revenue by customer, etc.), handcrafted Cypher is served directly without an LLM call. The LLM is only invoked for novel queries, reducing latency and token usage.

**Streaming via SSE:** The `/api/query/stream` endpoint uses Server-Sent Events to stream status updates and answer tokens to the frontend in real time, giving a typewriter effect without waiting for the full response.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI (Python 3.10+) |
| Graph database | Neo4j (with InMemory fallback) |
| LLM provider | Groq — `llama-3.3-70b-versatile` (free tier) |
| HTTP client | httpx (async) |
| Frontend | Vanilla HTML/JS with vis.js (Network) |
| Data format | JSONL (SAP O2C export) |

---

## Graph Model

### Nodes

| Label | Key Properties |
|---|---|
| `Customer` | id, name, category, isBlocked |
| `SalesOrder` | id, type, totalNetAmount, currency, overallDeliveryStatus, overallBillingStatus |
| `SalesOrderItem` | id, salesOrder, material, quantity, netAmount, productionPlant |
| `Delivery` | id, deliveryDocument, overallGoodsMovementStatus, shippingPoint, creationDate |
| `BillingDocument` | id, type, totalNetAmount, isCancelled, accountingDocument, billingDate |
| `JournalEntry` | id, accountingDocument, glAccount, amount, currency, postingDate |
| `Payment` | id, accountingDocument, amount, currency, clearingDate |
| `Product` | id, description |
| `Plant` | id, name, salesOrganization |

### Relationships (the O2C chain)

```
(Customer)        <-[:PLACED_BY]-         (SalesOrder)
(SalesOrder)      -[:HAS_ITEM]->          (SalesOrderItem)
(SalesOrderItem)  -[:REFERENCES_PRODUCT]->(Product)
(SalesOrderItem)  -[:SHIPS_FROM]->        (Plant)
(SalesOrder)      -[:HAS_DELIVERY]->      (Delivery)
(Delivery)        -[:FULFILLED_BY]->      (BillingDocument)
(BillingDocument) -[:BILLED_TO]->         (Customer)
(BillingDocument) -[:POSTS_TO_JOURNAL]->  (JournalEntry)
(BillingDocument) -[:SETTLED_BY]->        (Payment)
```

### Key Linkages Discovered from the Data

- `outbound_delivery_items.referenceSdDocument` → links Delivery back to SalesOrder (`HAS_DELIVERY`)
- `billing_document_items.referenceSdDocument` → links BillingDocument to Delivery (`FULFILLED_BY`)
- `billing_document_headers.accountingDocument` = `journal_entry_items.accountingDocument` → links billing to journal/payment

---

## Database Choice

**Neo4j** was chosen as the primary store because:

1. The O2C data is inherently relational — tracing a document through 5 hops (SO → Item → Delivery → Billing → Payment) is a natural graph traversal, not a join-heavy SQL query.
2. Cypher is readable and maps cleanly to the business domain (e.g. `MATCH (so:SalesOrder)-[:HAS_DELIVERY]->(d)-[:FULFILLED_BY]->(b:BillingDocument)`).
3. Neo4j Aura offers a free cloud tier suitable for this dataset size.

**InMemory fallback** is provided so reviewers can run the app locally without spinning up a database. It reads the same JSONL files and implements the same query patterns in Python dictionaries.

**Trade-off:** The InMemory client supports only a fixed set of pre-defined query patterns. Novel LLM-generated Cypher that doesn't match those patterns silently falls back to summary stats. For production use, Neo4j is required for full dynamic query support.

---

## LLM Integration & Prompting Strategy

### Provider

Groq (free tier) running `llama-3.3-70b-versatile`. Configured via `GROQ_API_KEY` in `.env`.

### Two-Stage Pipeline

**Stage 1 — NL to Cypher**

The prompt includes the full graph schema (node labels, properties, relationship types) and strict rules:
- Use only schema-defined labels and relationships
- Read-only queries (`MATCH … RETURN` only, no mutations)
- Aggregations use `COUNT(DISTINCT …)`
- Default `LIMIT 20`
- Return raw Cypher only (no markdown fences)

```
Convert the following natural language question into a valid Neo4j Cypher query.
Graph schema: <schema>
Rules: <rules>
Question: {query}
Cypher:
```

**Stage 2 — Answer Generation**

The original question, the Cypher used, and up to 20 result rows are passed together. The LLM is asked to write a concise, data-backed natural language answer using markdown formatting (bold numbers, tables for tabular results).

**Conversation context injection:** The last 3 turns of the session are prepended to Stage 1 prompts so follow-up questions ("show me more for that customer") resolve correctly.

**Temperature:** 0.2 — low temperature keeps Cypher generation deterministic while still allowing natural answer phrasing.

---

## Guardrails

Two layers of guardrails prevent off-topic usage:

### 1. Keyword blocklist (fast, pre-LLM)

A list of off-topic keywords is checked before any LLM call. If matched, the system returns immediately without consuming any API tokens:

```python
OFF_TOPIC_KEYWORDS = [
    "weather", "movie", "film", "recipe", "sport", "cricket",
    "football", "politics", "news", "joke", "poem", "capital of",
    "who is the president", "stock price", "bitcoin", "who invented",
    "tell me about yourself", "write a story", "translate", ...
]
```

### 2. System prompt restriction (LLM-level)

The system prompt passed to every LLM call instructs the model to stay within domain:

> *"You are a data analyst assistant embedded in an SAP Order-to-Cash graph exploration tool. You ONLY answer questions about this O2C dataset. For anything unrelated respond exactly: 'This system is restricted to queries about the Order to Cash dataset only.'"*

### Response for blocked queries

```
"This system is restricted to queries about the Order to Cash dataset only."
```

---

## Project Structure

```
o2c-graph-explorer/
├── app/
│   ├── main.py                  # FastAPI app, CORS, startup/shutdown
│   ├── models/
│   │   └── schemas.py           # Pydantic request/response models
│   ├── routers/
│   │   ├── graph.py             # /api/graph — graph sample, node detail, stats
│   │   ├── query.py             # /api/query — NL pipeline, streaming, memory
│   │   └── data.py              # /api/data — ingestion trigger
│   ├── services/
│   │   ├── llm_service.py       # Groq client, NL→Cypher, answer generation, guardrails
│   │   ├── ingestion.py         # JSONL → Neo4j MERGE (idempotent)
│   │   ├── conversation_memory.py  # Per-session chat history with TTL
│   │   └── clustering.py        # Community detection for node colouring
│   └── database/
│       └── neo4j_client.py      # Neo4jClient + InMemoryClient + smart factory
├── frontend/
│   └── public/
│       ├── index.html           # Graph visualiser + chat UI (vis.js)
│       └── data.js              # Pre-computed graph snapshot (optional)
├── scripts/
│   └── seed.py                  # CLI ingestion script
├── data/
│   └── sap-o2c-data/            # JSONL dataset files
│       ├── sales_order_headers/
│       ├── sales_order_items/
│       ├── outbound_delivery_headers/
│       ├── outbound_delivery_items/
│       ├── billing_document_headers/
│       ├── billing_document_items/
│       ├── billing_document_cancellations/
│       ├── payments_accounts_receivable/
│       ├── journal_entry_items_accounts_receivable/
│       ├── business_partners/
│       ├── products/
│       ├── product_descriptions/
│       ├── plants/
│       └── ...
└── .env                         # (create this — see below)
```

---

## Setup & Running Locally

### Prerequisites

- Python 3.10+
- A free [Groq API key](https://console.groq.com) (takes ~30 seconds to get)
- (Optional) A [Neo4j Aura Free](https://neo4j.com/cloud/aura/) instance

### 1. Clone and install

```bash
git clone <your-repo-url>
pip install -r requirements.txt
```

If you don't have a `requirements.txt`, install manually:

```bash
pip install fastapi uvicorn httpx python-dotenv pydantic
pip install neo4j  
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
# Required
GROQ_API_KEY=your_groq_api_key_here

# Optional — omit to use the InMemory fallback
NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password
```

### 3. (Optional) Seed Neo4j

If you have Neo4j configured, populate it with:

```bash
python scripts/seed.py
```

This is idempotent — safe to run multiple times.

### 4. Run the server

```bash
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` in your browser.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Free key from [console.groq.com](https://console.groq.com) |
| `NEO4J_URI` | No | `bolt://localhost:7687` or `neo4j+s://xxxx.databases.neo4j.io` |
| `NEO4J_USER` | No | Default: `neo4j` |
| `NEO4J_PASSWORD` | No | Your Neo4j password |
| `NEO4J_DATABASE` | No | Override DB name (auto-detected from Aura URI) |
| `DATA_DIR` | No | Path to JSONL data folder. Default: `data/sap-o2c-data` |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/api/graph/sample` | Graph nodes and edges for visualisation |
| `GET` | `/api/graph/node/{id}` | Single node detail with neighbours |
| `GET` | `/api/graph/stats` | Node and relationship counts |
| `POST` | `/api/query/natural` | NL query → Cypher → answer (standard) |
| `POST` | `/api/query/stream` | NL query → Cypher → answer (SSE streaming) |
| `GET` | `/api/query/suggestions` | Pre-built example queries for the UI |
| `GET` | `/api/query/memory/{session_id}` | Get conversation history |
| `DELETE` | `/api/query/memory/{session_id}` | Clear conversation history |
| `POST` | `/api/data/ingest` | Trigger data ingestion into Neo4j |
| `GET` | `/api/data/stats` | Row counts from raw JSONL files |

---

## Example Queries

These are answered accurately by the system:

| Query | What it does |
|---|---|
| *Which products are associated with the highest number of billing documents?* | Aggregates SalesOrderItems by Product, ranks by count |
| *Trace the full flow of billing document 90504274* | Walks SO → Delivery → Billing → Journal → Payment for a specific document |
| *Find sales orders that have been delivered but not billed* | Identifies broken O2C flows using delivery/billing status flags |
| *Show revenue by customer* | Sums `totalNetAmount` across SalesOrders, groups by Customer |
| *How many billing documents were cancelled?* | Filters BillingDocument by `isCancelled = true` |
| *Find orders billed but not yet paid* | BillingDocuments with no linked Payment node |
| *Show the end-to-end chain: Sales Order → Delivery → Billing* | Traverses the full FULFILLED_BY chain with a 20-row sample |
| *Give me a dataset overview* | Returns counts of all node types in one query |

---

## Bonus Features Implemented

- **Streaming responses** — `/api/query/stream` uses SSE to stream answer tokens word-by-word
- **Conversation memory** — per-session history (last 10 turns, 30-minute TTL) injected into Cypher prompts for follow-up questions
- **Graph clustering** — Louvain-inspired community detection colours nodes by business community (Sales, Finance, Logistics, Supply Chain)
- **Natural language to Cypher translation** — dynamic Cypher generated by LLaMA 3.3 70B for any query beyond the pre-built patterns
- **Node highlighting** — nodes referenced in query answers are highlighted in the graph visualiser