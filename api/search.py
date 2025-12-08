from http.server import BaseHTTPRequestHandler
import json
import os
import psycopg2
from urllib.parse import parse_qs, urlparse
import voyageai
import anthropic

# Clients (inizializzati una volta per container)
vo = voyageai.Client(api_key=os.environ.get("VOYAGE_API_KEY"))
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def get_db():
    return psycopg2.connect(os.environ.get("NEON_DATABASE_URL"))

def search_books(query: str, limit: int = 10) -> list:
    result = vo.embed([query], model="voyage-3-lite", input_type="query")
    query_embedding = result.embeddings[0]
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            id, titolo, editore, anno, descrizione, tag, 
            prezzo_def_euro_web, pagine, lingua, rilegatura, 
            formato, permalinkimmagine, isbn_expo,
            1 - (embedding <=> %s::vector) as similarity
        FROM public.books
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, (query_embedding, query_embedding, limit))
    
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'tag', 
               'prezzo', 'pagine', 'lingua', 'rilegatura', 'formato', 
               'immagine', 'isbn', 'similarity']
    results = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return results

def generate_response(query: str, results: list) -> str:
    if not results:
        return "Nessun risultato trovato con i criteri specificati."
    
    books_context = "\n".join([
        f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']}) [{r['lingua']}] - {r['descrizione'][:150] if r['descrizione'] else 'Nessuna descrizione'}..."
        for r in results[:7]
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""Sei un archivista specializzato in libri d'arte, fotografia e pubblicazioni rare.
L'utente cerca: "{query}"

Risultati dal catalogo:
{books_context}

Rispondi con tono professionale e sobrio, da archivio o biblioteca di ricerca.
Presenta i risultati in modo conciso e informativo, senza enfasi commerciale.
Evidenzia pertinenza bibliografica, rarità o rilevanza storica se presenti.
Non fare domande, non usare punti esclamativi, evita formule di vendita.
Rispondi nella stessa lingua usata dall'utente nella query."""
        }]
    )
    
    return message.content[0].text

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        query = params.get('q', [''])[0]
        limit = int(params.get('limit', ['10'])[0])
        
        if not query:
            self.wfile.write(json.dumps({
                "status": "ok",
                "message": "Libro Search API. Usa ?q=query per cercare."
            }).encode())
            return
        
        try:
            results = search_books(query, limit)
            risposta = generate_response(query, results)
            
            self.wfile.write(json.dumps({
                "risposta": risposta,
                "risultati": results
            }, default=str).encode())
        except Exception as e:
            self.wfile.write(json.dumps({
                "error": str(e)
            }).encode())
    
    def do_POST(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            query = data.get('query', '')
            limit = data.get('limit', 10)
            
            if not query:
                self.wfile.write(json.dumps({
                    "error": "Query richiesta"
                }).encode())
                return
            
            results = search_books(query, limit)
            risposta = generate_response(query, results)
            
            self.wfile.write(json.dumps({
                "risposta": risposta,
                "risultati": results
            }, default=str).encode())
        except Exception as e:
            self.wfile.write(json.dumps({
                "error": str(e)
            }).encode())
