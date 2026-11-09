# Formazing — Centro operazioni e calendario

## Centro operazioni (solo amministratori)

La sezione **Operazioni** del menu registra la calendarizzazione e l'invio
feedback avviati dalle conferme della dashboard. Usa SQLite locale, senza un
nuovo servizio server, e conserva **stato, checkpoint, attore, orari e solo
i dati strettamente necessari a riconciliare Notion**. Non conserva il testo
dei messaggi, destinatari email o token dei provider.

Configura `APP__OPERATIONS_DB` in `.env`, per esempio:

```dotenv
APP__OPERATIONS_DB=var/operations.sqlite3
```

Il file e' escluso da Git e non deve trovarsi sotto `app/static/` o in
una cartella esposta al web. Su Linux e' creato con permessi restrittivi.
**Il percorso deve essere persistente e condiviso da tutti i worker Flask**:
se l'app gira in container, montare un volume di dati per SQLite, inclusi i
file WAL se presenti. Salvare il DB con backup SQLite consistente prima
di ogni aggiornamento dell'app. Evitare condivisioni di rete che non
garantiscono i lock SQLite.

### Idempotenza e resilienza

Per la coppia **(ID Notion, tipo di operazione)** e' possibile creare un
solo tentativo. Anche una doppia conferma da due schede o due processi viene
rifiutata senza chiamare Microsoft, Notion o Telegram una seconda volta.

Ogni passaggio irreversibile e' marcato **prima** della richiesta esterna:

1. **Calendarizzazione**: validazione → Microsoft *attempted* → risultato
   Microsoft registrato → Notion *pending* → Telegram non ancora tentato
   → Telegram *attempted* → completato.
2. **Feedback**: validazione → Telegram *attempted* → Notion *pending*
   → completato.

**Una richiesta HTTP fallita non dimostra che il provider non abbia ricevuto
il messaggio.** Quando l'esito Teams/email/Telegram e' incerto, l'operazione
resta *Da verificare* e non offre un generico pulsante "Riprova tutto".
Non vengono inoltrate email, nuovi eventi o Telegram in automatico
per recuperare un errore di rete.

### Retry selettivo e riconciliazione

Se il checkpoint e' `notion_pending`, esiste la prova che Microsoft ha
restituito codice e link della riunione. Il comando **Riconcilia solo
Notion** confronta ID, codice, link e stato con Notion e applica
**soltanto** i campi mancanti. Se trova un valore differente si ferma,
senza sovrascrivere.

Se il checkpoint e' `feedback_notion_pending`, Telegram aveva restituito
successo per tutti i gruppi. La riconciliazione modifica **solo** lo stato
Notion, senza reinviare feedback.

Dopo una calendarizzazione riconciliata, il registro puo' indicare
`telegram_not_started`: **Invia Telegram (prima esecuzione)** applica i
**template standard** (gli eventuali testi personalizzati precedenti non
sono conservati) e registra l'intenzione prima di inviare.
Se la richiesta fallisce, non e' ripetibile dalla UI senza una verifica
manuale del provider.

Una riconciliazione interrotta o un checkpoint `*_attempted` non
autorizza altri invii. L'amministratore deve controllare i sistemi
esterni e documentare l'esito prima di qualsiasi intervento manuale.

**Concorrenza codici formazione:** il legacy `sequence_counter.txt` è ancora
un contatore a file privo di lock interprocesso. Il journal previene le
doppie conferme della **stessa** formazione, ma non garantisce codici
sequenziali univoci se **formazioni diverse** vengono inviate da worker
multipli nello stesso istante. Fino alla migrazione del contatore a una
sequenza transazionale, usare un solo worker per le conferme o prevedere
la serializzazione delle assegnazioni. Non è corretto dichiarare il
sistema interamente safe per invii concorrenti multi-worker.

**Limiti:** la lettura/scrittura su Notion non offre una compare-and-swap
distribuita su Microsoft/Telegram. Il registro riduce duplicati per gli
invii avviati dalla dashboard, non e' una garanzia di *exactly once* dei
provider. Le chiamate a `TrainingService` fatte da script esterni senza
journal non sono coperte: migrare eventuali entry point aggiuntivi prima
di usarli per operazioni reali. Non cancellare il DB per "sbloccare"
un'operazione.

## Calendario e gestione formazioni

- **Calendario** nel menu: navigazione per mese, ricerca testuale, filtro
  per area e stato. I dati sono letti dalla dashboard Notion, riutilizzando
  la cache di 10 minuti; non vengono creati calendari paralleli.
- **Dashboard**: filtro per nome/codice/periodo/data/area/partecipanti/stato
  e contatore dei risultati.
- **Possibili conflitti**: segnala formazioni con lo stesso *istante
  iniziale* e aree coincidenti, o stesso nome alla stessa ora. Non presume
  una durata quando non e' nota e **non verifica la disponibilita' reale
  su Microsoft Graph**. Prima di calendarizzare va verificato il calendario
  Teams/Outlook e la programmazione delle persone.
- **Partecipanti**: pagina riservata agli amministratori con campo
  Partecipanti, Numero Partecipanti e Durata registrati in Notion.
  La sincronizzazione Teams resta un **POST esplicito con CSRF**,
  mai un GET o un'azione automatica leggendo la pagina.

### Requisiti e test

Lo sviluppo non introduce dipendenze Python. Gli unit test offline verificano
concorrenza SQLite, resistenza ai doppi invii, replay bloccato, scenari
Notion riconciliabili e non riconciliabili, filtri calendario, confini
dei mesi e contratti HTTP. In CI:

```sh
python -m pip install Flask==3.1.2 pytest==8.4.2
python -m pytest -q -o addopts= --confcutdir=tests/security tests/security/
```

Prima della distribuzione reale e' obbligatorio il collaudo manuale con
**Notion, Entra/Graph/Teams, Telegram**, la persistenza del DB dopo
riavvio/redeploy e la sincronizzazione di partecipanti tramite account di prova.

Per la configurazione SSO/CSRF e i requisiti HTTPS:
[security-hardening.md](security-hardening.md).
