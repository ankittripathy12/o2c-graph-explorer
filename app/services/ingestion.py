
import glob
import json
import logging
import os
from typing import Any, Dict, List, Tuple

from app.database.neo4j_client import neo4j_client

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "data/sap-o2c-data")


def _load(folder: str) -> List[Dict]:
    rows: List[Dict] = []
    pattern = os.path.join(DATA_DIR, folder, "*.jsonl")
    for filepath in glob.glob(pattern):
        with open(filepath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return json.dumps(value)
    return value


def _props(row: Dict, *keys) -> Dict:
    return {k: _clean(row[k]) for k in keys if k in row and row[k] is not None and row[k] != ""}


# ── public entry-point ────────────────────────────────────────────────
def ingest_all() -> Tuple[Dict[str, int], int]:
    logger.info("Starting full O2C ingestion from %s …", DATA_DIR)
    counts: Dict[str, int] = {}
    rel_count = 0

    # 1. Customers
    partners = _load("business_partners")
    for bp in partners:
        cid = str(bp["customer"])
        neo4j_client.merge_node("Customer", cid, {
            "id":       cid,
            "name":     bp.get("businessPartnerFullName") or bp.get("businessPartnerName", ""),
            "category": bp.get("businessPartnerCategory", ""),
            "isBlocked": bp.get("businessPartnerIsBlocked", False),
        })
    counts["Customer"] = len(partners)
    logger.info("  ✔ %d Customers", len(partners))

    # 2. Products
    products = [p for p in _load("product_descriptions") if p.get("language") == "EN"]
    for p in products:
        neo4j_client.merge_node("Product", str(p["product"]), {
            "id":          str(p["product"]),
            "description": p.get("productDescription", ""),
        })
    counts["Product"] = len(products)
    logger.info("  ✔ %d Products", len(products))

    # 3. Plants
    plants = _load("plants")
    for pl in plants:
        neo4j_client.merge_node("Plant", str(pl["plant"]), {
            "id":                str(pl["plant"]),
            "name":              pl.get("plantName", ""),
            "salesOrganization": pl.get("salesOrganization", ""),
        })
    counts["Plant"] = len(plants)
    logger.info("  ✔ %d Plants", len(plants))

    # 4. Sales Order Headers
    so_headers = _load("sales_order_headers")
    for so in so_headers:
        soid = str(so["salesOrder"])
        neo4j_client.merge_node("SalesOrder", soid, {
            "id":                    soid,
            "type":                  so.get("salesOrderType", ""),
            "creationDate":          str(so.get("creationDate", "")),
            "totalNetAmount":        float(so.get("totalNetAmount") or 0),
            "currency":              so.get("transactionCurrency", ""),
            "overallDeliveryStatus": so.get("overallDeliveryStatus", ""),
            "overallBillingStatus":  so.get("overallOrdReltdBillgStatus", ""),
        })
        cid = str(so.get("soldToParty", ""))
        if cid:
            neo4j_client.merge_relationship("SalesOrder", soid, "Customer", cid, "PLACED_BY")
            rel_count += 1
    counts["SalesOrder"] = len(so_headers)
    logger.info("  ✔ %d SalesOrders", len(so_headers))

    # 5. Sales Order Items
    so_items = _load("sales_order_items")
    for item in so_items:
        iid = f"{item['salesOrder']}_{item['salesOrderItem']}"
        neo4j_client.merge_node("SalesOrderItem", iid, {
            "id":        iid,
            "salesOrder": str(item["salesOrder"]),
            "material":  str(item.get("material", "")),
            "quantity":  float(item.get("requestedQuantity") or 0),
            "netAmount": float(item.get("netAmount") or 0),
        })
        soid = str(item["salesOrder"])
        neo4j_client.merge_relationship("SalesOrder", soid, "SalesOrderItem", iid, "HAS_ITEM")
        rel_count += 1
        mat = str(item.get("material", ""))
        if mat:
            neo4j_client.merge_relationship("SalesOrderItem", iid, "Product", mat, "REFERENCES_PRODUCT")
            rel_count += 1
        plant = str(item.get("productionPlant", ""))
        if plant:
            neo4j_client.merge_relationship("SalesOrderItem", iid, "Plant", plant, "SHIPS_FROM")
            rel_count += 1
    counts["SalesOrderItem"] = len(so_items)
    logger.info("  ✔ %d SalesOrderItems", len(so_items))

    # 6. Deliveries + HAS_DELIVERY links from delivery_items
    deliveries = _load("outbound_delivery_headers")
    delivery_items = _load("outbound_delivery_items")

    # Build deliveryDoc → salesOrder map from delivery_items
    delivery_to_so: Dict[str, str] = {}
    for di in delivery_items:
        ddoc = str(di.get("deliveryDocument", ""))
        so   = str(di.get("referenceSdDocument", ""))
        if ddoc and so and ddoc not in delivery_to_so:
            delivery_to_so[ddoc] = so

    for d in deliveries:
        did = str(d["deliveryDocument"])
        neo4j_client.merge_node("Delivery", did, {
            "id":                         did,
            "creationDate":               str(d.get("creationDate", "")),
            "actualGoodsMovementDate":    str(d.get("actualGoodsMovementDate", "")),
            "overallGoodsMovementStatus": d.get("overallGoodsMovementStatus", ""),
            "overallPickingStatus":       d.get("overallPickingStatus", ""),
            "shippingPoint":              str(d.get("shippingPoint", "")),
        })
        # SalesOrder ──HAS_DELIVERY──► Delivery
        so_id = delivery_to_so.get(did)
        if so_id:
            neo4j_client.merge_relationship("SalesOrder", so_id, "Delivery", did, "HAS_DELIVERY")
            rel_count += 1

    counts["Delivery"] = len(deliveries)
    logger.info("  ✔ %d Deliveries  (SO→Delivery links: %d)", len(deliveries), len(delivery_to_so))

    # 7. Billing Documents + FULFILLED_BY links from billing_items
    billings = _load("billing_document_cancellations")
    billing_items = _load("billing_document_items")
    billing_acct_map: Dict[str, str] = {}

    # Build billingDoc → deliveryDoc map from billing_items
    billing_to_del: Dict[str, str] = {}
    for bi in billing_items:
        bdoc = str(bi.get("billingDocument", ""))
        ddoc = str(bi.get("referenceSdDocument", ""))
        if bdoc and ddoc and bdoc not in billing_to_del:
            billing_to_del[bdoc] = ddoc

    for b in billings:
        bid  = str(b["billingDocument"])
        acct = str(b.get("accountingDocument", ""))
        if acct:
            billing_acct_map[acct] = bid

        neo4j_client.merge_node("BillingDocument", bid, {
            "id":             bid,
            "type":           b.get("billingDocumentType", ""),
            "creationDate":   str(b.get("creationDate", "")),
            "totalNetAmount": float(b.get("totalNetAmount") or 0),
            "currency":       b.get("transactionCurrency", ""),
            "accountingDocument": acct,
            "isCancelled":    bool(b.get("billingDocumentIsCancelled", False)),
        })
        cid = str(b.get("soldToParty", ""))
        if cid:
            neo4j_client.merge_relationship("BillingDocument", bid, "Customer", cid, "BILLED_TO")
            rel_count += 1
        # Delivery ──FULFILLED_BY──► BillingDocument
        ddoc = billing_to_del.get(bid)
        if ddoc:
            neo4j_client.merge_relationship("Delivery", ddoc, "BillingDocument", bid, "FULFILLED_BY")
            rel_count += 1

    counts["BillingDocument"] = len(billings)
    logger.info("  ✔ %d BillingDocuments  (Delivery→Billing links: %d)",
                len(billings), len(billing_to_del))

    # 8. Journal Entries
    journals = _load("journal_entry_items_accounts_receivable")
    seen_je: set = set()
    for j in journals:
        acct = str(j.get("accountingDocument", ""))
        item = str(j.get("accountingDocumentItem", "1"))
        jid  = f"{acct}_{item}"
        if jid in seen_je:
            continue
        seen_je.add(jid)
        neo4j_client.merge_node("JournalEntry", jid, {
            "id":               jid,
            "accountingDocument": acct,
            "amount":           float(j.get("amountInTransactionCurrency") or 0),
            "postingDate":      str(j.get("postingDate", "")),
            "glAccount":        j.get("glAccount", ""),
        })
        if acct in billing_acct_map:
            neo4j_client.merge_relationship(
                "BillingDocument", billing_acct_map[acct], "JournalEntry", jid, "POSTS_TO_JOURNAL"
            )
            rel_count += 1
    counts["JournalEntry"] = len(seen_je)
    logger.info("  ✔ %d JournalEntries", len(seen_je))

    # 9. Payments
    payments = _load("payments_accounts_receivable")
    seen_pay: set = set()
    for p in payments:
        acct = str(p.get("accountingDocument", ""))
        item = str(p.get("accountingDocumentItem", "1"))
        pid  = f"{acct}_{item}"
        if pid in seen_pay:
            continue
        seen_pay.add(pid)
        neo4j_client.merge_node("Payment", pid, {
            "id":              pid,
            "accountingDocument": acct,
            "amount":          float(p.get("amountInTransactionCurrency") or 0),
            "clearingDate":    str(p.get("clearingDate", "")),
            "customer":        str(p.get("customer", "")),
        })
        if acct in billing_acct_map:
            neo4j_client.merge_relationship(
                "BillingDocument", billing_acct_map[acct], "Payment", pid, "SETTLED_BY"
            )
            rel_count += 1
    counts["Payment"] = len(seen_pay)
    logger.info("  ✔ %d Payments", len(seen_pay))

    logger.info("Ingestion complete. Nodes: %s | Relationships: %d", counts, rel_count)
    return counts, rel_count
