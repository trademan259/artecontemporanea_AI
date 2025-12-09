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
    """Usa Claude per estrarre nomi di artisti/autori e filtri dalla query."""
    
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Analizza questa query di ricerca libri: "{query}"

Estrai:
1. Se cerca libri DI o SU una persona specifica (artista, fotografo, autore, critico)
2. Eventuali filtri: lingua, anno, periodo

Rispondi SOLO in JSON con questi campi:
- tipo: "nome" o "tematica"
- nome: "Nome Cognome" (se tipo=nome)
- tema: "descrizione" (se tipo=tematica)
- lingua: "EN", "IT", "DE", "FR", "JP", etc. (se specificata)
- anno_min: numero (se specificato)
- anno_max: numero (se specificato)

Esempi:
"tutti i libri di Bruce Nauman" → {{"tipo": "nome", "nome": "Bruce Nauman"}}
"Luigi Ghirri in inglese" → {{"tipo": "nome", "nome": "Luigi Ghirri", "lingua": "EN"}}
"Cindy Sherman libri italiani" → {{"tipo": "nome", "nome": "Cindy Sherman", "lingua": "IT"}}
"fotografia giapponese anni 70" → {{"tipo": "tematica", "tema": "fotografia giapponese", "anno_min": 1970, "anno_max": 1979}}
"Gerhard Richter dopo il 2000" → {{"tipo": "nome", "nome": "Gerhard Richter", "anno_min": 2000}}

JSON:"""
        }]
    )
    
    try:
        return json.loads(response.content[0].text.strip())
    except:
        return {"tipo": "tematica", "tema": query}

def search_by_name(name: str, filters: dict = None, limit: int = 100) -> dict:
    """Cerca tutti i libri collegati a un nome, con ranking e filtri."""
    
    conn = get_db()
    cur = conn.cursor()
    
    filters = filters or {}
    
    # Prepara entrambe le forme: "Nome Cognome" e "Cognome Nome"
    name_lower = name.lower().strip()
    parts = name_lower.split()
    
    if len(parts) >= 2:
        reversed_name = " ".join(reversed(parts))
        pattern_original = f"%{name_lower}%"
        pattern_reversed = f"%{reversed_name}%"
    else:
        pattern_original = f"%{name_lower}%"
        pattern_reversed = pattern_original
    
    # Costruisci filtri aggiuntivi
    extra_conditions = ""
    extra_params = []
    
    if filters.get('lingua'):
        extra_conditions += " AND LOWER(b.lingua) LIKE %s"
        extra_params.append(f"%{filters['lingua'].lower()}%")
    
    if filters.get('anno_min'):
        extra_conditions += " AND b.anno >= %s"
        extra_params.append(str(filters['anno_min']))
    
    if filters.get('anno_max'):
        extra_conditions += " AND b.anno <= %s"
        extra_params.append(str(filters['anno_max']))
    
    # 1. Monografie: libri dove è l'unico artista E nome/cognome nel titolo
    cur.execute(f"""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione, 
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               1 as ranking, 'monografia_titolo' as tipo
        FROM public.books b
        JOIN public.book_artists ba ON b.id = ba.book_id
        WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
          AND (LOWER(b.titolo) LIKE %s OR LOWER(b.titolo) LIKE %s)
          AND (SELECT COUNT(*) FROM public.book_artists ba2 WHERE ba2.book_id = b.id) = 1
          {extra_conditions}
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed) + tuple(extra_params))
    monografie_titolo = cur.fetchall()
    
    # 2. Monografie: unico artista, nome non nel titolo
    cur.execute(f"""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               2 as ranking, 'monografia' as tipo
        FROM public.books b
        JOIN public.book_artists ba ON b.id = ba.book_id
        WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
          AND LOWER(b.titolo) NOT LIKE %s AND LOWER(b.titolo) NOT LIKE %s
          AND (SELECT COUNT(*) FROM public.book_artists ba2 WHERE ba2.book_id = b.id) = 1
          {extra_conditions}
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed) + tuple(extra_params))
    monografie = cur.fetchall()
    
    # 3. Collettive: più artisti
    cur.execute(f"""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               3 as ranking, 'collettiva' as tipo
        FROM public.books b
        JOIN public.book_artists ba ON b.id = ba.book_id
        WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
          AND (SELECT COUNT(*) FROM public.book_artists ba2 WHERE ba2.book_id = b.id) > 1
          {extra_conditions}
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed) + tuple(extra_params))
    collettive = cur.fetchall()
    
    # 4. Come autore (testi critici, saggi)
    cur.execute(f"""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               4 as ranking, 'autore' as tipo
        FROM public.books b
        JOIN public.book_authors bau ON b.id = bau.book_id
        WHERE (LOWER(bau.author) LIKE %s OR LOWER(bau.author) LIKE %s)
          {extra_conditions}
        ORDER BY b.anno DESC
    """, (pattern_original, pattern_reversed) + tuple(extra_params))
    come_autore = cur.fetchall()
    
    # 5. Altri libri che menzionano l'artista in descrizione/titolo (non già trovati)
    found_ids = [r[0] for r in monografie_titolo + monografie + collettive + come_autore]
    if found_ids:
        cur.execute(f"""
            SELECT b.id, b.titolo, b.editore, b.anno, b.descrizione,
                   b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
                   5 as ranking, 'menzione' as tipo
            FROM public.books b
            WHERE (LOWER(b.descrizione) LIKE %s OR LOWER(b.descrizione) LIKE %s
                   OR LOWER(b.titolo) LIKE %s OR LOWER(b.titolo) LIKE %s)
              AND b.id NOT IN %s
              {extra_conditions}
            ORDER BY b.anno DESC
            LIMIT 50
        """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed, tuple(found_ids)) + tuple(extra_params))
    else:
        cur.execute(f"""
            SELECT b.id, b.titolo, b.editore, b.anno, b.descrizione,
                   b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
                   5 as ranking, 'menzione' as tipo
            FROM public.books b
            WHERE (LOWER(b.descrizione) LIKE %s OR LOWER(b.descrizione) LIKE %s
                   OR LOWER(b.titolo) LIKE %s OR LOWER(b.titolo) LIKE %s)
              {extra_conditions}
            ORDER BY b.anno DESC
            LIMIT 50
        """, (pattern_original, pattern_reversed, pattern_original, pattern_reversed) + tuple(extra_params))
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

def generate_response_for_name(name: str, results: dict, filters: dict = None) -> str:
    """Genera risposta per ricerca per nome."""
    
    filters = filters or {}
    
    if results['totale'] == 0:
        filter_msg = ""
        if filters.get('lingua'):
            filter_msg = f" in lingua {filters['lingua']}"
        if filters.get('anno_min') or filters.get('anno_max'):
            if filters.get('anno_min') and filters.get('anno_max'):
                filter_msg += f" dal {filters['anno_min']} al {filters['anno_max']}"
            elif filters.get('anno_min'):
                filter_msg += f" dal {filters['anno_min']}"
            elif filters.get('anno_max'):
                filter_msg += f" fino al {filters['anno_max']}"
        return f"Non ho trovato pubblicazioni relative a {name}{filter_msg} nel catalogo."
    
    # Costruisci contesto per Claude
    context_parts = []
    
    # Aggiungi info sui filtri applicati
    filter_info = []
    if filters.get('lingua'):
        filter_info.append(f"lingua: {filters['lingua']}")
    if filters.get('anno_min'):
        filter_info.append(f"dal {filters['anno_min']}")
    if filters.get('anno_max'):
        filter_info.append(f"fino al {filters['anno_max']}")
    
    if filter_info:
        context_parts.append(f"FILTRI APPLICATI: {', '.join(filter_info)}")
    
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
                # Ricerca per nome con filtri
                name = query_info['nome']
                filters = {k: v for k, v in query_info.items() if k in ['lingua', 'anno_min', 'anno_max']}
                results = search_by_name(name, filters, limit)
                risposta = generate_response_for_name(name, results, filters)
                
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
                filters = {k: v for k, v in query_info.items() if k in ['lingua', 'anno_min', 'anno_max']}
                results = search_by_name(name, filters, limit)
                risposta = generate_response_for_name(name, results, filters)
                
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
