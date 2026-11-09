# Sicurezza e operatività — Formazing

Questa pagina descrive i controlli introdotti nella PR di hardening del
9 novembre 2026. **Non sostituisce una verifica di sicurezza completa** con
Microsoft Entra, Notion, Microsoft Graph e Telegram reali.

## Prima di avviare

1. Copiare `.env.example` in `.env` senza inserirlo nel repository.
2. Generare una chiave crittograficamente casuale e assegnarla a
   `FLASK__SECRET_KEY` (almeno 32 caratteri). Per esempio:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   In assenza di una chiave adeguata, l'applicazione **non si avvia**:
   è stata eliminata la precedente chiave fissa `dev-secret-key`.
   **Le sessioni firmate con la vecchia chiave non sono riutilizzabili**;
   dopo la sostituzione gli utenti devono autenticarsi di nuovo.

3. Registrare in Microsoft Entra lo stesso redirect usato dall'app.
   Il valore locale predefinito è
   `http://localhost:5000/auth/callback`. In altri ambienti configurare
   `MSAL_REDIRECT_URI` e il redirect autorizzato nella registrazione Entra.
4. Configurare `AUTH__ALLOWED_DOMAINS` con domini esatti, separati da
   virgola, e `AUTH__ADMIN_USERS` con le email specifiche degli admin.
   Il flag admin non aggira più il controllo del dominio.
5. Il server Flask ora ascolta soltanto su `127.0.0.1` per impostazione
   predefinita. Un host pubblico esplicito richiede un deployment con
   proxy HTTPS e session cookie secure, senza debugger Flask.
   Impostare `FLASK__SESSION_COOKIE_SECURE=true` **solo quando HTTPS
   è operativo**; altrimenti il browser non invia il cookie su HTTP.
   Flask development server **non è un server di produzione**.

## Autenticazione e operazioni sensibili

- L'accesso Microsoft usa un valore `state` non prevedibile per ogni
  login, memorizzato nella sessione, con durata massima 10 minuti.
  Callback senza stato corretto o ripetute vengono rifiutate.
- Tutte le richieste POST/PUT/PATCH/DELETE al web server richiedono un
  token CSRF legato alla sessione. I form di conferma e sincronizzazione
  lo generano automaticamente. In caso di scadenza, aggiornare la pagina
  prima di riprovare; **non** disabilitare il controllo.
- La sincronizzazione presenze ora accetta esclusivamente **POST**
  con conferma esplicita: un link GET non può modificare dati di Notion.
- La pagina `/loading` accetta solo destinazioni interne e usa JSON
  escaping nell'inserimento dell'URL in JavaScript.
- Le risposte includono protezioni base del browser: `nosniff`,
  `DENY` per l'embedding, restrizioni `frame-ancestors`,
  `base-uri`, `object-src`, `form-action`, e `no-store` per
  risposte applicative. Il progetto fa uso di script inline e CDN:
  **non è ancora presente una CSP completa con nonce/hash**.

## Visualizzatore log (soltanto amministratori)

La pagina **Stato Sistema** contiene ora filtri operativi:

- Intervallo massimo **31 giorni**; opzioni Ultima ora, Oggi, Ieri,
  7 giorni, 30 giorni.
- Livello: `ALL`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
- Ricerca case-insensitive fino a 100 caratteri.
- Ultime 100, 200 o 500 righe: il server restituisce un indicatore
  `truncated` se vi sono più corrispondenze.
- Lettura delle rotazioni `formazing.log.N` senza attraversare
  collegamenti simbolici; estratti limitati e redazione delle forme più
  comuni di credenziali (password/token/API key).
- Il pulsante download esporta **solo le righe attualmente visibili e
  redatte**: non un backup integrale del file.

**Attenzione:** la redazione è difesa in profondità, non una garanzia
che dati personali/segreti non possano essere scritti in log. Non inserire
credenziali, dati di partecipanti o messaggi confidenziali nei log;
limitarne accesso e retention. Il lettore continua a scandire i file per
trovare le corrispondenze: non è un motore di ricerca indicizzato.

## Test

```sh
python -m pip install Flask==3.1.2 pytest==8.4.2
python -m pytest -q -o addopts= tests/security/test_security_controls.py
python -m compileall -q app config.py run.py
```

Il workflow `.github/workflows/security-ci.yml` esegue questi controlli
senza credenziali né richieste ai provider. Non certifica l'intera
suite di integrazione con servizi reali. Prove aggiuntive consigliate:
login e logout in Microsoft Entra, RBAC, invio notifiche solo da anteprima,
aggiornamento presenze POST, configurazione HTTPS, ripristino sessione e
consultazione dei log con dati sintetici.
