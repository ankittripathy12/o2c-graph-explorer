"""
Standalone seed script — run this instead of the API endpoint
if you prefer command-line ingestion.

Usage:
    cd o2c-backend
    python scripts/seed.py
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

from app.database.neo4j_client import neo4j_client
from app.services.ingestion import ingest_all

if __name__ == "__main__":
    print("\n  SAP O2C — Graph Seed Script\n")
    neo4j_client.connect()
    neo4j_client.create_constraints()

    print("Clearing existing graph …")
    neo4j_client.clear_all()

    counts, rel_count = ingest_all()

    print("\n  Done!")
    print("   Nodes created:")
    for label, n in counts.items():
        print(f"     {label:<20} {n}")
    print(f"   Relationships:       {rel_count}")
    neo4j_client.close()
