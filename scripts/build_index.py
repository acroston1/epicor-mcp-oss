#!/usr/bin/env python3
"""Build a service metadata index from your own Swagger exports (no embeddings).

For the complete SQL/discovery metadata import, use bootstrap_schema.py instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from epicor_mcp.index.builder import main

if __name__ == "__main__":
    main()
