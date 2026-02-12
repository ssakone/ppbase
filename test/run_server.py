#!/usr/bin/env python3
"""Script pour démarrer le serveur PPBase via import.

Usage:
    python test/run_server.py
    # ou depuis test/
    cd test && python run_server.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ajouter la racine du projet au path pour importer ppbase
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

if __name__ == "__main__":
    from ppbase import PPBase

    pb = PPBase()
    print("Démarrage PPBase...")
    print("  Admin UI: http://127.0.0.1:8090/_/")
    print("  API:      http://127.0.0.1:8090/api/")
    print("  Ctrl+C pour arrêter")
    pb.start(host="127.0.0.1", port=8090)
