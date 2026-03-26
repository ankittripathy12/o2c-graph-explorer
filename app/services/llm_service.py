
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ── Schema ────────────────────────────────────────────────────────────
SCHEMA_DESCRIPTION = """
Neo4j graph schema for SAP Order-to-Cash:

Node labels and key properties:
  Customer        {id, name, category, isBlocked}
  SalesOrder      {id, type, totalNetAmount, currency, overallDeliveryStatus,
                   overallBillingStatus, billingBlockReason, deliveryBlockReason, creationDate}
  SalesOrderItem  {id, salesOrder, material, quantity, netAmount, productionPlant,
                   rejectionReason, billingBlock}
  Delivery        {id, deliveryDocument, overallGoodsMovementStatus, overallPickingStatus,
                   shippingPoint, creationDate}
  BillingDocument {id, type, totalNetAmount, currency, isCancelled,
                   accountingDocument, creationDate, billingDate}
  JournalEntry    {id, accountingDocument, glAccount, referenceDocument,
                   amount, currency, postingDate, docType}
  Payment         {id, accountingDocument, amount, currency, clearingDate, postingDate}
  Product         {id, description}
  Plant           {id, name, salesOrganization}

Relationships (all directed) — COMPLETE O2C CHAIN:
  (Customer)        <-[:PLACED_BY]-         (SalesOrder)
  (SalesOrder)      -[:HAS_ITEM]->          (SalesOrderItem)
  (SalesOrderItem)  -[:REFERENCES_PRODUCT]->(Product)
  (SalesOrderItem)  -[:SHIPS_FROM]->        (Plant)
  (SalesOrder)      -[:HAS_DELIVERY]->      (Delivery)
  (Delivery)        -[:FULFILLED_BY]->      (BillingDocument)
  (BillingDocument) -[:BILLED_TO]->         (Customer)
  (BillingDocument) -[:POSTS_TO_JOURNAL]->  (JournalEntry)
  (BillingDocument) -[:SETTLED_BY]->        (Payment)

Key join facts:
  BillingDocument.accountingDocument = JournalEntry.accountingDocument
  BillingDocument.accountingDocument = Payment.accountingDocument
  overallDeliveryStatus / overallGoodsMovementStatus:
    C = Complete, A = Not Started, B = Partial
"""

SYSTEM_PROMPT = (
    "You are a data analyst assistant embedded in an SAP Order-to-Cash "
    "graph exploration tool. "
    "You ONLY answer questions about this O2C dataset. "
    "For anything unrelated respond exactly: "
    "'This system is restricted to queries about the Order to Cash dataset only.' "
    "Be concise. Use **bold** for key numbers. Use markdown tables for lists."
)

OFF_TOPIC_KEYWORDS = [
    "weather", "movie", "film", "recipe", "sport", "cricket",
    "football", "politics", "news", "joke", "poem", "capital of",
    "who is the president", "stock price", "bitcoin", "who invented",
    "tell me about yourself", "who are you", "what is your name",
    "write a story", "write an essay", "translate", "what is the meaning",
    "world cup", "election", "celebrity", "song", "music",
]


class LLMService:
    def __init__(self):
        self.groq_key = os.getenv("GROQ_API_KEY", "")
        if not self.groq_key:
            logger.warning("GROQ_API_KEY not set — LLM calls will fail.")

    # ── provider status ───────────────────────────────────────────────
    def provider_status(self) -> Dict[str, Any]:
        return {
            "default": "groq",
            "groq": {"configured": bool(self.groq_key), "model": GROQ_MODEL},
        }

    # ── Groq call ─────────────────────────────────────────────────────
    async def _call(self, messages: List[Dict], max_tokens: int = 1024) -> Tuple[str, str]:
        if not self.groq_key:
            raise ValueError("GROQ_API_KEY is not set. Get a free key at https://console.groq.com")

        payload = {
            "model":       GROQ_MODEL,
            "max_tokens":  max_tokens,
            "messages":    [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            "temperature": 0.2,
        }
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.groq_key}",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GROQ_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"], "groq"

    # ── 1. domain guard ───────────────────────────────────────────────
    def is_off_topic(self, query: str) -> bool:
        ql = query.lower()
        return any(kw in ql for kw in OFF_TOPIC_KEYWORDS)

    # ── 2. NL → Cypher ────────────────────────────────────────────────
    async def translate_to_cypher(
        self, query: str, provider: Optional[str] = None
    ) -> Tuple[str, str]:
        prompt = f"""Convert the following natural language question into a valid Neo4j Cypher query.

Graph schema:
{SCHEMA_DESCRIPTION}

Rules:
- Use ONLY node labels and relationship types defined in the schema above.
- Always use MATCH … RETURN; never use CREATE / DELETE / SET.
- For aggregations use COUNT(DISTINCT …).
- Add LIMIT 20 unless the user asks for more.
- Return ONLY the raw Cypher — no explanation, no markdown fences, no backticks.

Question: {query}

Cypher:"""

        raw, used = await self._call([{"role": "user", "content": prompt}], max_tokens=400)
        cypher = raw.strip()
        for fence in ["```cypher", "```", "`"]:
            cypher = cypher.replace(fence, "")
        return cypher.strip(), used

    # ── 3. answer generation ──────────────────────────────────────────
    async def generate_answer(
        self,
        user_question: str,
        cypher: str,
        results: List[Dict[str, Any]],
        provider: Optional[str] = None,
    ) -> Tuple[str, str]:
        results_text = json.dumps(results[:20], indent=2, default=str)
        prompt = f"""Original question: {user_question}

Cypher query used:
{cypher}

Query results (up to 20 rows):
{results_text}

Write a concise, data-backed natural language answer.
If the results are empty, say so clearly.
Use **bold** for key numbers. Use a markdown table if the results are tabular."""

        return await self._call([{"role": "user", "content": prompt}], max_tokens=600)

    # ── 4. pre-built Cypher ───────────────────────────────────────────
    def get_prebuilt_cypher(self, query: str) -> Optional[str]:
        import re
        q = query.lower()

        # products with highest billing documents
        if "billing document" in q and ("product" in q or "highest" in q or "most" in q):
            return """
MATCH (soi:SalesOrderItem)-[:REFERENCES_PRODUCT]->(p:Product)
WITH p, COUNT(soi) AS item_count
RETURN p.id AS product_id, p.description AS product_name,
       item_count AS billing_document_count
ORDER BY billing_document_count DESC
LIMIT 10
""".strip()

        # trace full flow of a billing document
        if "trace" in q or ("flow" in q and "billing" in q):
            m = re.search(r"\b(\d{7,})\b", query)
            doc_id = m.group(1) if m else "0"
            return f"""
MATCH (b:BillingDocument {{id: '{doc_id}'}})
OPTIONAL MATCH (d:Delivery)-[:FULFILLED_BY]->(b)
OPTIONAL MATCH (so:SalesOrder)-[:HAS_DELIVERY]->(d)
OPTIONAL MATCH (b)-[:BILLED_TO]->(c:Customer)
OPTIONAL MATCH (b)-[:POSTS_TO_JOURNAL]->(j:JournalEntry)
OPTIONAL MATCH (b)-[:SETTLED_BY]->(p:Payment)
RETURN b.id AS billingDocument, b.totalNetAmount AS amount,
       c.id AS customer, c.name AS customerName,
       so.id AS salesOrder,
       d.id AS deliveryDocument, d.overallGoodsMovementStatus AS deliveryStatus,
       j.id AS journalEntry, j.accountingDocument AS accountingDoc,
       p.id AS payment, p.amount AS paymentAmount
""".strip()

        # incomplete flows: delivered but not billed
        if "incomplete" in q or "broken" in q or ("delivered" in q and "not billed" in q):
            return """
MATCH (so:SalesOrder)
WHERE so.overallDeliveryStatus = 'C'
  AND NOT EXISTS {
    MATCH (b:BillingDocument)-[:BILLED_TO]->(c:Customer)<-[:PLACED_BY]-(so)
  }
RETURN so.id AS salesOrder, so.totalNetAmount AS amount,
       so.overallDeliveryStatus AS deliveryStatus,
       so.overallBillingStatus  AS billingStatus
LIMIT 20
""".strip()

        # billed not paid
        if "billed" in q and ("not paid" in q or "no payment" in q):
            return """
MATCH (b:BillingDocument)-[:BILLED_TO]->(c:Customer)
WHERE NOT EXISTS { MATCH (b)-[:SETTLED_BY]->(:Payment) }
  AND b.isCancelled = false
RETURN b.id AS billingDocument, b.totalNetAmount AS amount,
       c.id AS customer, c.name AS customerName
ORDER BY b.totalNetAmount DESC
LIMIT 20
""".strip()

        # revenue by customer
        if ("revenue" in q or "total" in q) and "customer" in q:
            return """
MATCH (so:SalesOrder)-[:PLACED_BY]->(c:Customer)
RETURN c.id AS customerId, c.name AS customerName,
       COUNT(so) AS orderCount,
       SUM(so.totalNetAmount) AS totalRevenue,
       so.currency AS currency
ORDER BY totalRevenue DESC
""".strip()

        # cancelled billings
        if "cancel" in q and "bill" in q:
            return """
MATCH (b:BillingDocument)
WHERE b.isCancelled = true
RETURN b.id AS billingDocument, b.totalNetAmount AS amount,
       b.creationDate AS date, b.type AS type
ORDER BY b.creationDate DESC
LIMIT 20
""".strip()

        # delivery status breakdown
        if "delivery" in q and "status" in q:
            return """
MATCH (d:Delivery)
RETURN d.overallGoodsMovementStatus AS status, COUNT(d) AS count
ORDER BY count DESC
""".strip()

        # show sales order to delivery to billing chain
        if ("chain" in q or "end to end" in q or "end-to-end" in q or
                ("sales order" in q and "delivery" in q and "billing" in q)):
            return """
MATCH (so:SalesOrder)-[:HAS_DELIVERY]->(d:Delivery)-[:FULFILLED_BY]->(b:BillingDocument)
RETURN so.id AS salesOrder, so.totalNetAmount AS soAmount,
       d.id AS delivery, d.overallGoodsMovementStatus AS deliveryStatus,
       b.id AS billingDocument, b.totalNetAmount AS billingAmount
LIMIT 20
""".strip()

        # payment summary
        if "payment" in q and ("summary" in q or "total" in q or "how many" in q):
            return """
MATCH (p:Payment)
RETURN COUNT(p) AS totalPayments,
       SUM(p.amount) AS totalAmount,
       p.currency AS currency,
       COUNT(DISTINCT p.customer) AS uniqueCustomers
""".strip()

        # list/all products
        if "product" in q and ("unique" in q or "list" in q or "all" in q or
                                "catalogue" in q or "catalog" in q):
            return """
MATCH (p:Product)
RETURN p.id AS product_id, p.description AS product_name
ORDER BY p.description
""".strip()

        # most ordered products
        if "product" in q and ("most" in q or "appear" in q or "ordered" in q or "popular" in q):
            return """
MATCH (soi:SalesOrderItem)-[:REFERENCES_PRODUCT]->(p:Product)
RETURN p.id AS product_id, p.description AS product_name,
       COUNT(soi) AS times_ordered
ORDER BY times_ordered DESC
LIMIT 20
""".strip()

        # plant shipping most
        if "plant" in q and ("most" in q or "ships" in q or "highest" in q):
            return """
MATCH (soi:SalesOrderItem)-[:SHIPS_FROM]->(pl:Plant)
RETURN pl.id AS plant_id, pl.name AS plant_name,
       COUNT(soi) AS items_shipped
ORDER BY items_shipped DESC
""".strip()

        # list plants
        if "plant" in q and ("list" in q or "all" in q or "show" in q):
            return """
MATCH (pl:Plant)
RETURN pl.id AS plant_id, pl.name AS plant_name,
       pl.salesOrganization AS sales_org
ORDER BY pl.name
""".strip()

        # how many customers
        if "how many" in q and "customer" in q:
            return """
MATCH (c:Customer)
RETURN COUNT(c) AS total_customers
""".strip()

        # dataset overview
        if (("how many" in q and "order" in q) or "overview" in q or "dataset" in q or
                "summary" in q):
            return """
MATCH (so:SalesOrder) WITH COUNT(so) AS orders
MATCH (b:BillingDocument) WITH orders, COUNT(b) AS billings
MATCH (p:Payment) WITH orders, billings, COUNT(p) AS payments
MATCH (d:Delivery) WITH orders, billings, payments, COUNT(d) AS deliveries
MATCH (c:Customer) WITH orders, billings, payments, deliveries, COUNT(c) AS customers
MATCH (pr:Product) WITH orders, billings, payments, deliveries, customers, COUNT(pr) AS products
RETURN orders, billings, payments, deliveries, customers, products
""".strip()

        # billing type breakdown
        if "billing" in q and ("type" in q or "breakdown" in q):
            return """
MATCH (b:BillingDocument)
RETURN b.type AS billing_type,
       COUNT(b) AS count,
       SUM(b.totalNetAmount) AS total_amount
ORDER BY count DESC
""".strip()

        # total revenue
        if "total revenue" in q or ("total" in q and "revenue" in q and "customer" not in q):
            return """
MATCH (so:SalesOrder)
RETURN COUNT(so) AS total_orders,
       SUM(so.totalNetAmount) AS total_revenue,
       so.currency AS currency
""".strip()

        return None
