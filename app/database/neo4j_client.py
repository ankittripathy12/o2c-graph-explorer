"""
neo4j_client.py  —  Smart client with automatic InMemory fallback.

Priority:
  1. Try to connect to Neo4j using env vars NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
  2. If connection fails (driver not installed, wrong creds, DB unreachable)
     → silently fall back to InMemoryClient so the app keeps working.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


import glob
import json

DATA_DIR = os.getenv("DATA_DIR", "data/sap-o2c-data")


def _load(folder: str) -> List[Dict]:
    rows = []
    pattern = os.path.join(DATA_DIR, folder, "*.jsonl")
    for filepath in glob.glob(pattern):
        with open(filepath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


class InMemoryClient:
    """
    Pure-Python fallback — no Neo4j required.
    Reads JSONL files from DATA_DIR and answers queries in-process.
    """

    def __init__(self):
        self._loaded = False
        self.so_headers      = []
        self.so_items        = []
        self.billings        = []
        self.billing_items   = []
        self.delivery_items  = []
        self.deliveries      = []
        self.payments        = []
        self.journals        = []
        self.partners        = []
        self.products        = []
        self.plants          = []

        self._billing_acct_map:   Dict[str, str] = {}
        self._product_map:        Dict[str, str] = {}
        self._partner_map:        Dict[str, str] = {}
        self._delivery_to_so_map: Dict[str, str] = {}
        self._billing_to_del_map: Dict[str, str] = {}

    
    def connect(self):
        self._load_all()

    def close(self):
        pass

    def create_constraints(self):
        pass

  
    def _load_all(self):
        if self._loaded:
            return
        try:
            self.so_headers     = _load("sales_order_headers")
            self.so_items       = _load("sales_order_items")
            self.billings       = _load("billing_document_headers")
            self.billing_items  = _load("billing_document_items")
            self.delivery_items = _load("outbound_delivery_items")
            self.deliveries     = _load("outbound_delivery_headers")
            self.payments       = _load("payments_accounts_receivable")
            self.journals       = _load("journal_entry_items_accounts_receivable")
            self.partners       = _load("business_partners")
            self.products       = [p for p in _load("product_descriptions") if p.get("language") == "EN"]
            self.plants         = _load("plants")

            self._billing_acct_map = {
                str(b["accountingDocument"]): str(b["billingDocument"])
                for b in self.billings if b.get("accountingDocument")
            }
            self._product_map = {
                p["product"]: p["productDescription"]
                for p in self.products
            }
            self._partner_map = {
                bp["customer"]: bp.get("businessPartnerFullName") or bp.get("businessPartnerName", "")
                for bp in self.partners
            }
            for di in self.delivery_items:
                ddoc = str(di.get("deliveryDocument", ""))
                so   = str(di.get("referenceSdDocument", ""))
                if ddoc and so and ddoc not in self._delivery_to_so_map:
                    self._delivery_to_so_map[ddoc] = so
            for bi in self.billing_items:
                bdoc = str(bi.get("billingDocument", ""))
                ddoc = str(bi.get("referenceSdDocument", ""))
                if bdoc and ddoc and bdoc not in self._billing_to_del_map:
                    self._billing_to_del_map[bdoc] = ddoc

            self._loaded = True
            logger.info(
                "InMemory store loaded — %d SOs, %d deliveries, %d billings, %d payments",
                len(self.so_headers), len(self.deliveries),
                len(self.billings), len(self.payments),
            )
        except Exception as e:
            logger.warning("Could not load data files: %s", e)

    def get_graph_sample(self, limit: int = 150) -> Dict:
        self._load_all()
        nodes, links = [], []
        nm = set()

        def node(id_, label, props):
            if id_ not in nm:
                nm.add(id_)
                nodes.append({"id": id_, "_label": label, **props})

        def edge(src, tgt, rel):
            if src in nm and tgt in nm:
                links.append({"source": src, "target": tgt, "relationship": rel})

        for bp in self.partners:
            node("C_" + bp["customer"], "Customer", {
                "id": "C_" + bp["customer"],
                "name": bp.get("businessPartnerFullName", ""),
                "customer": bp["customer"],
            })
        for so in self.so_headers[:limit]:
            sid = "SO_" + str(so["salesOrder"])
            node(sid, "SalesOrder", {
                "id": sid, "salesOrder": str(so["salesOrder"]),
                "totalNetAmount": so.get("totalNetAmount", ""),
                "currency": so.get("transactionCurrency", ""),
                "overallDeliveryStatus": so.get("overallDeliveryStatus", ""),
                "overallBillingStatus":  so.get("overallOrdReltdBillgStatus", ""),
            })
            edge("C_" + str(so.get("soldToParty", "")), sid, "PLACED_BY")
        for item in self.so_items[:limit]:
            iid = "SOI_" + str(item["salesOrder"]) + "_" + str(item["salesOrderItem"])
            node(iid, "SalesOrderItem", {
                "id": iid, "salesOrder": str(item["salesOrder"]),
                "material": str(item.get("material", "")),
                "netAmount": item.get("netAmount", ""),
            })
            edge("SO_" + str(item["salesOrder"]), iid, "HAS_ITEM")
            if item.get("material"):
                pid = "P_" + str(item["material"])
                node(pid, "Product", {
                    "id": pid,
                    "description": self._product_map.get(str(item["material"]), ""),
                })
                edge(iid, pid, "REFERENCES_PRODUCT")
        for d in self.deliveries[:limit]:
            ddoc = str(d["deliveryDocument"])
            did  = "D_" + ddoc
            node(did, "Delivery", {
                "id": did, "deliveryDocument": ddoc,
                "overallGoodsMovementStatus": d.get("overallGoodsMovementStatus", ""),
                "overallPickingStatus": d.get("overallPickingStatus", ""),
                "shippingPoint": str(d.get("shippingPoint", "")),
                "creationDate":  str(d.get("creationDate", "")),
            })
            so_id = self._delivery_to_so_map.get(ddoc)
            if so_id:
                edge("SO_" + so_id, did, "HAS_DELIVERY")
        for b in self.billings[:limit]:
            bid_str = str(b["billingDocument"])
            bid     = "B_" + bid_str
            node(bid, "BillingDocument", {
                "id": bid, "billingDocument": bid_str,
                "totalNetAmount": b.get("totalNetAmount", ""),
                "isCancelled":    b.get("billingDocumentIsCancelled", False),
                "creationDate":   str(b.get("creationDate", "")),
                "accountingDocument": str(b.get("accountingDocument", "")),
            })
            edge("C_" + str(b.get("soldToParty", "")), bid, "BILLED_TO")
            ddoc = self._billing_to_del_map.get(bid_str)
            if ddoc:
                edge("D_" + ddoc, bid, "FULFILLED_BY")
        seen_je = set()
        for j in self.journals:
            acct = str(j.get("accountingDocument", ""))
            key  = acct + "_" + str(j.get("accountingDocumentItem", "1"))
            if key in seen_je:
                continue
            seen_je.add(key)
            jid = "JE_" + key
            node(jid, "JournalEntry", {
                "id": jid, "accountingDocument": acct,
                "amount": j.get("amountInTransactionCurrency", ""),
                "postingDate": str(j.get("postingDate", "")),
            })
            bill_id = self._billing_acct_map.get(acct)
            if bill_id:
                edge("B_" + bill_id, jid, "POSTS_TO_JOURNAL")
        seen_pay = set()
        for p in self.payments:
            acct = str(p.get("accountingDocument", ""))
            key  = acct + "_" + str(p.get("accountingDocumentItem", "1"))
            if key in seen_pay:
                continue
            seen_pay.add(key)
            pyid = "PAY_" + key
            node(pyid, "Payment", {
                "id": pyid, "accountingDocument": acct,
                "amount": p.get("amountInTransactionCurrency", ""),
                "customer": p.get("customer", ""),
                "clearingDate": str(p.get("clearingDate", "")),
            })
            bill_id = self._billing_acct_map.get(acct)
            if bill_id:
                edge("B_" + bill_id, pyid, "SETTLED_BY")

        return {"nodes": nodes, "relationships": links}

    def get_node_detail(self, node_id: str) -> Optional[Dict]:
        self._load_all()
        graph = self.get_graph_sample(500)
        node  = next((n for n in graph["nodes"] if n["id"] == node_id), None)
        if not node:
            return None
        neighbours = [
            {
                "id":  r["target"] if r["source"] == node_id else r["source"],
                "rel": r["relationship"],
                "dir": "out" if r["source"] == node_id else "in",
            }
            for r in graph["relationships"]
            if r["source"] == node_id or r["target"] == node_id
        ]
        return {"label": node.get("_label", ""), "props": node, "neighbours": neighbours}

    # ── write ops (no-ops in fallback mode) ─────────────────────────
    def merge_node(self, label: str, id_value: str, props: Dict) -> None:
        pass  # InMemory: reads only, no persistence

    def merge_relationship(self, src_label: str, src_id: str,
                           tgt_label: str, tgt_id: str,
                           rel_type: str, props: Optional[Dict] = None) -> None:
        pass  # InMemory: reads only, no persistence

    # ── query router ─────────────────────────────────────────────────
    def query(self, cypher: str, params: Optional[Dict] = None) -> List[Dict]:
        self._load_all()
        c = cypher.lower()

        if "references_product" in c and "count" in c:
            return self._top_billed_products()
        if "billed_to" in c and "posts_to_journal" in c:
            import re
            m = re.search(r"id:\s*['\"](\w+)['\"]", cypher)
            return self._trace_billing(m.group(1) if m else None)
        if "overalldeliverystatus" in c and "not exists" in c:
            return self._incomplete_flows()
        if "settled_by" in c and "not exists" in c:
            return self._billed_not_paid()
        if "placed_by" in c and "sum" in c:
            return self._revenue_by_customer()
        if "iscancelled" in c:
            return self._cancelled_billings()
        if "overallgoodsmovementstatus" in c and "count" in c:
            return self._delivery_status()
        if "payment" in c and "count" in c and "sum" in c:
            return self._payment_summary()
        if "has_delivery" in c or "fulfilled_by" in c:
            return self._delivery_chain_sample()
        return self._summary_stats()

    def _top_billed_products(self) -> List[Dict]:
        mat_count: Dict[str, int] = {}
        billed_customers = {str(b["soldToParty"]) for b in self.billings}
        for so in self.so_headers:
            if str(so.get("soldToParty", "")) in billed_customers:
                for item in self.so_items:
                    if str(item["salesOrder"]) == str(so["salesOrder"]):
                        m = str(item.get("material", ""))
                        if m:
                            mat_count[m] = mat_count.get(m, 0) + 1
        return sorted(
            [{"product_id": k, "product_name": self._product_map.get(k, k), "billing_document_count": v}
             for k, v in mat_count.items()],
            key=lambda x: -x["billing_document_count"],
        )[:10]

    def _trace_billing(self, doc_id: Optional[str]) -> List[Dict]:
        b = next((x for x in self.billings if str(x["billingDocument"]) == str(doc_id)), None)
        if not b:
            return [{"error": f"Billing document {doc_id} not found"}]
        acct     = str(b.get("accountingDocument", ""))
        cid      = str(b.get("soldToParty", ""))
        ddoc     = self._billing_to_del_map.get(str(doc_id))
        delivery = next((d for d in self.deliveries if str(d.get("deliveryDocument", "")) == ddoc), None) if ddoc else None
        so_id    = self._delivery_to_so_map.get(ddoc) if ddoc else None
        so       = next((s for s in self.so_headers if str(s.get("salesOrder", "")) == so_id), None) if so_id else None
        je       = next((j for j in self.journals if str(j.get("accountingDocument", "")) == acct), None)
        pay      = next((p for p in self.payments if str(p.get("accountingDocument", "")) == acct), None)
        return [{
            "salesOrder": str(so["salesOrder"]) if so else "N/A",
            "salesOrderAmount": so.get("totalNetAmount", "") if so else None,
            "deliveryDocument": ddoc or "N/A",
            "deliveryStatus": delivery.get("overallGoodsMovementStatus", "") if delivery else None,
            "billingDocument": str(b["billingDocument"]),
            "billingAmount": b.get("totalNetAmount", ""),
            "customer": cid, "customerName": self._partner_map.get(cid, ""),
            "journalEntry": str(je.get("accountingDocument", "")) if je else None,
            "accountingDoc": acct,
            "payment": str(pay.get("accountingDocument", "")) if pay else None,
            "paymentAmount": pay.get("amountInTransactionCurrency", "") if pay else None,
            "paymentDate": str(pay.get("clearingDate", "")) if pay else None,
        }]

    def _incomplete_flows(self) -> List[Dict]:
        billed = {str(b["soldToParty"]) for b in self.billings}
        return [
            {"salesOrder": str(so["salesOrder"]), "amount": so.get("totalNetAmount", ""),
             "deliveryStatus": so.get("overallDeliveryStatus", ""),
             "billingStatus": so.get("overallOrdReltdBillgStatus", "")}
            for so in self.so_headers
            if so.get("overallDeliveryStatus") == "C" and str(so.get("soldToParty", "")) not in billed
        ][:20]

    def _billed_not_paid(self) -> List[Dict]:
        paid_accts = {str(p["accountingDocument"]) for p in self.payments}
        return sorted(
            [{"billingDocument": str(b["billingDocument"]), "amount": b.get("totalNetAmount", ""),
              "customer": str(b.get("soldToParty", "")),
              "customerName": self._partner_map.get(str(b.get("soldToParty", "")), "")}
             for b in self.billings
             if not b.get("billingDocumentIsCancelled") and
                str(b.get("accountingDocument", "")) not in paid_accts],
            key=lambda x: -float(x["amount"] or 0),
        )[:20]

    def _revenue_by_customer(self) -> List[Dict]:
        rev: Dict[str, Dict] = {}
        for so in self.so_headers:
            cid = str(so.get("soldToParty", ""))
            if cid not in rev:
                rev[cid] = {"customerId": cid, "customerName": self._partner_map.get(cid, ""),
                             "orderCount": 0, "totalRevenue": 0.0,
                             "currency": so.get("transactionCurrency", "")}
            rev[cid]["orderCount"]   += 1
            rev[cid]["totalRevenue"] += float(so.get("totalNetAmount") or 0)
        return sorted(rev.values(), key=lambda x: -x["totalRevenue"])

    def _cancelled_billings(self) -> List[Dict]:
        return [
            {"billingDocument": str(b["billingDocument"]), "amount": b.get("totalNetAmount", ""),
             "date": str(b.get("creationDate", "")), "type": b.get("billingDocumentType", "")}
            for b in self.billings if b.get("billingDocumentIsCancelled")
        ][:20]

    def _delivery_status(self) -> List[Dict]:
        counts: Dict[str, int] = {}
        labels = {"C": "Completed", "A": "Not Started", "B": "Partial"}
        for d in self.deliveries:
            s = d.get("overallGoodsMovementStatus", "Unknown")
            counts[s] = counts.get(s, 0) + 1
        return [{"status": labels.get(k, k), "count": v} for k, v in counts.items()]

    def _payment_summary(self) -> List[Dict]:
        total = sum(float(p.get("amountInTransactionCurrency") or 0) for p in self.payments)
        return [{"totalPayments": len(self.payments), "totalAmount": round(total, 2),
                 "currency": "INR", "uniqueCustomers": len({p.get("customer") for p in self.payments})}]

    def _delivery_chain_sample(self) -> List[Dict]:
        result = []
        for ddoc, so_id in list(self._delivery_to_so_map.items())[:20]:
            so    = next((s for s in self.so_headers if str(s.get("salesOrder", "")) == so_id), None)
            b_doc = next((bid for bid, d in self._billing_to_del_map.items() if d == ddoc), None)
            result.append({"salesOrder": so_id, "soAmount": so.get("totalNetAmount", "") if so else None,
                            "deliveryDocument": ddoc, "billingDocument": b_doc})
            if len(result) >= 10:
                break
        return result

    def _summary_stats(self) -> List[Dict]:
        return [{"salesOrders": len(self.so_headers), "billingDocuments": len(self.billings),
                 "deliveries": len(self.deliveries), "payments": len(self.payments),
                 "customers": len(self.partners), "products": len(self.products)}]

    def clear_all(self): pass

    def node_counts(self) -> Dict[str, int]:
        self._load_all()
        return {"Customer": len(self.partners), "SalesOrder": len(self.so_headers),
                "SalesOrderItem": len(self.so_items), "BillingDocument": len(self.billings),
                "JournalEntry": len(self.journals), "Payment": len(self.payments),
                "Delivery": len(self.deliveries), "Product": len(self.products), "Plant": len(self.plants)}

    def relationship_counts(self) -> Dict[str, int]:
        return {"PLACED_BY": len(self.so_headers), "HAS_ITEM": len(self.so_items),
                "HAS_DELIVERY": len(self._delivery_to_so_map),
                "FULFILLED_BY": len(self._billing_to_del_map),
                "BILLED_TO": len(self.billings),
                "SETTLED_BY": min(len(self.payments), len(self.billings)),
                "POSTS_TO_JOURNAL": min(len(self.journals), len(self.billings))}

    @property
    def mode(self) -> str:
        return "inmemory"


# ══════════════════════════════════════════════════════════════════════
# SECTION 2 — Real Neo4jClient  (only instantiated when driver is available)
#
# Uses driver.execute_query() — the official recommended API as per Neo4j docs.
# Also passes database_ extracted from the URI for Aura instances, so
# queries hit the right database (e.g. "63234061" from neo4j+s://63234061.databases.neo4j.io).
# ══════════════════════════════════════════════════════════════════════
class Neo4jClient:
    """
    Wraps the official neo4j Python driver using execute_query() — the
    modern API recommended by Neo4j docs (not the legacy session.run()).

    Install:  pip install neo4j
    Env vars: NEO4J_URI  NEO4J_USER  NEO4J_PASSWORD

    For Neo4j Aura the database name is the subdomain of the URI.
    e.g. neo4j+s://63234061.databases.neo4j.io  →  database = "63234061"
    For local / self-hosted instances leave NEO4J_DATABASE unset and
    it will default to "neo4j".
    """

    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase  
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._uri    = uri

        
        db_env = os.getenv("NEO4J_DATABASE", "").strip()
        if db_env:
            self._database = db_env
        else:
            
            import re
            m = re.match(r"[a-z+]+://([^.]+)\.", uri)
            self._database = m.group(1) if m else "neo4j"

        logger.info("Neo4j driver created — URI: %s | database: %s", uri, self._database)

  
    def connect(self):
        """Verify connectivity — raises if unreachable or auth fails."""
        self._driver.verify_connectivity()
        logger.info("Neo4j connection verified ✔  (database: %s)", self._database)

    def close(self):
        self._driver.close()

    def create_constraints(self):
        """
        Idempotent uniqueness constraints — safe to run on every startup.
        Uses execute_query() as per official docs.
        """
        constraints = [
            ("Customer",        "id"),
            ("SalesOrder",      "id"),
            ("SalesOrderItem",  "id"),
            ("BillingDocument", "id"),
            ("JournalEntry",    "id"),
            ("Payment",         "id"),
            ("Delivery",        "id"),
            ("Product",         "id"),
            ("Plant",           "id"),
        ]
        for label, prop in constraints:
            self._driver.execute_query(
                f"CREATE CONSTRAINT IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE",
                database_=self._database,
            )
        logger.info("Neo4j constraints ensured for database '%s'", self._database)

    def merge_node(self, label: str, id_value: str, props: Dict) -> None:
        """
        MERGE a node by id, then SET all properties.
        Uses execute_query() — no manual session/transaction management needed.
        """
        self._driver.execute_query(
            f"MERGE (n:{label} {{id: $id}}) SET n += $props",
            id=id_value,
            props=props,
            database_=self._database,
        )

    def merge_relationship(self, src_label: str, src_id: str,
                           tgt_label: str, tgt_id: str,
                           rel_type: str, props: Optional[Dict] = None) -> None:
        """
        MERGE a relationship between two already-existing nodes.
        Silently skips if either node is not yet in the DB.
        """
        cypher = (
            f"MATCH (a:{src_label} {{id: $src_id}}) "
            f"MATCH (b:{tgt_label} {{id: $tgt_id}}) "
            f"MERGE (a)-[r:{rel_type}]->(b)"
            + (" SET r += $props" if props else "")
        )
        self._driver.execute_query(
            cypher,
            src_id=src_id,
            tgt_id=tgt_id,
            props=props or {},
            database_=self._database,
        )

    # ── read ops ─────────────────────────────────────────────────────
    def query(self, cypher: str, params: Optional[Dict] = None) -> List[Dict]:
        """
        Run any read Cypher and return results as a list of plain dicts.
        Uses execute_query() + record.data() per official docs.
        """
        records, _, _ = self._driver.execute_query(
            cypher,
            **(params or {}),
            database_=self._database,
        )
        return [record.data() for record in records]

    def get_graph_sample(self, limit: int = 150) -> Dict:
        """
        Pull a sample of the graph for the visual explorer.
        Two separate execute_query() calls — nodes first, then relationships.
        """
        # ── nodes ──
        node_records, _, _ = self._driver.execute_query(
            "MATCH (n) RETURN n, labels(n)[0] AS _label LIMIT $limit",
            limit=limit,
            database_=self._database,
        )
        nodes = []
        for rec in node_records:
            props = dict(rec["n"])          
            props["_label"] = rec["_label"]
            nodes.append(props)

        # ── relationships ──
        rel_records, _, _ = self._driver.execute_query(
            "MATCH (a)-[r]->(b) "
            "RETURN a.id AS source, b.id AS target, type(r) AS relationship "
            "LIMIT $limit",
            limit=limit * 3,
            database_=self._database,
        )
        relationships = [rec.data() for rec in rel_records]

        return {"nodes": nodes, "relationships": relationships}

    def get_node_detail(self, node_id: str) -> Optional[Dict]:
        """Fetch a single node plus its direct neighbours."""
        records, _, _ = self._driver.execute_query(
            """
            MATCH (n {id: $id})
            OPTIONAL MATCH (n)-[r]-(m)
            RETURN n, labels(n)[0] AS label,
                   collect({
                     id:  m.id,
                     rel: type(r),
                     dir: CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END
                   }) AS neighbours
            """,
            id=node_id,
            database_=self._database,
        )
        if not records:
            return None
        rec = records[0]
        return {
            "label":      rec["label"],
            "props":      dict(rec["n"]),
            "neighbours": [n for n in rec["neighbours"] if n.get("id")],
        }

    def clear_all(self):
        """Delete every node and relationship — use with caution."""
        self._driver.execute_query(
            "MATCH (n) DETACH DELETE n",
            database_=self._database,
        )
        logger.warning("Neo4j graph cleared (database: %s)", self._database)

    def node_counts(self) -> Dict[str, int]:
        labels = ["Customer", "SalesOrder", "SalesOrderItem", "BillingDocument",
                  "JournalEntry", "Payment", "Delivery", "Product", "Plant"]
        counts = {}
        for lbl in labels:
            records, _, _ = self._driver.execute_query(
                f"MATCH (n:{lbl}) RETURN count(n) AS c",
                database_=self._database,
            )
            counts[lbl] = records[0]["c"] if records else 0
        return counts

    def relationship_counts(self) -> Dict[str, int]:
        rels = ["PLACED_BY", "HAS_ITEM", "HAS_DELIVERY", "FULFILLED_BY",
                "BILLED_TO", "SETTLED_BY", "POSTS_TO_JOURNAL", "REFERENCES_PRODUCT", "SHIPS_FROM"]
        counts = {}
        for rel in rels:
            records, _, _ = self._driver.execute_query(
                f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c",
                database_=self._database,
            )
            counts[rel] = records[0]["c"] if records else 0
        return counts

    @property
    def mode(self) -> str:
        return "neo4j"


def _build_client():
    """
    Attempt to build a real Neo4jClient.
    Falls back to InMemoryClient if:
      - env vars are missing
      - neo4j driver package is not installed
      - the database is unreachable / auth fails
    """
    uri      = os.getenv("NEO4J_URI", "").strip()
    user     = os.getenv("NEO4J_USER", "neo4j").strip()
    password = os.getenv("NEO4J_PASSWORD", "").strip()

    if not uri or not password:
        logger.info(
            "NEO4J_URI / NEO4J_PASSWORD not set → using InMemoryClient. "
            "Add them to your .env to enable Neo4j."
        )
        client = InMemoryClient()
        client.connect()
        return client

    try:
        client = Neo4jClient(uri, user, password)
        client.connect()                  
        client.create_constraints()
        logger.info("✅  Connected to Neo4j at %s", uri)
        return client

    except ImportError:
        logger.warning(
            "neo4j package not installed (pip install neo4j) → falling back to InMemoryClient"
        )
    except Exception as exc:
        logger.warning(
            "Could not connect to Neo4j (%s: %s) → falling back to InMemoryClient",
            type(exc).__name__, exc,
        )

    client = InMemoryClient()
    client.connect()
    return client


neo4j_client = _build_client()


def get_client_status() -> Dict[str, Any]:
    """Returns a dict the frontend can display to show which mode is active."""
    mode = neo4j_client.mode
    return {
        "mode":        mode,
        "neo4j":       {"connected": mode == "neo4j",
                        "uri": os.getenv("NEO4J_URI", "not configured")},
        "inmemory":    {"active": mode == "inmemory"},
        "description": ("Connected to Neo4j graph database" if mode == "neo4j"
                        else "Running on in-memory fallback (set NEO4J_URI + NEO4J_PASSWORD in .env)"),
    }