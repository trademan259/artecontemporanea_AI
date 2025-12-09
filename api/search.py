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

def extract_name_from_query(query: str, context: dict = None) -> dict:
    """Usa Claude per estrarre nomi di artisti/autori, titoli e filtri dalla query, considerando il contesto."""
    
    context = context or {}
    context_info = ""
    
    if context.get('previousSearch'):
        context_info = f"""
CONTESTO CONVERSAZIONE PRECEDENTE:
- Ultima ricerca: "{context.get('previousSearch')}"
- Filtri applicati: {context.get('previousFilters', {})}

Se l'utente sta raffinando la ricerca precedente (es. "solo in inglese", "mostrami le monografie", "dopo il 2000"), 
mantieni il nome della ricerca precedente e aggiungi/modifica i filtri.
"""
    
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Analizza questa query di ricerca libri: "{query}"
{context_info}
Estrai:
1. Se cerca un TITOLO SPECIFICO di libro (es. "hai il libro X", "cerco il catalogo Y", titolo tra virgolette)
2. Se cerca libri DI o SU una persona specifica (artista, fotografo, autore)
3. Se è una ricerca tematica generica
4. Eventuali filtri: lingua, anno, periodo, tipo (monografia/collettiva)
5. Se è un follow-up della ricerca precedente

Rispondi SOLO con un oggetto JSON valido (niente altro testo):
- tipo: "titolo" o "nome" o "tematica" o "followup"
- titolo: "titolo cercato" (se tipo=titolo)
- nome: "Nome Cognome" (se tipo=nome)
- tema: "descrizione" (se tipo=tematica)
- lingua: "EN", "IT", "DE", "FR", "JP", etc. (se specificata)
- anno_min: numero (se specificato)
- anno_max: numero (se specificato)
- tipo_pub: "monografia" o "collettiva" o "autore" (se l'utente chiede un tipo specifico)

Esempi:
"Bruce Nauman. Inventa e muori" → {{"tipo": "titolo", "titolo": "Inventa e muori"}}
"hai il catalogo When attitudes become form?" → {{"tipo": "titolo", "titolo": "When attitudes become form"}}
"cerco Live in your head" → {{"tipo": "titolo", "titolo": "Live in your head"}}
"Bruce Nauman" → {{"tipo": "nome", "nome": "Bruce Nauman"}}
"libri di Bruce Nauman" → {{"tipo": "nome", "nome": "Bruce Nauman"}}
"solo in inglese" (dopo ricerca su Ghirri) → {{"tipo": "followup", "nome": "Luigi Ghirri", "lingua": "EN"}}
"fotografia giapponese anni 70" → {{"tipo": "tematica", "tema": "fotografia giapponese", "anno_min": 1970, "anno_max": 1979}}

IMPORTANTE: Se la query contiene un titolo specifico di libro (riconoscibile da maiuscole, punteggiatura, o parole come "catalogo", "libro", "hai"), usa tipo="titolo".

JSON:"""
        }]
    )
    
    try:
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        result = json.loads(text)
        
        # Se è un followup, usa il nome dal contesto se non specificato
        if result.get('tipo') == 'followup' and context.get('previousSearch'):
            if not result.get('nome') or result.get('nome') == '[nome precedente]':
                result['nome'] = context.get('previousSearch')
            result['tipo'] = 'nome'  # Trattalo come ricerca per nome
            
            # Merge dei filtri precedenti con i nuovi
            prev_filters = context.get('previousFilters', {})
            for key in ['lingua', 'anno_min', 'anno_max']:
                if key not in result and key in prev_filters:
                    result[key] = prev_filters[key]
        
        return result
    except:
        return {"tipo": "tematica", "tema": query}

def search_by_title(title: str, limit: int = 20) -> list:
    """Cerca libri per titolo esatto o parziale."""
    
    conn = get_db()
    cur = conn.cursor()
    
    title_lower = title.lower().strip()
    pattern = f"%{title_lower}%"
    
    cur.execute("""
        SELECT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo
        FROM public.books b
        WHERE LOWER(b.titolo) LIKE %s
        ORDER BY 
            CASE WHEN LOWER(b.titolo) = %s THEN 0
                 WHEN LOWER(b.titolo) LIKE %s THEN 1
                 ELSE 2 END,
            b.anno DESC
        LIMIT %s
    """, (pattern, title_lower, title_lower + '%', limit))
    
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'prezzo', 
               'pagine', 'lingua', 'immagine', 'isbn']
    results = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return results

def generate_response_for_title(title: str, results: list) -> str:
    """Genera risposta per ricerca per titolo."""
    
    if not results:
        return f"Non ho trovato libri con titolo \"{title}\". Prova con parole chiave diverse o cerca per autore/artista."
    
    # Raccogli libri per post-processing
    all_books = results[:10]
    
    # Prepara contesto con ID
    books_context = "\n".join([
        f"- ID:{r['id']} | \"{r['titolo']}\" ({r['editore']}, {r['anno']}) - Lingua: {r['lingua']}"
        for r in all_books
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Sei un bibliotecario specializzato in libri d'arte.

L'utente cerca il titolo: "{title}"

RISULTATI TROVATI ({len(results)} titoli):
{books_context}

ISTRUZIONI:
- Se c'è un match esatto o molto simile, conferma: "Sì, abbiamo [titolo]"
- QUANDO CITI UN LIBRO, USA ESATTAMENTE QUESTO FORMATO: [[ID:xxx|Titolo del libro]]
- Elenca i risultati trovati con editore, anno e lingua
- Se ci sono più risultati, chiedi se l'utente cerca una edizione specifica
- Risposte brevi e dirette"""
        }]
    )
    
    response_text = message.content[0].text
    
    # Post-processing: converti [[ID:xxx|Titolo]] in link HTML
    import re
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    
    return response_text

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
    
    # Filtro per tipo di pubblicazione
    tipo_pub = filters.get('tipo_pub')
    
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
    
    result_dict = {
        'monografie_titolo': [dict(zip(columns, r)) for r in monografie_titolo],
        'monografie': [dict(zip(columns, r)) for r in monografie],
        'collettive': [dict(zip(columns, r)) for r in collettive],
        'come_autore': [dict(zip(columns, r)) for r in come_autore],
        'citazioni': [dict(zip(columns, r)) for r in citazioni],
    }
    
    # Filtra per tipo di pubblicazione se richiesto
    if tipo_pub:
        if tipo_pub == 'monografia':
            result_dict['collettive'] = []
            result_dict['come_autore'] = []
            result_dict['citazioni'] = []
        elif tipo_pub == 'collettiva':
            result_dict['monografie_titolo'] = []
            result_dict['monografie'] = []
            result_dict['come_autore'] = []
            result_dict['citazioni'] = []
        elif tipo_pub == 'autore':
            result_dict['monografie_titolo'] = []
            result_dict['monografie'] = []
            result_dict['collettive'] = []
            result_dict['citazioni'] = []
    
    result_dict['totale'] = (len(result_dict['monografie_titolo']) + 
                             len(result_dict['monografie']) + 
                             len(result_dict['collettive']) + 
                             len(result_dict['come_autore']) + 
                             len(result_dict['citazioni']))
    
    # Calcola filtri disponibili per i bottoni
    all_results = (result_dict['monografie_titolo'] + result_dict['monografie'] + 
                   result_dict['collettive'] + result_dict['come_autore'] + result_dict['citazioni'])
    
    # Lingue disponibili
    lingue = {}
    for r in all_results:
        lang = r.get('lingua', '').strip().upper()
        if lang:
            # Normalizza le lingue comuni
            if lang in ['I', 'IT', 'ITA', 'ITALIANO']:
                lang = 'IT'
            elif lang in ['E', 'EN', 'ENG', 'ENGLISH']:
                lang = 'EN'
            elif lang in ['D', 'DE', 'DEU', 'DEUTSCH']:
                lang = 'DE'
            elif lang in ['F', 'FR', 'FRA', 'FRANCAIS']:
                lang = 'FR'
            lingue[lang] = lingue.get(lang, 0) + 1
    
    # Anni - trova range
    anni = [int(r.get('anno', 0)) for r in all_results if r.get('anno') and str(r.get('anno')).isdigit()]
    anno_min = min(anni) if anni else None
    anno_max = max(anni) if anni else None
    
    result_dict['filtri_disponibili'] = {
        'lingue': dict(sorted(lingue.items(), key=lambda x: -x[1])),  # Ordinate per frequenza
        'tipi': {
            'monografia': len(result_dict['monografie_titolo']) + len(result_dict['monografie']),
            'collettiva': len(result_dict['collettive']),
            'autore': len(result_dict['come_autore'])
        },
        'anni': {'min': anno_min, 'max': anno_max}
    }
    
    return result_dict

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
        return f"Non ho trovato pubblicazioni su {name}{filter_msg}. Vuoi provare senza filtri o cercare un nome simile?"
    
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
    
    # Conteggi
    n_mono = len(results['monografie_titolo']) + len(results['monografie'])
    n_coll = len(results['collettive'])
    n_autore = len(results['come_autore'])
    n_citazioni = len(results['citazioni'])
    
    context_parts.append(f"""CONTEGGI:
- Monografie: {n_mono}
- Cataloghi collettivi: {n_coll}
- Scritti dell'artista: {n_autore}
- Menzioni in altri volumi: {n_citazioni}
- Totale: {results['totale']}""")
    
    # Raccogli tutti i libri con ID per il post-processing
    all_books = []
    
    if results['monografie_titolo']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['monografie_titolo'][:4]]
        context_parts.append(f"MONOGRAFIE PRINCIPALI:\n" + "\n".join(titles))
        all_books.extend(results['monografie_titolo'][:4])
    
    if results['monografie']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['monografie'][:3]]
        context_parts.append(f"ALTRE MONOGRAFIE:\n" + "\n".join(titles))
        all_books.extend(results['monografie'][:3])
    
    if results['collettive']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['collettive'][:3]]
        context_parts.append(f"CATALOGHI COLLETTIVI:\n" + "\n".join(titles))
        all_books.extend(results['collettive'][:3])
    
    if results['come_autore']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['come_autore'][:2]]
        context_parts.append(f"SCRITTI DALL'ARTISTA:\n" + "\n".join(titles))
        all_books.extend(results['come_autore'][:2])
    
    context = "\n\n".join(context_parts)
    
    # Prepara lista libri con ID per il formato link
    books_with_ids = "\n".join([
        f"ID:{b['id']} | {b['titolo']}"
        for b in all_books
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Sei un bibliotecario specializzato in libri d'arte, fotografia e illustrazione.

TONO:
- Informativo, preciso, disponibile
- Mai da venditore: niente aggettivi roboanti, niente enfasi promozionale
- Mai "eccellente", "straordinario", "imperdibile", "maestro"
- Dici i dati, offri opzioni, aiuti a trovare

L'utente cerca: {name}

DATI DAL CATALOGO:
{context}

LIBRI DISPONIBILI (usa questi ID per i link):
{books_with_ids}

ISTRUZIONI:
1. Inizia con i numeri: totale titoli, suddivisione per tipo
2. QUANDO CITI UN LIBRO, USA ESATTAMENTE QUESTO FORMATO: [[ID:xxx|Titolo del libro]]
   Esempio: [[ID:12345|Nome del Libro]]
3. Cita 3-5 titoli significativi usando il formato [[ID:xxx|Titolo]]
4. Concludi offrendo un filtro: "Filtro per periodo, lingua o tipo?"
5. Risposte brevi, max 3-4 righe per paragrafo
6. Rispondi nella lingua dell'utente"""
        }]
    )
    
    response_text = message.content[0].text
    
    # Post-processing: converti [[ID:xxx|Titolo]] in link HTML
    import re
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    
    return response_text

def generate_response_semantic(query: str, results: list) -> str:
    """Genera risposta per ricerca semantica (esplorativa)."""
    
    if not results:
        return "Non ho trovato risultati per questa ricerca. Prova con termini diversi o chiedimi un suggerimento su un tema specifico."
    
    # Raccogli libri per post-processing - includi ID nel contesto
    all_books = results[:7]
    
    # Prepara contesto CON ID per ogni libro
    books_context = "\n".join([
        f"- ID:{r['id']} | \"{r['titolo']}\" ({r['editore']}, {r['anno']})"
        for r in all_books
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Sei un bibliotecario specializzato in libri d'arte, fotografia e illustrazione.

TONO:
- Informativo, preciso, disponibile
- Mai da venditore: niente aggettivi roboanti, niente enfasi promozionale
- Mai "eccellente", "straordinario", "imperdibile", "maestro"
- Colloquiale ma competente

L'utente cerca: "{query}"

RISULTATI TROVATI:
{books_context}

ISTRUZIONI CRITICHE:
1. Questa è una ricerca esplorativa/tematica
2. Presenta brevemente cosa hai trovato
3. QUANDO CITI UN LIBRO, USA ESATTAMENTE QUESTO FORMATO: [[ID:xxx|Titolo del libro]]
   Esempio: [[ID:12345|Nome del Libro]]
4. Cita 3-5 libri usando il formato [[ID:xxx|Titolo]]
5. Suggerisci come affinare la ricerca o proponi direzioni correlate
6. Risposte brevi, conversazionali
7. Rispondi nella lingua dell'utente"""
        }]
    )
    
    response_text = message.content[0].text
    
    # Post-processing: converti [[ID:xxx|Titolo]] in link HTML
    import re
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    
    return response_text

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
            # Estrai tipo di query (GET non ha contesto)
            query_info = extract_name_from_query(query, None)
            
            if query_info.get('tipo') == 'titolo':
                # Ricerca per titolo
                title = query_info['titolo']
                results = search_by_title(title, limit)
                risposta = generate_response_for_title(title, results)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "titolo",
                    "titolo_cercato": title,
                    "risposta": risposta,
                    "risultati": results
                }, default=str).encode())
            elif query_info.get('tipo') == 'nome':
                # Ricerca per nome con filtri
                name = query_info['nome']
                filters = {k: v for k, v in query_info.items() if k in ['lingua', 'anno_min', 'anno_max', 'tipo_pub']}
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
                    "filtri": filters,
                    "risposta": risposta,
                    "risultati": all_results,
                    "filtri_disponibili": results.get('filtri_disponibili', {}),
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
            direct_filters = data.get('filters', None)  # Filtri diretti dai bottoni
            
            if not query:
                self.wfile.write(json.dumps({
                    "error": "Query richiesta"
                }).encode())
                return
            
            # Se ci sono filtri diretti, salta Claude e fai ricerca diretta per nome
            if direct_filters:
                name = query  # query è il nome dell'artista
                results = search_by_name(name, direct_filters, limit)
                
                # Genera risposta semplice SENZA Claude
                totale = results['totale']
                filter_desc = []
                if direct_filters.get('lingua'):
                    lang_names = {'IT': 'in italiano', 'EN': 'in inglese', 'DE': 'in tedesco', 'FR': 'in francese', 'ES': 'in spagnolo'}
                    filter_desc.append(lang_names.get(direct_filters['lingua'], f"in {direct_filters['lingua']}"))
                if direct_filters.get('tipo_pub'):
                    tipo_names = {'monografia': 'monografie', 'collettiva': 'cataloghi collettivi', 'autore': 'scritti come autore'}
                    filter_desc.append(tipo_names.get(direct_filters['tipo_pub'], direct_filters['tipo_pub']))
                if direct_filters.get('anno_min'):
                    filter_desc.append(f"dal {direct_filters['anno_min']}")
                if direct_filters.get('anno_max'):
                    filter_desc.append(f"fino al {direct_filters['anno_max']}")
                
                filter_text = ', '.join(filter_desc) if filter_desc else ''
                
                if totale == 0:
                    risposta = f"Nessun risultato per {name} {filter_text}. Prova a rimuovere qualche filtro."
                else:
                    risposta = f"{totale} risultati per {name} {filter_text}."
                
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
                    "filtri": direct_filters,
                    "risposta": risposta,
                    "risultati": all_results,
                    "filtri_disponibili": results.get('filtri_disponibili', {}),
                    "conteggi": {
                        "monografie": len(results['monografie_titolo']) + len(results['monografie']),
                        "collettive": len(results['collettive']),
                        "come_autore": len(results['come_autore']),
                        "citazioni": len(results['citazioni']),
                        "totale": results['totale']
                    }
                }, default=str).encode())
                return
            
            # Estrai tipo di query con contesto
            context = data.get('context', {})
            query_info = extract_name_from_query(query, context)
            
            if query_info.get('tipo') == 'titolo':
                # Ricerca per titolo
                title = query_info['titolo']
                results = search_by_title(title, limit)
                risposta = generate_response_for_title(title, results)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "titolo",
                    "titolo_cercato": title,
                    "risposta": risposta,
                    "risultati": results
                }, default=str).encode())
            elif query_info.get('tipo') == 'nome':
                name = query_info['nome']
                filters = {k: v for k, v in query_info.items() if k in ['lingua', 'anno_min', 'anno_max', 'tipo_pub']}
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
                    "filtri": filters,
                    "risposta": risposta,
                    "risultati": all_results,
                    "filtri_disponibili": results.get('filtri_disponibili', {}),
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
