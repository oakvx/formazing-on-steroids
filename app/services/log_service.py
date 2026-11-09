"""
LogService - Lettura e filtraggio dei log applicativi per intervallo di date.

Responsabilità:
- Scoperta automatica dei file di log ruotati (formazing.log, formazing.log.1, ...)
- Parsing delle righe di log con timestamp e gestione dei traceback multiriga
- Filtro efficiente per intervallo di date senza limiti arbitrari di righe
"""

import os
import re
import logging
from collections import deque
from datetime import datetime, date
from typing import List, Optional

logger = logging.getLogger(__name__)

# Pattern per riconoscere l'inizio di una nuova riga di log con timestamp.
# Formato atteso: "2026-08-07 14:22:33 | LEVEL | module | message"
_LOG_LINE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


class LogService:
    """
    Servizio per leggere e filtrare i file di log dell'applicazione Formazing.

    Gestisce la scoperta e lettura dei file di log ruotati (es. formazing.log,
    formazing.log.1, formazing.log.2, ...) e filtra le righe in base a un
    intervallo di date specificato dall'utente.

    Design decisions:
    - I file vengono scansionati dal più vecchio al più recente per garantire
      l'ordine cronologico corretto dell'output.
    - I file il cui mtime è antecedente a start_dt vengono saltati per efficienza.
    - I traceback multiriga (righe senza timestamp) vengono "appesi" all'evento
      precedente per mantenere il contesto degli errori completo e leggibile.
    - Non viene imposto alcun limite al numero di righe restituite: l'utente
      riceve TUTTI i log che rientrano nell'intervallo richiesto.
    """

    def __init__(self):
        """
        Inizializza il LogService leggendo il path del file di log da Proteus,
        lo stesso sorgente usato da setup_logging() in config.py per scrivere i log.

        In questo modo LogService punta sempre alla directory corretta indipendentemente
        dall'ambiente (sviluppo, produzione, working directory diversa, path assoluto nel .env).
        """
        # Legge il path del log file dalla stessa configurazione usata da setup_logging().
        # Esempio valori possibili: 'logs/formazing.log', '/var/log/formazing/formazing.log'
        try:
            from config import proteus
            configured_log_file = proteus.get('LOG.FILE', 'logs/formazing.log')
        except Exception:
            # Fallback nel caso Proteus non sia disponibile (es. test isolati)
            configured_log_file = 'logs/formazing.log'

        # Se il path è relativo, lo rendiamo assoluto rispetto alla root del progetto
        # (3 livelli su da app/services/log_service.py → app/services → app → root)
        if not os.path.isabs(configured_log_file):
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            configured_log_file = os.path.join(project_root, configured_log_file)

        self._log_dir      = os.path.dirname(configured_log_file)
        self._log_basename = os.path.basename(configured_log_file)

        logger.debug(
            f"LogService inizializzato | dir: '{self._log_dir}' | file: '{self._log_basename}'"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_logs(
        self,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None
    ) -> List[str]:
        """
        Restituisce tutte le righe di log (inclusi traceback) nell'intervallo
        temporale specificato.

        Se start_dt è None, usa l'inizio del giorno corrente.
        Se end_dt è None, usa la fine del giorno corrente (23:59:59).

        Args:
            start_dt: Datetime di inizio intervallo (inclusivo).
            end_dt:   Datetime di fine intervallo (inclusivo).

        Returns:
            Lista di stringhe, una per ogni riga di log (incluse righe di traceback
            associate a eventi che rientrano nell'intervallo). Ordinate dalla più
            vecchia alla più recente.
        """
        # Valori di default: giorno corrente
        if start_dt is None:
            today = date.today()
            start_dt = datetime(today.year, today.month, today.day, 0, 0, 0)
        if end_dt is None:
            today = date.today()
            end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

        log_files = self._discover_log_files()
        if not log_files:
            logger.warning(f"Nessun file di log trovato in: {self._log_dir}")
            return []

        results: List[str] = []
        for filepath in log_files:
            results.extend(self._read_file_in_range(filepath, start_dt, end_dt))

        logger.debug(
            f"LogService: trovate {len(results)} righe nell'intervallo "
            f"{start_dt.isoformat()} - {end_dt.isoformat()}"
        )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _discover_log_files(self) -> List[str]:
        """
        Scopre tutti i file di log nella directory, ordinati dal più vecchio
        al più recente (i backup numerati più alti sono i più vecchi).

        Returns:
            Lista di path assoluti ai file di log, ordinata cronologicamente
            (il più vecchio prima).
        """
        if not os.path.isdir(self._log_dir):
            logger.error(f"Directory dei log non trovata: {self._log_dir}")
            return []

        files = []
        for filename in os.listdir(self._log_dir):
            # Accetta: formazing.log, formazing.log.1, formazing.log.2, ...
            if filename == self._log_basename or (
                filename.startswith(self._log_basename + '.') and
                filename[len(self._log_basename) + 1:].isdigit()
            ):
                candidate = os.path.join(self._log_dir, filename)
                # Ignore symlinked log files: only the configured log family
                # inside the log directory is authorized for the viewer.
                if os.path.isfile(candidate) and not os.path.islink(candidate):
                    files.append(candidate)

        # Ordina: i file numerati più grandi (es. .5) sono i più vecchi;
        # il file senza suffisso numerico è il più recente.
        def sort_key(path: str) -> int:
            """Assegna un numero d'ordine: più alto = più vecchio = prima nella lista."""
            basename = os.path.basename(path)
            suffix = basename[len(self._log_basename):]
            if suffix == '':
                return 0  # file principale = più recente, messo alla fine
            try:
                return -int(suffix.lstrip('.'))  # es. .3 → -3 (più alto = più vecchio)
            except ValueError:
                return 1

        # Ordina ascendente: i più vecchi (numeri negativi grandi) vengono prima
        files.sort(key=sort_key)
        logger.debug(f"LogService: file di log individuati: {[os.path.basename(f) for f in files]}")
        return files

    def _read_file_in_range(
        self,
        filepath: str,
        start_dt: datetime,
        end_dt: datetime
    ) -> List[str]:
        """
        Legge un singolo file di log e restituisce le righe che rientrano
        nell'intervallo specificato, incluse le righe di traceback associate.

        Ottimizzazione: se la data di ultima modifica del file (mtime) è
        antecedente a start_dt, il file viene saltato completamente poiché
        non può contenere log più recenti di quella data.

        Args:
            filepath: Path assoluto al file da leggere.
            start_dt: Datetime di inizio (inclusivo).
            end_dt:   Datetime di fine (inclusivo).

        Returns:
            Lista di righe di log nell'intervallo, in ordine cronologico.
        """
        # Ottimizzazione: skip del file se modificato prima dell'intervallo richiesto
        try:
            file_mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            if file_mtime < start_dt:
                logger.debug(
                    f"LogService: skip '{os.path.basename(filepath)}' "
                    f"(mtime {file_mtime.date()} < start {start_dt.date()})"
                )
                return []
        except OSError as e:
            logger.warning(f"LogService: impossibile leggere mtime di '{filepath}': {e}")

        results: List[str] = []
        # Buffer che accumula le righe dell'evento corrente (riga principale + traceback)
        current_event_lines: List[str] = []
        current_event_in_range: bool = False

        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                for raw_line in f:
                    line = raw_line.rstrip('\r\n')
                    match = _LOG_LINE_RE.match(line)

                    if match:
                        # Nuova riga di log con timestamp: scarica l'evento precedente
                        if current_event_in_range and current_event_lines:
                            results.extend(current_event_lines)

                        # Parsa il timestamp della nuova riga
                        ts_str = match.group(1)
                        try:
                            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            current_event_lines = [line]
                            current_event_in_range = False
                            continue

                        # Controlla se la riga rientra nell'intervallo richiesto
                        current_event_in_range = (start_dt <= ts <= end_dt)
                        current_event_lines = [line]

                        # Se il timestamp supera end_dt, i successivi saranno ancora più
                        # recenti: possiamo interrompere la lettura del file.
                        if ts > end_dt:
                            current_event_in_range = False
                            break
                    else:
                        # Riga senza timestamp: è continuazione (traceback, stacktrace)
                        # dell'evento corrente. La aggiungiamo al buffer senza modificare
                        # current_event_in_range.
                        current_event_lines.append(line)

                # Scarica l'ultimo evento rimasto nel buffer
                if current_event_in_range and current_event_lines:
                    results.extend(current_event_lines)

        except OSError as e:
            logger.error(f"LogService: errore lettura file '{filepath}': {e}")

        return results

    def get_filtered_logs(self, start_dt: datetime, end_dt: datetime, *,
                          level: str = 'ALL', query: str = '', limit: int = 200) -> dict:
        """Search rotated log files with bounded response memory.

        Return the newest *limit* lines across matching events. Each event
        includes its traceback context up to a safe, explicit size. The
        existing get_logs() method stays available for trusted local scripts.
        """
        if limit < 1 or limit > 500:
            raise ValueError('limit must be between 1 and 500')
        if level not in {'ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
            raise ValueError('invalid log severity')
        if len(query) > 100:
            raise ValueError('search query too long')

        lines = deque(maxlen=limit)
        matched_lines = 0
        needle = query.casefold()
        level_pattern = re.compile(r'\|\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|')
        # Avoid leaking common secret-bearing key/value fields into the
        # viewer and downloaded text. Logging should also avoid secrets.
        secret_pattern = re.compile(
            r'(?i)\b(authorization|bearer|client_secret|password|'
            r'access_token|refresh_token|token|api[_-]?key)\b(\s*[:=]\s*|\s+)([^\s,;&]+)'
        )

        def add_event(event, in_range, event_level):
            nonlocal matched_lines
            if not in_range or (level != 'ALL' and event_level != level):
                return
            if needle and needle not in '\n'.join(event).casefold():
                return
            for line in event:
                scrubbed = secret_pattern.sub(lambda m: m.group(1) + m.group(2) + '[REDACTED]', line)
                lines.append(scrubbed)
                matched_lines += 1

        for filename in self._discover_log_files():
            event = []
            in_range = False
            event_level = ''
            try:
                with open(filename, 'r', encoding='utf-8', errors='replace') as source:
                    for raw in source:
                        line = raw.rstrip('\r\n')[:2000]
                        timestamp = _LOG_LINE_RE.match(line)
                        if timestamp:
                            add_event(event, in_range, event_level)
                            event = []
                            try:
                                moment = datetime.strptime(timestamp.group(1), '%Y-%m-%d %H:%M:%S')
                            except ValueError:
                                in_range = False
                                continue
                            # RotatingFileHandler writes chronologically in a
                            # single file. Do not retain out-of-range events.
                            if moment > end_dt:
                                in_range = False
                                break
                            in_range = start_dt <= moment <= end_dt
                            severity = level_pattern.search(line)
                            event_level = severity.group(1) if severity else ''
                        if in_range and len(event) < 50:
                            event.append(line)
                        elif in_range and len(event) == 50:
                            event.append('[traceback truncated: too many lines]')
                    add_event(event, in_range, event_level)
            except OSError:
                logger.warning("LogService: failed to read one configured log file")
                continue

        return {
            'logs': list(lines),
            'count': len(lines),
            'truncated': matched_lines > limit,
            'limit': limit,
        }
