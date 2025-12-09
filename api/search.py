from http.server import BaseHTTPRequestHandler
import json
import os
import psycopg2
from urllib.parse import parse_qs, urlparse
import voyageai
import anthropic

# Clients
vo = voyageai.Client(api_key=os.environ.get("VOYAGE_API_KEY"))
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def get_db():
    return psycopg2.connect(os.environ.get("NEON_DATABASE_URL"))

def extract_name_from_query(query: str) -> dict:
    """Usa Claude per estrarre nomi di artisti/autori dalla query."""
    
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Analizza questa query di ricerca libri: "{query}"

Se la query cerca libri DI o SU una persona specifica (artista, fotografo, autore, critico), estrai il nome.

Rispondi SOLO in JSON:
- Se c'è un nome: {{"tipo": "nome", "nome": "Nome Cognome"}}
- Se è una ricerca tematica: {{"tipo": "tematica", "tema": "descrizione breve"}}

Esempi:
"tutti i libri di Bruce Nauman" → {{"tipo": "nome", "nome": "Bruce Nauman"}}
"fotografia giapponese anni 70" → {{"tipo": "tematica", "tema": "fotografia giapponese anni 70"}}
"cataloghi di Luigi Ghirri" → {{"tipo": "nome", "nome": "Luigi Ghirri"}}
"arte concettuale" → {{"tipo": "tematica", "tema": "arte concettuale"}}

JSON:"""
        }]
    )
    
    try:
        return json.loads(response.content[0].text.strip())
    except:
        return {"tipo": "tematica", "tema": query}

def search_by_name(name: str, limit: int = 100) -> dict:
    """Cerca tutti i libri collegati a un nome, con ranking."""
    
    conn = get_db()
    cur = conn.cursor()
    
    # Prepara entrambe le forme: "Nome Cognome" e "Cognome Nome"
    name_lower = name.lower().strip()
    parts = name_lower.split()
    
    if len(parts) >= 2:
        # "Bruce Nauman" -> cerca anche "Nauman Bruce"
        reversed_name = " ".join(reversed(parts))
        pattern_original = f"%{name_lower}%"
        pattern_reversed = f"%{reversed_name}%"
    else:
        # Solo un nome/cognome
        pattern_original = f"%{name_lower}%"
        pattern_reversed = pattern_original
    
    # 1. Monografie: libri dove è l'unico artista E nome/cognome nel titolo
    cur.execute("""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione, 
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               1 as ranking, 'monografia_titolo' as tipo
        FROM public.books b
        JOIN public.book_artists ba ON b.id = ba.book_id
        WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
          AND (LOWER(b.titolo) LIKE %s OR LOWER(b.titolo) LIKE %s)
          AND (SELECT COUNT(*) FROM public.book_artists ba2 WHERE ba2.book_id = b.id) = 1
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed))
    monografie_titolo = cur.fetchall()
    
    # 2. Monografie: unico artista, nome non nel titolo
    cur.execute("""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               2 as ranking, 'monografia' as tipo
        FROM public.books b
        JOIN public.book_artists ba ON b.id = ba.book_id
        WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
          AND LOWER(b.titolo) NOT LIKE %s AND LOWER(b.titolo) NOT LIKE %s
          AND (SELECT COUNT(*) FROM public.book_artists ba2 WHERE ba2.book_id = b.id) = 1
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed))
    monografie = cur.fetchall()
    
    # 3. Collettive: più artisti
    cur.execute("""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               3 as ranking, 'collettiva' as tipo
        FROM public.books b
        JOIN public.book_artists ba ON b.id = ba.book_id
        WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
          AND (SELECT COUNT(*) FROM public.book_artists ba2 WHERE ba2.book_id = b.id) > 1
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed))
    collettive = cur.fetchall()
    
    # 4. Come autore (testi critici, saggi)
    cur.execute("""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               4 as ranking, 'autore' as tipo
        FROM public.books b
        JOIN public.book_authors bau ON b.id = bau.book_id
        WHERE (LOWER(bau.author) LIKE %s OR LOWER(bau.author) LIKE %s)
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed))
    come_autore = cur.fetchall()
    
    # 5. Altri libri che menzionano l'artista in descrizione/titolo (non già trovati)
    found_ids = [r[0] for r in monografie_titolo + monografie + collettive + come_autore]
    if found_ids:
        cur.execute("""
            SELECT b.id, b.titolo, b.editore, b.anno, b.descrizione,
                   b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
                   5 as ranking, 'menzione' as tipo
            FROM public.books b
            WHERE (LOWER(b.descrizione) LIKE %s OR LOWER(b.descrizione) LIKE %s
                   OR LOWER(b.titolo) LIKE %s OR LOWER(b.titolo) LIKE %s)
              AND b.id NOT IN %s
            ORDER BY b.anno DESC
            LIMIT 50
        """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed, tuple(found_ids)))
    else:
        cur.execute("""
            SELECT b.id, b.titolo, b.editore, b.anno, b.descrizione,
                   b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
                   5 as ranking, 'menzione' as tipo
            FROM public.books b
            WHERE (LOWER(b.descrizione) LIKE %s OR LOWER(b.descrizione) LIKE %s
                   OR LOWER(b.titolo) LIKE %s OR LOWER(b.titolo) LIKE %s)
            ORDER BY b.anno DESC
            LIMIT 50
        """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed))
    citazioni = cur.fetchall()
    
    cur.close()
    conn.close()
    
    # Formatta risultati
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'prezzo', 
               'pagine', 'lingua', 'immagine', 'isbn', 'ranking', 'tipo']
    
    return {
        'monografie_titolo': [dict(zip(columns, r)) for r in monografie_titolo],
        'monografie': [dict(zip(columns, r)) for r in monografie],
        'collettive': [dict(zip(columns, r)) for r in collettive],
        'come_autore': [dict(zip(columns, r)) for r in come_autore],
        'citazioni': [dict(zip(columns, r)) for r in citazioni],
        'totale': len(monografie_titolo) + len(monografie) + len(collettive) + len(come_autore) + len(citazioni)
    }

def search_semantic(query: str, limit: int = 10) -> list:
    """Ricerca semantica classica."""
    
    result = vo.embed([query], model="voyage-3-lite", input_type="query")
    query_embedding = result.embeddings[0]
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            id, titolo, editore, anno, descrizione, 
            prezzo_def_euro_web, pagine, lingua, permalinkimmagine, isbn_expo,
            1 - (embedding <=> %s::vector) as similarity
        FROM public.books
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, (query_embedding, query_embedding, limit))
    
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'prezzo', 
               'pagine', 'lingua', 'immagine', 'isbn', 'similarity']
    results = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return results

def generate_response_for_name(name: str, results: dict) -> str:
    """Genera risposta per ricerca per nome."""
    
    if results['totale'] == 0:
        return f"Non ho trovato pubblicazioni relative a {name} nel catalogo."
    
    # Costruisci contesto per Claude
    context_parts = []
    
    # Raccogli tutti i libri con ID per il post-processing
    all_books = []
    
    if results['monografie_titolo']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['monografie_titolo'][:5]]
        context_parts.append(f"MONOGRAFIE DEDICATE - titolo con nome artista ({len(results['monografie_titolo'])} titoli):\n" + "\n".join(titles))
        all_books.extend(results['monografie_titolo'][:5])
    
    if results['monografie']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['monografie'][:5]]
        context_parts.append(f"ALTRE MONOGRAFIE ({len(results['monografie'])} titoli):\n" + "\n".join(titles))
        all_books.extend(results['monografie'][:5])
    
    if results['collettive']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['collettive'][:5]]
        context_parts.append(f"CATALOGHI COLLETTIVI con l'artista ({len(results['collettive'])} titoli):\n" + "\n".join(titles))
        all_books.extend(results['collettive'][:5])
    
    if results['come_autore']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['come_autore'][:3]]
        context_parts.append(f"LIBRI SCRITTI DALL'ARTISTA ({len(results['come_autore'])} titoli):\n" + "\n".join(titles))
        all_books.extend(results['come_autore'][:3])
    
    if results['citazioni']:
        context_parts.append(f"ALTRI LIBRI che menzionano l'artista: {len(results['citazioni'])} titoli")
    
    context = "\n\n".join(context_parts)
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Sei un libraio specializzato in arte contemporanea. L'utente cerca pubblicazioni su: {name}

LIBRI DISPONIBILI NEL NOSTRO CATALOGO:
{context}

TOTALE: {results['totale']} pubblicazioni in vendita

Scrivi una risposta in 3-4 paragrafi separati da righe vuote.

Primo paragrafo: sintesi dei titoli disponibili (quante monografie, cataloghi collettivi, ecc.)

Secondo paragrafo: descrivi le monografie più significative disponibili, con titolo tra virgolette, editore e anno.

Terzo paragrafo: menzione dei cataloghi collettivi più rilevanti, con titolo tra virgolette.

Tono: professionale ma commerciale, stai presentando libri in vendita.
Rispondi nella lingua dell'utente."""
        }]
    )
    
    response_text = message.content[0].text
    
    # Post-processing: sostituisci i titoli con link
    for book in all_books:
        titolo = book['titolo']
        book_id = book['id']
        # Cerca il titolo tra virgolette e sostituiscilo con un link
        link = f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{titolo}</a>'
        # Sostituisci "Titolo" con il link
        response_text = response_text.replace(f'"{titolo}"', link)
        response_text = response_text.replace(f'«{titolo}»', link)
    
    return response_text

def generate_response_semantic(query: str, results: list) -> str:
    """Genera risposta per ricerca semantica."""
    
    if not results:
        return "Nessun risultato trovato per questa ricerca."
    
    books_context = "\n".join([
        f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})"
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

Scrivi 3-4 paragrafi separati da righe vuote.
Tono professionale, da biblioteca di ricerca.
Cita i titoli tra virgolette.
Rispondi nella lingua dell'utente."""
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
                "message": "Libro Search API v2. Usa ?q=query per cercare."
            }).encode())
            return
        
        try:
            # Estrai tipo di query
            query_info = extract_name_from_query(query)
            
            if query_info.get('tipo') == 'nome':
                # Ricerca per nome
                name = query_info['nome']
                results = search_by_name(name, limit)
                risposta = generate_response_for_name(name, results)
                
                # Combina tutti i risultati per la lista
                all_results = (
                    results['monografie_titolo'] + 
                    results['monografie'] + 
                    results['collettive'] + 
                    results['come_autore'] + 
                    results['citazioni'][:20]
                )
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "nome",
                    "nome_cercato": name,
                    "risposta": risposta,
                    "risultati": all_results,
                    "conteggi": {
                        "monografie": len(results['monografie_titolo']) + len(results['monografie']),
                        "collettive": len(results['collettive']),
                        "come_autore": len(results['come_autore']),
                        "citazioni": len(results['citazioni']),
                        "totale": results['totale']
                    }
                }, default=str).encode())
            else:
                # Ricerca semantica
                results = search_semantic(query, limit)
                risposta = generate_response_semantic(query, results)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "semantica",
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
            limit = data.get('limit', 50)
            
            if not query:
                self.wfile.write(json.dumps({
                    "error": "Query richiesta"
                }).encode())
                return
            
            # Estrai tipo di query
            query_info = extract_name_from_query(query)
            
            if query_info.get('tipo') == 'nome':
                name = query_info['nome']
                results = search_by_name(name, limit)
                risposta = generate_response_for_name(name, results)
                
                all_results = (
                    results['monografie_titolo'] + 
                    results['monografie'] + 
                    results['collettive'] + 
                    results['come_autore'] + 
                    results['citazioni'][:20]
                )
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "nome",
                    "nome_cercato": name,
                    "risposta": risposta,
                    "risultati": all_results,
                    "conteggi": {
                        "monografie": len(results['monografie_titolo']) + len(results['monografie']),
                        "collettive": len(results['collettive']),
                        "come_autore": len(results['come_autore']),
                        "citazioni": len(results['citazioni']),
                        "totale": results['totale']
                    }
                }, default=str).encode())
            else:
                results = search_semantic(query, limit)
                risposta = generate_response_semantic(query, results)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "semantica",
                    "risposta": risposta,
                    "risultati": results
                }, default=str).encode())
                
        except Exception as e:
            self.wfile.write(json.dumps({
                "error": str(e)
            }).encode())
