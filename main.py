from http.server import HTTPServer
import sys
import os

# Aggiungi la cartella api al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'api'))

from search import handler

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), handler)
    print(f"Server running on port {port}")
    server.serve_forever()