# Guida d'uso di InvestmentManager

## Cosa fa

InvestmentManager conserva le fotografie nel tempo delle posizioni sui broker e
calcola come ripartire un nuovo budget rispetto alle direttive di investimento
concordate. Oggi acquisisce i dati già letti dagli screenshot di Trade Republic e
Scalable Capital e restituisce una proposta per classi di attività, insieme agli
scostamenti per area geografica, settore e dimensione.

Il nodo **consiglia, ma non esegue mai ordini**. La scelta finale dello strumento e
l'acquisto o la vendita restano sempre al proprietario. Non è un bot di trading e non
gestisce spese o entrate personali.

## Avvio in sviluppo

Il nodo non è in produzione e non ha un frontend: si usa tramite le API del backend.

1. Dalla radice del progetto, prepara `.env`: se non c'è, `cp .env.example .env`; se
   esiste già (per esempio con le sole chiavi del bus), aggiungi a mano `DB_PASSWORD`,
   `DB_HOST_PORT` e `DATABASE_URL` copiandoli da `.env.example`.

2. Avvia PostgreSQL in Docker sulla porta `11338`:

   ```bash
   docker compose --env-file .env -f deploy/docker-compose.db.yml up -d
   ```

3. Su un database nuovo applica le migrazioni (`cd backend && ../.venv/bin/alembic upgrade head`).
   Broker e direttiva attiva non si creano da soli: finché non ci sono, ingest e allocazione
   rispondono 404 (vedi «Problemi comuni»).
4. Avvia il backend sulla porta `10026`, con l'ambiente Python del progetto già
   installato:

   ```bash
   set -a
   source .env
   set +a
   PYTHONPATH=backend uvicorn investment_manager_backend.app:app --host 127.0.0.1 --port 10026 --reload
   ```

Le chiamate seguenti usano `http://127.0.0.1:10026`.

## Casi d'uso

### Verificare che il backend sia attivo

```bash
curl -fsS http://127.0.0.1:10026/api/health
```

### Salvare uno screenshot di Trade Republic

Prima leggi dallo screenshot i valori effettivamente visibili. Poi inviali così:

```bash
curl -fsS -X POST \
  http://127.0.0.1:10026/api/positions/ingest/trade-republic \
  -H 'Content-Type: application/json' \
  -d '{
    "snapshot_at": "2026-09-22T09:00:00Z",
    "source": "screenshot",
    "source_ref": "shot1.jpg",
    "positions": [
      {
        "instrument_name": "Msci World Ucits Hedged EUR (Acc)",
        "quantity": "52.373619",
        "last_price": "11.38",
        "market_value": "595.88"
      },
      {
        "instrument_name": "Tesla",
        "quantity": "0.1992",
        "last_price": "313.65",
        "market_value": "62.48",
        "uncertain": true,
        "uncertain_notes": "Change% non leggibile"
      }
    ]
  }'
```

`market_value` è obbligatorio. Sono ammessi anche `ticker`, `change_pct` e
`currency` (predefinita a `EUR`). Non indovinare i campi illeggibili: omettili e usa
`uncertain` con `uncertain_notes`. Ogni chiamata crea un nuovo snapshot storico.

### Salvare uno screenshot di Scalable Capital

Questa schermata espone valore di mercato, variazione percentuale e l'eventuale badge
del piano di accumulo, ma non quantità o prezzi:

```bash
curl -fsS -X POST \
  http://127.0.0.1:10026/api/positions/ingest/scalable-capital \
  -H 'Content-Type: application/json' \
  -d '{
    "snapshot_at": "2026-09-22T09:00:00Z",
    "source": "screenshot",
    "source_ref": "shot2.jpg",
    "positions": [
      {
        "instrument_name": "Xtrackers MSCI World (Acc)",
        "market_value": "1265.36",
        "change_pct": "1.09",
        "plan_badge": "P"
      },
      {
        "instrument_name": "Brookfield Renewable",
        "market_value": "52.45",
        "change_pct": "0.63"
      }
    ]
  }'
```

Il badge va riportato come testo, senza attribuirgli un significato non mostrato
dalla schermata. La sua presenza indica soltanto che esiste un piano ricorrente.

### Calcolare la ripartizione di un nuovo budget

Per simulare l'aggiunta di 500 euro usando la direttiva attiva più recente:

```bash
curl -fsS 'http://127.0.0.1:10026/api/portfolio/allocation?budget=500'
```

Per scegliere esplicitamente una direttiva esistente:

```bash
curl -fsS 'http://127.0.0.1:10026/api/portfolio/allocation?budget=500&directive_slug=core-satellite-2026-09-21-v2'
```

`budget` deve essere almeno zero e vale `0` se omesso. Il calcolo usa l'ultimo
snapshot di ogni broker. In `gaps`, un `gap_eur` negativo indica una classe sotto
target; uno positivo indica una classe sopra target. `proposal` ripartisce il budget
solo tra classi di attività e non indica ordini da eseguire. Un eventuale
`unallocated_eur` resta deliberatamente non assegnato. Controlla sempre `warnings` e
`unclassified` prima di usare il risultato.

## Servizi LLM

Oggi il nodo non chiama alcun modello LLM. Le analisi vengono svolte dalla sessione
Claude che usa i dati e le API del nodo, non dal backend.

Se in futuro verranno aggiunte funzioni basate su modelli, dovranno essere dichiarate
in `life-personal/config/llm-routing.json` con un identificatore nel formato
`investmentManager.<funzione>`.

## Problemi comuni

- `503 database del nodo non configurato`: verifica che `.env` contenga
  `DATABASE_URL`, che sia stato caricato prima di avviare Uvicorn e che PostgreSQL sia
  attivo.
- Docker pubblica una porta diversa da `11338`: avvia Compose con `--env-file .env` e
  verifica che `DB_HOST_PORT=11338` sia presente.
- `404 broker ... non trovato`: gli slug ammessi dagli endpoint sono
  `trade-republic` e `scalable-capital`, ma il broker deve anche esistere già nella
  tabella `brokers`.
- `422` durante l'ingest: controlla i campi richiesti e che lo stesso
  `instrument_name` non compaia due volte nello stesso payload.
- `404` sull'allocazione: non esiste la direttiva richiesta oppure non c'è alcuna
  direttiva con stato `active`.
- Posizioni in `unclassified`: il nome dello strumento non coincide esattamente con
  una classificazione presente nel database. Il loro valore è contato nel totale, ma
  è escluso da percentuali e gap; non assegnare una classe a intuito.
