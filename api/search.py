from http.server import BaseHTTPRequestHandler
import json
import os
import psycopg2
from urllib.parse import parse_qs, urlparse
import voyageai
import anthropic
import re
import imagehash
from PIL import Image
from io import BytesIO
import base64

# Clients
vo = voyageai.Client(api_key=os.environ.get("VOYAGE_API_KEY"))
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def get_db():
    return psycopg2.connect(os.environ.get("NEON_DATABASE_URL"))

# ============ IMAGE HASH FUNCTIONS (NEW) ============

def compute_image_hash(image_base64: str) -> str:
    """Calcola l'hash percettivo di un'immagine in base64."""
    try:
        if ',' in image_base64:
            image_base64 = image_base64.split(',')[1]
        
        image_data = base64.b64decode(image_base64)
        img = Image.open(BytesIO(image_data))
        hash_value = str(imagehash.average_hash(img))
        img.close()
        return hash_value
    except Exception as e:
        print(f"Errore calcolo hash: {e}")
        return None

def compare_hashes(hash1: str, hash2: str) -> int:
    """Confronta due hash e restituisce la distanza di Hamming."""
    if not hash1 or not hash2:
        return 999
    try:
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        return h1 - h2
    except:
        return 999

def search_by_image_hybrid(query_info: dict, image_base64: str, limit: int = 50) -> dict:
    """Ricerca ibrida: combina ricerca testuale + confronto hash immagine."""
    
    user_hash = compute_image_hash(image_base64)
    
    conn = get_db()
    cur = conn.cursor()
    
    candidates = []
    search_term = query_info.get('titolo') or query_info.get('nome') or ''
    
    if search_term:
        search_pattern = f"%{search_term.lower()}%"
        
        cur.execute("""
            SELECT id, titolo, editore, anno, image_hash, permalinkimmagine
            FROM public.books 
            WHERE (LOWER(titolo) LIKE %s OR LOWER(descrizione) LIKE %s)
            AND image_hash IS NOT NULL
            LIMIT %s
        """, (search_pattern, search_pattern, limit))
        candidates.extend(cur.fetchall())
        
        if query_info.get('nome'):
            name_lower = query_info['nome'].lower()
            parts = name_lower.split()
            if len(parts) >= 2:
                reversed_name = " ".join(reversed(parts))
                pattern_original = f"%{name_lower}%"
                pattern_reversed = f"%{reversed_name}%"
            else:
                pattern_original = f"%{name_lower}%"
                pattern_reversed = pattern_original
            
            cur.execute("""
                SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.image_hash, b.permalinkimmagine
                FROM public.books b
                JOIN public.book_artists ba ON b.id = ba.book_id
                WHERE (LOWER(ba.artist) LIKE %s OR LOWER(ba.artist) LIKE %s)
                AND b.image_hash IS NOT NULL
                LIMIT %s
            """, (pattern_original, pattern_reversed, limit))
            candidates.extend(cur.fetchall())
    
    cur.close()
    conn.close()
    
    seen_ids = set()
    unique_candidates = []
    for c in candidates:
        if c[0] not in seen_ids:
            seen_ids.add(c[0])
            unique_candidates.append(c)
    
    results = []
    best_match = None
    
    for candidate in unique_candidates:
        book_id, titolo, editore, anno, db_hash, immagine = candidate
        
        distance = compare_hashes(user_hash, db_hash) if user_hash else 999
        
        result = {
            'id': book_id,
            'titolo': titolo,
            'editore': editore,
            'anno': anno,
            'immagine': immagine,
            'hash_distance': distance,
            'text_match': True,
            'image_match': distance <= 25,
            'confidence': 'alta' if distance <= 15 else ('media' if distance <= 25 else 'bassa')
        }
        results.append(result)
        
        if distance <= 25 and (best_match is None or distance < best_match['hash_distance']):
            best_match = result
    
    results.sort(key=lambda x: (not x['image_match'], x['hash_distance']))
    
    return {
        'candidati': results[:limit],
        'best_match': best_match,
        'user_hash': user_hash,
        'search_term': search_term,
        'total_candidates': len(unique_candidates)
    }

def generate_response_for_image_search(search_result: dict, query_info: dict) -> str:
    """Genera risposta per ricerca con immagine."""
    
    best_match = search_result.get('best_match')
    candidates = search_result.get('candidati', [])
    search_term = search_result.get('search_term', '')
    
    if best_match and best_match.get('confidence') in ['alta', 'media']:
        confidence_text = "con alta probabilità" if best_match['confidence'] == 'alta' else "probabilmente"
        response = f"""Ho identificato {confidence_text} il libro dalla copertina:

<a href="https://test01-frontend.vercel.app/books/{best_match['id']}" target="_blank"><strong>{best_match['titolo']}</strong></a>
{f"({best_match['editore']}, {best_match['anno']})" if best_match.get('editore') else ''}"""
        
        if len(candidates) > 1:
            other_matches = [c for c in candidates[:5] if c['id'] != best_match['id'] and c.get('image_match')]
            if other_matches:
                response += "\n\nAltri possibili match:"
                for m in other_matches[:3]:
                    response += f"\n• <a href=\"https://test01-frontend.vercel.app/books/{m['id']}\" target=\"_blank\">{m['titolo']}</a>"
        
        return response
    
    elif candidates:
        response = f"""Dalla copertina ho letto: "{search_term}"

Ho trovato {len(candidates)} possibili corrispondenze:"""
        for c in candidates[:5]:
            conf_icon = "✓" if c.get('image_match') else "?"
            response += f"\n{conf_icon} <a href=\"https://test01-frontend.vercel.app/books/{c['id']}\" target=\"_blank\">{c['titolo']}</a>"
        
        return response
    
    else:
        return f"""Non sono riuscito a identificare il libro dalla copertina.
        
Ho letto: "{search_term}"

Prova a scattare una foto più nitida o scrivi il titolo/autore."""

# ============ AUTOCOMPLETE / SUGGEST (NEW) ============

def get_suggestions(suggestion_type: str, query: str, limit: int = 10) -> list:
    """Restituisce suggerimenti per artisti o autori."""
    
    if len(query) < 2:
        return []
    
    conn = get_db()
    cur = conn.cursor()
    
    query_pattern = f"{query.lower()}%"
    query_contains = f"%{query.lower()}%"
    
    if suggestion_type == 'artist':
        cur.execute("""
            SELECT DISTINCT artist, COUNT(*) as cnt
            FROM public.book_artists
            WHERE LOWER(artist) LIKE %s OR LOWER(artist) LIKE %s
            GROUP BY artist
            ORDER BY 
                CASE WHEN LOWER(artist) LIKE %s THEN 0 ELSE 1 END,
                cnt DESC
            LIMIT %s
        """, (query_pattern, query_contains, query_pattern, limit))
    else:
        cur.execute("""
            SELECT DISTINCT author, COUNT(*) as cnt
            FROM public.book_authors
            WHERE LOWER(author) LIKE %s OR LOWER(author) LIKE %s
            GROUP BY author
            ORDER BY 
                CASE WHEN LOWER(author) LIKE %s THEN 0 ELSE 1 END,
                cnt DESC
            LIMIT %s
        """, (query_pattern, query_contains, query_pattern, limit))
    
    results = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return results

# ============ DIRECT SEARCH - NO AI (NEW) ============

def search_direct_artist(name: str, limit: int = 100) -> dict:
    """Ricerca diretta per artista - SQL only, no Claude."""
    
    conn = get_db()
    cur = conn.cursor()
    
    name_lower = name.lower().strip()
    parts = name_lower.split()
    
    if len(parts) >= 2:
        reversed_name = " ".join(reversed(parts))
        pattern_original = f"%{name_lower}%"
        pattern_reversed = f"%{reversed_name}%"
    else:
        pattern_original = f"%{name_lower}%"
        pattern_reversed = pattern_original
    
    # 1. Monografie titolo
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
    
    # 2. Monografie
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
    
    # 3. Collettive
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
    
    # 4. Menzioni
    found_ids = [r[0] for r in monografie_titolo + monografie + collettive]
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
    menzioni = cur.fetchall()
    
    cur.close()
    conn.close()
    
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'prezzo', 
               'pagine', 'lingua', 'immagine', 'isbn', 'ranking', 'tipo']
    
    all_results = (
        [dict(zip(columns, r)) for r in monografie_titolo] +
        [dict(zip(columns, r)) for r in monografie] +
        [dict(zip(columns, r)) for r in collettive] +
        [dict(zip(columns, r)) for r in menzioni]
    )
    
    return {
        'risultati': all_results[:limit],
        'nome_cercato': name,
        'conteggi': {
            'monografie': len(monografie_titolo) + len(monografie),
            'collettive': len(collettive),
            'menzioni': len(menzioni),
            'totale': len(all_results)
        }
    }

def search_direct_author(name: str, limit: int = 100) -> dict:
    """Ricerca diretta per autore - SQL only, no Claude."""
    
    conn = get_db()
    cur = conn.cursor()
    
    name_lower = name.lower().strip()
    parts = name_lower.split()
    
    if len(parts) >= 2:
        reversed_name = " ".join(reversed(parts))
        pattern_original = f"%{name_lower}%"
        pattern_reversed = f"%{reversed_name}%"
    else:
        pattern_original = f"%{name_lower}%"
        pattern_reversed = pattern_original
    
    cur.execute("""
        SELECT DISTINCT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               4 as ranking, 'autore' as tipo
        FROM public.books b
        JOIN public.book_authors bau ON b.id = bau.book_id
        WHERE (LOWER(bau.author) LIKE %s OR LOWER(bau.author) LIKE %s)
        ORDER BY b.anno DESC
        LIMIT %s
    """, (pattern_original, pattern_reversed, limit))
    
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'prezzo', 
               'pagine', 'lingua', 'immagine', 'isbn', 'ranking', 'tipo']
    results = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {
        'risultati': results,
        'nome_cercato': name,
        'conteggi': {'totale': len(results)}
    }

def search_direct_title(title: str, limit: int = 50) -> dict:
    """Ricerca diretta per titolo - SQL only, no Claude."""
    
    conn = get_db()
    cur = conn.cursor()
    
    title_lower = title.lower().strip()
    pattern = f"%{title_lower}%"
    
    cur.execute("""
        SELECT b.id, b.titolo, b.editore, b.anno, b.descrizione,
               b.prezzo_def_euro_web, b.pagine, b.lingua, b.permalinkimmagine, b.isbn_expo,
               1 as ranking, 'titolo' as tipo
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
               'pagine', 'lingua', 'immagine', 'isbn', 'ranking', 'tipo']
    results = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {
        'risultati': results,
        'titolo_cercato': title,
        'conteggi': {'totale': len(results)}
    }

# ============ AI-POWERED SEARCH (existing) ============

def extract_name_from_query(query: str, context: dict = None, image_base64: str = None) -> dict:
    """Usa Claude per estrarre nomi di artisti/autori, titoli e filtri dalla query.
    
    Args:
        query: Testo della query utente
        context: Contesto conversazione precedente
        image_base64: Immagine in base64 (opzionale) - foto copertina libro
    """
    
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
    
    # Costruisci il contenuto del messaggio (supporta testo + immagine)
    content = []
    
    # Se c'è un'immagine, aggiungila prima
    if image_base64:
        # Rimuovi eventuale prefisso data:image/...;base64,
        clean_base64 = image_base64
        if ',' in clean_base64:
            clean_base64 = clean_base64.split(',')[1]
        
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": clean_base64
            }
        })
        
        # Prompt per ricerca con immagine
        text_prompt = f"""Stai analizzando una FOTO DI COPERTINA di un libro d'arte.

Esamina attentamente l'immagine ed estrai:
- Titolo del libro (spesso in grande sulla copertina)
- Nome artista/autore (spesso sotto il titolo o in alto)
- Editore (spesso in basso o sul dorso)
- Qualsiasi altro testo visibile utile

{f'Nota aggiuntiva dall utente: "{query}"' if query and query.strip() else ''}
{context_info}

Rispondi SOLO con un oggetto JSON valido:
- tipo: "titolo" (se hai identificato un titolo specifico) o "nome" (se hai identificato principalmente un artista/autore)
- titolo: "titolo letto dalla copertina" (se tipo=titolo)
- nome: "Nome Cognome" (se tipo=nome, o se hai letto un nome artista)
- editore: "nome editore" (se visibile)

Se non riesci a leggere nulla di utile, rispondi con:
{{"tipo": "errore", "messaggio": "Non riesco a leggere il testo sulla copertina"}}

JSON:"""
    else:
        # Prompt originale per ricerca testuale
        text_prompt = f"""Analizza questa query di ricerca libri: "{query}"
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
"Bruce Nauman" → {{"tipo": "nome", "nome": "Bruce Nauman"}}
"fotografia giapponese anni 70" → {{"tipo": "tematica", "tema": "fotografia giapponese", "anno_min": 1970, "anno_max": 1979}}

JSON:"""
    
    content.append({"type": "text", "text": text_prompt})
    
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": content}]
    )
    
    try:
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        result = json.loads(text)
        
        # Gestione errore lettura immagine
        if result.get('tipo') == 'errore':
            return {"tipo": "tematica", "tema": query or "ricerca generica"}
        
        if result.get('tipo') == 'followup' and context.get('previousSearch'):
            if not result.get('nome') or result.get('nome') == '[nome precedente]':
                result['nome'] = context.get('previousSearch')
            result['tipo'] = 'nome'
            
            prev_filters = context.get('previousFilters', {})
            for key in ['lingua', 'anno_min', 'anno_max']:
                if key not in result and key in prev_filters:
                    result[key] = prev_filters[key]
        
        return result
    except:
        return {"tipo": "tematica", "tema": query or "ricerca generica"}

def generate_refined_response(refinement: str, results: list, original_query: str) -> str:
    """Genera risposta breve per ricerca affinata."""
    
    if not results:
        return f"Nessun risultato per '{original_query}' + '{refinement}'."
    
    books_context = "\n".join([
        f"- ID:{r.get('id')} | \"{r.get('titolo')}\" ({r.get('editore', '')}, {r.get('anno', '')})"
        for r in results[:8]
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=350,
        messages=[{
            "role": "user",
            "content": f"""L'utente cercava "{original_query}" e ha affinato con "{refinement}".

Risultati: {books_context}

ISTRUZIONI:
1. NON iniziare con "Ho trovato..."
2. Commenta 4-6 titoli, formato: [[ID:xxx|Titolo]] - frase secca
3. Niente domande finali"""
        }]
    )
    
    response_text = message.content[0].text.strip()
    
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    return response_text

def generate_comment_response(filter_term: str, books: list, original_query: str) -> str:
    """Genera commenti brevi sui libri filtrati."""
    
    if not books:
        return f"Nessun risultato specifico per '{filter_term}'."
    
    books_context = "\n".join([
        f"- ID:{b.get('id')} | \"{b.get('titolo')}\" ({b.get('editore', '')}, {b.get('anno', '')})"
        for b in books[:8]
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""L'utente cercava "{original_query}" e ha filtrato per "{filter_term}".

Libri: {books_context}

ISTRUZIONI:
1. Commenta 3-5 libri, formato: [[ID:xxx|Titolo]] - frase secca
2. Niente domande finali, tono da bibliotecario"""
        }]
    )
    
    response_text = message.content[0].text.strip()
    
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    return response_text

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
        return f"Non ho trovato libri con titolo \"{title}\". Prova con parole chiave diverse."
    
    books_context = "\n".join([
        f"- ID:{r['id']} | \"{r['titolo']}\" ({r['editore']}, {r['anno']}) - Lingua: {r['lingua']}"
        for r in results[:10]
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Sei un bibliotecario. L'utente cerca: "{title}"

RISULTATI ({len(results)} titoli):
{books_context}

ISTRUZIONI:
- Conferma se c'è un match: "Sì, abbiamo [[ID:xxx|Titolo]]"
- Formato: [[ID:xxx|Titolo]] con editore, anno, lingua
- Risposte brevi"""
        }]
    )
    
    response_text = message.content[0].text
    
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
    
    name_lower = name.lower().strip()
    parts = name_lower.split()
    
    if len(parts) >= 2:
        reversed_name = " ".join(reversed(parts))
        pattern_original = f"%{name_lower}%"
        pattern_reversed = f"%{reversed_name}%"
    else:
        pattern_original = f"%{name_lower}%"
        pattern_reversed = pattern_original
    
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
    
    tipo_pub = filters.get('tipo_pub')
    
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
    
    columns = ['id', 'titolo', 'editore', 'anno', 'descrizione', 'prezzo', 
               'pagine', 'lingua', 'immagine', 'isbn', 'ranking', 'tipo']
    
    result_dict = {
        'monografie_titolo': [dict(zip(columns, r)) for r in monografie_titolo],
        'monografie': [dict(zip(columns, r)) for r in monografie],
        'collettive': [dict(zip(columns, r)) for r in collettive],
        'come_autore': [dict(zip(columns, r)) for r in come_autore],
        'citazioni': [dict(zip(columns, r)) for r in citazioni],
    }
    
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
    
    all_results = (result_dict['monografie_titolo'] + result_dict['monografie'] + 
                   result_dict['collettive'] + result_dict['come_autore'] + result_dict['citazioni'])
    
    lingue = {}
    for r in all_results:
        lang = r.get('lingua', '').strip().upper()
        if lang:
            if lang in ['I', 'IT', 'ITA', 'ITALIANO']:
                lang = 'IT'
            elif lang in ['E', 'EN', 'ENG', 'ENGLISH']:
                lang = 'EN'
            elif lang in ['D', 'DE', 'DEU', 'DEUTSCH']:
                lang = 'DE'
            elif lang in ['F', 'FR', 'FRA', 'FRANCAIS']:
                lang = 'FR'
            lingue[lang] = lingue.get(lang, 0) + 1
    
    anni = [int(r.get('anno', 0)) for r in all_results if r.get('anno') and str(r.get('anno')).isdigit()]
    anno_min = min(anni) if anni else None
    anno_max = max(anni) if anni else None
    
    result_dict['filtri_disponibili'] = {
        'lingue': dict(sorted(lingue.items(), key=lambda x: -x[1])),
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
        return f"Non ho trovato pubblicazioni su {name}{filter_msg}. Vuoi provare senza filtri?"
    
    context_parts = []
    
    filter_info = []
    if filters.get('lingua'):
        filter_info.append(f"lingua: {filters['lingua']}")
    if filters.get('anno_min'):
        filter_info.append(f"dal {filters['anno_min']}")
    if filters.get('anno_max'):
        filter_info.append(f"fino al {filters['anno_max']}")
    
    if filter_info:
        context_parts.append(f"FILTRI: {', '.join(filter_info)}")
    
    n_mono = len(results['monografie_titolo']) + len(results['monografie'])
    n_coll = len(results['collettive'])
    n_autore = len(results['come_autore'])
    n_citazioni = len(results['citazioni'])
    
    context_parts.append(f"""CONTEGGI:
- Monografie: {n_mono}
- Collettive: {n_coll}
- Come autore: {n_autore}
- Menzioni: {n_citazioni}
- Totale: {results['totale']}""")
    
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
        context_parts.append(f"COLLETTIVE:\n" + "\n".join(titles))
        all_books.extend(results['collettive'][:3])
    
    if results['come_autore']:
        titles = [f"- \"{r['titolo']}\" ({r['editore']}, {r['anno']})" for r in results['come_autore'][:2]]
        context_parts.append(f"COME AUTORE:\n" + "\n".join(titles))
        all_books.extend(results['come_autore'][:2])
    
    context = "\n\n".join(context_parts)
    
    books_with_ids = "\n".join([f"ID:{b['id']} | {b['titolo']}" for b in all_books])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Bibliotecario arte. Utente cerca: {name}

DATI: {context}

LIBRI (usa per link): {books_with_ids}

REGOLE:
- Formato link: [[ID:xxx|Titolo]]
- Inizia con numeri totali
- Cita 3-5 titoli
- Concludi: "Filtro per periodo, lingua o tipo?"
- Breve, lingua utente"""
        }]
    )
    
    response_text = message.content[0].text
    
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    return response_text

def generate_response_semantic(query: str, results: list) -> dict:
    """Genera risposta per ricerca semantica."""
    
    if not results:
        return {
            "risposta": "Non ho trovato risultati. Prova con termini diversi.",
            "suggerimenti": []
        }
    
    books_context = "\n".join([
        f"- ID:{r['id']} | \"{r['titolo']}\" ({r['editore']}, {r['anno']})"
        for r in results[:7]
    ])
    
    message = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""Bibliotecario arte. Query: "{query}"

RISULTATI: {books_context}

REGOLE:
- Formato link: [[ID:xxx|Titolo]]
- Cita 3-5 libri
- Se ricerca generica, fai domande
- Lingua utente

OBBLIGATORIO - ULTIMA RIGA:
SUGGERIMENTI: termine1, termine2, termine3, termine4
(3-5 parole brevi per affinare)"""
        }]
    )
    
    response_text = message.content[0].text.strip()
    
    suggerimenti = []
    if "SUGGERIMENTI:" in response_text:
        parts = response_text.split("SUGGERIMENTI:")
        response_text = parts[0].strip()
        if len(parts) > 1:
            sugg_text = parts[1].strip()
            suggerimenti = [s.strip() for s in sugg_text.split(",") if s.strip()]
    
    def replace_link(match):
        book_id = match.group(1)
        title = match.group(2)
        return f'<a href="https://test01-frontend.vercel.app/books/{book_id}" target="_blank">{title}</a>'
    
    response_text = re.sub(r'\[\[ID:([^\|]+)\|([^\]]+)\]\]', replace_link, response_text)
    
    return {"risposta": response_text, "suggerimenti": suggerimenti}

# ============ HTTP HANDLER ============

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
        path = parsed.path
        params = parse_qs(parsed.query)
        
        # NEW: /api/suggest endpoint
        if path == '/api/suggest':
            suggestion_type = params.get('type', ['artist'])[0]
            query = params.get('q', [''])[0]
            limit = int(params.get('limit', ['10'])[0])
            
            suggestions = get_suggestions(suggestion_type, query, limit)
            
            self.wfile.write(json.dumps({
                "suggestions": suggestions
            }).encode())
            return
        
        # Existing /api/search GET
        query = params.get('q', [''])[0]
        limit = int(params.get('limit', ['10'])[0])
        
        if not query:
            self.wfile.write(json.dumps({
                "status": "ok",
                "message": "Libro Search API v3. Usa ?q=query per cercare."
            }).encode())
            return
        
        try:
            query_info = extract_name_from_query(query, None)
            
            if query_info.get('tipo') == 'titolo':
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
                    results['monografie_titolo'] + results['monografie'] + 
                    results['collettive'] + results['come_autore'] + results['citazioni'][:20]
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
                response_data = generate_response_semantic(query, results)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "semantica",
                    "risposta": response_data["risposta"],
                    "suggerimenti": response_data.get("suggerimenti", []),
                    "risultati": results
                }, default=str).encode())
                
        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())
    
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
            direct = data.get('direct', False)
            search_type = data.get('searchType', None)
            direct_filters = data.get('filters', None)
            mode = data.get('mode', None)
            image_base64 = data.get('image', None)  # NUOVO: supporto immagine
            
            # Se c'è un'immagine ma nessuna query, è ok (ricerca solo per immagine)
            if not query and not image_base64:
                self.wfile.write(json.dumps({"error": "Query richiesta"}).encode())
                return
            
            # NEW: Direct search (no AI)
            if direct:
                if search_type == 'artist':
                    result = search_direct_artist(query, limit)
                    self.wfile.write(json.dumps({
                        "tipo_ricerca": "diretto",
                        "nome_cercato": query,
                        "risultati": result['risultati'],
                        "conteggi": result['conteggi']
                    }, default=str).encode())
                    return
                    
                elif search_type == 'author':
                    result = search_direct_author(query, limit)
                    self.wfile.write(json.dumps({
                        "tipo_ricerca": "diretto",
                        "nome_cercato": query,
                        "risultati": result['risultati'],
                        "conteggi": result['conteggi']
                    }, default=str).encode())
                    return
                    
                elif search_type == 'title':
                    result = search_direct_title(query, limit)
                    self.wfile.write(json.dumps({
                        "tipo_ricerca": "diretto",
                        "titolo_cercato": query,
                        "risultati": result['risultati'],
                        "conteggi": result['conteggi']
                    }, default=str).encode())
                    return
            
            # Comment mode
            if mode == 'comment':
                filtered_books = data.get('filteredBooks', [])
                original_query = data.get('originalQuery', '')
                risposta = generate_comment_response(query, filtered_books, original_query)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "commento",
                    "risposta": risposta,
                    "risultati": filtered_books
                }, default=str).encode())
                return
            
            # Refined mode
            if mode == 'refined':
                original_query = data.get('originalQuery', '')
                refinement = data.get('refinement', '')
                results = search_semantic(query, limit)
                risposta = generate_refined_response(refinement, results, original_query)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "affinata",
                    "risposta": risposta,
                    "risultati": results,
                    "suggerimenti": []
                }, default=str).encode())
                return
            
            # Direct filters (existing)
            if direct_filters:
                name = query
                results = search_by_name(name, direct_filters, limit)
                
                totale = results['totale']
                filter_desc = []
                if direct_filters.get('lingua'):
                    lang_names = {'IT': 'in italiano', 'EN': 'in inglese', 'DE': 'in tedesco', 'FR': 'in francese'}
                    filter_desc.append(lang_names.get(direct_filters['lingua'], f"in {direct_filters['lingua']}"))
                if direct_filters.get('tipo_pub'):
                    tipo_names = {'monografia': 'monografie', 'collettiva': 'collettive', 'autore': 'come autore'}
                    filter_desc.append(tipo_names.get(direct_filters['tipo_pub'], direct_filters['tipo_pub']))
                
                filter_text = ', '.join(filter_desc) if filter_desc else ''
                risposta = f"{totale} risultati per {name} {filter_text}." if totale > 0 else f"Nessun risultato per {name} {filter_text}."
                
                all_results = (
                    results['monografie_titolo'] + results['monografie'] + 
                    results['collettive'] + results['come_autore'] + results['citazioni'][:20]
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
            
            # AI-powered search (con supporto immagine ibrido)
            context = data.get('context', {})
            query_info = extract_name_from_query(query, context, image_base64)
            
            # Se c'è un'immagine, usa la ricerca ibrida
            if image_base64:
                image_search_result = search_by_image_hybrid(query_info, image_base64, limit)
                
                risposta = generate_response_for_image_search(image_search_result, query_info)
                
                risultati = []
                for c in image_search_result['candidati'][:20]:
                    risultati.append({
                        'id': c['id'],
                        'titolo': c['titolo'],
                        'editore': c.get('editore', ''),
                        'anno': c.get('anno', ''),
                        'immagine': c.get('immagine', ''),
                        'confidence': c.get('confidence', 'bassa'),
                        'image_match': c.get('image_match', False)
                    })
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "immagine",
                    "risposta": risposta,
                    "risultati": risultati,
                    "best_match": image_search_result.get('best_match'),
                    "search_term": image_search_result.get('search_term'),
                    "total_candidates": image_search_result.get('total_candidates', 0)
                }, default=str).encode())
                return
            
            if query_info.get('tipo') == 'titolo':
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
                    results['monografie_titolo'] + results['monografie'] + 
                    results['collettive'] + results['come_autore'] + results['citazioni'][:20]
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
                response_data = generate_response_semantic(query, results)
                
                self.wfile.write(json.dumps({
                    "tipo_ricerca": "semantica",
                    "risposta": response_data["risposta"],
                    "suggerimenti": response_data.get("suggerimenti", []),
                    "risultati": results
                }, default=str).encode())
                
        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())
