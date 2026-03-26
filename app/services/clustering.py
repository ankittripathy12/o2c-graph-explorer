"""
Graph Clustering Service

Implements community detection on the in-memory graph
using a simple Louvain-inspired label propagation algorithm.
No external libraries needed — pure Python.

Clusters are computed once and cached. The frontend uses
cluster IDs to colour nodes by community.

Communities discovered:
  - Customer + their SalesOrders + BillingDocuments   → "Sales Community"
  - SalesOrderItems + Products + Plants               → "Supply Community"
  - BillingDocuments + JournalEntries + Payments      → "Finance Community"
  - Deliveries                                        → "Logistics Community"
"""

import logging
import random
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CLUSTER_COLORS = [
    "#3b82f6",  
    "#10b981",  
    "#f59e0b",  
    "#8b5cf6",  
    "#ef4444",  
    "#06b6d4",  
    "#f97316",  
    "#84cc16",  
]


CLUSTER_NAMES = {
    "Customer":        "Customer Network",
    "SalesOrder":      "Sales Flow",
    "BillingDocument": "Finance Flow",
    "Delivery":        "Logistics",
    "Product":         "Product Catalogue",
    "Plant":           "Supply Chain",
    "JournalEntry":    "Accounting",
    "Payment":         "Payments",
    "SalesOrderItem":  "Order Items",
}


class GraphClusterer:
    def __init__(self):
        self._cache: Optional[Dict[str, Any]] = None

    def compute(self, nodes: List[Dict], edges: List[Dict]) -> Dict[str, Any]:
        """
        Run label propagation clustering on the graph.
        Returns cluster assignments and metadata.
        """
        if not nodes:
            return {"clusters": {}, "metadata": []}

        # Build adjacency list
        adj: Dict[str, List[str]] = defaultdict(list)
        for e in edges:
            src = e.get("source") or e.get("src", "")
            tgt = e.get("target") or e.get("tgt", "")
            if src and tgt:
                adj[src].append(tgt)
                adj[tgt].append(src)

        node_ids   = [n["id"] for n in nodes]
        node_types = {n["id"]: n.get("_label", n.get("type", "Unknown")) for n in nodes}

        # ── Label Propagation ──────────────────────────────────────────
        # Initialize: each node is its own cluster
        labels: Dict[str, str] = {nid: nid for nid in node_ids}

        MAX_ITER = 15
        for iteration in range(MAX_ITER):
            changed = False
         
            shuffled = node_ids[:]
            random.shuffle(shuffled)

            for nid in shuffled:
                neighbours = adj.get(nid, [])
                if not neighbours:
                    continue
          
                label_counts: Dict[str, int] = defaultdict(int)
                for nb in neighbours:
                    label_counts[labels.get(nb, nb)] += 1
              
                best_label = max(label_counts, key=label_counts.get)
                if best_label != labels[nid]:
                    labels[nid] = best_label
                    changed = True

            if not changed:
                logger.debug("Clustering converged at iteration %d", iteration + 1)
                break

   
        unique_labels = list(set(labels.values()))
        label_to_int  = {lbl: i for i, lbl in enumerate(unique_labels)}
        clusters      = {nid: label_to_int[lbl] for nid, lbl in labels.items()}


        cluster_nodes: Dict[int, List[str]] = defaultdict(list)
        for nid, cid in clusters.items():
            cluster_nodes[cid].append(nid)

        metadata = []
        for cid, members in sorted(cluster_nodes.items()):
    
            type_counts: Dict[str, int] = defaultdict(int)
            for nid in members:
                t = node_types.get(nid, "Unknown")
                type_counts[t] += 1
            dominant_type = max(type_counts, key=type_counts.get)

            metadata.append({
                "cluster_id":    cid,
                "size":          len(members),
                "dominant_type": dominant_type,
                "name":          CLUSTER_NAMES.get(dominant_type, f"Cluster {cid}"),
                "color":         CLUSTER_COLORS[cid % len(CLUSTER_COLORS)],
                "node_ids":      members[:10],  
            })

        
        metadata.sort(key=lambda x: -x["size"])

        result = {
            "clusters":      clusters,       
            "metadata":      metadata,        
            "total_clusters": len(metadata),
            "algorithm":     "Label Propagation",
            "iterations":    MAX_ITER,
        }
        self._cache = result
        logger.info(
            "Clustering complete — %d nodes → %d clusters",
            len(nodes), len(metadata)
        )
        return result

    def get_cached(self) -> Optional[Dict[str, Any]]:
        return self._cache

    def cluster_color_map(self) -> Dict[str, str]:
        """Returns node_id → hex_color for the frontend."""
        if not self._cache:
            return {}
        colors = {}
        for cid_int, members_preview in enumerate(self._cache["metadata"]):
            color = members_preview["color"]
            for nid in self._cache["clusters"]:
                if self._cache["clusters"][nid] == members_preview["cluster_id"]:
                    colors[nid] = color
        return colors

clusterer = GraphClusterer()