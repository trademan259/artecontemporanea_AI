# Libro Search - Deploy Vercel

## Struttura
```
libro-search-vercel/
├── api/
│   └── search.py          # Serverless function
├── public/
│   └── index.html         # Frontend
├── requirements.txt       # Dipendenze Python
├── vercel.json           # Configurazione Vercel
└── README.md
```

## Deploy su Vercel

### 1. Crea repository GitHub
```bash
cd libro-search-vercel
git init
git add .
git commit -m "Initial commit"
```

Poi push su GitHub (crea un nuovo repo su github.com).

### 2. Connetti a Vercel
1. Vai su [vercel.com](https://vercel.com)
2. "Add New Project"
3. Importa il repository GitHub
4. **IMPORTANTE**: Aggiungi le Environment Variables:
   - `NEON_DATABASE_URL` = postgresql://...
   - `VOYAGE_API_KEY` = pa-...
   - `ANTHROPIC_API_KEY` = sk-ant-...

### 3. Deploy
Vercel farà il deploy automaticamente. L'URL sarà tipo:
`https://libro-search-xxx.vercel.app`

## Test locale (opzionale)
```bash
npm i -g vercel
vercel dev
```

## Endpoints
- `GET /` → Frontend HTML
- `POST /api/search` → API ricerca
- `GET /api/search?q=query` → API ricerca (GET)
