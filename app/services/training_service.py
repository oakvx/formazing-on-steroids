"""
Training Service - Orchestratore per operazioni formazioni

Centralizza tutta la logica business per:
- Preview messaggi
- Invio comunicazioni  
- Gestione feedback
- Generazione codici
- Aggiornamenti stati

Separazione netta tra controllo HTTP (routes) e business logic (questo service).

DESIGN PATTERN: Singleton
- Garantisce una sola istanza per tutta la vita dell'app
- Bot Telegram sempre online (no restart continui)
- Riutilizzo connessioni Notion/Microsoft/Telegram
"""

import logging
import os
import threading
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from app.services.notion import NotionService, NotionServiceError
from app.services.telegram_service import TelegramService
from app.services.microsoft import MicrosoftService, MicrosoftServiceError
from config import proteus

logger = logging.getLogger(__name__)


class TrainingServiceError(Exception):
    """Eccezione specifica per errori TrainingService."""
    pass


class TrainingService:
    """
    Orchestratore principale per operazioni su formazioni.
    
    DESIGN PATTERN: Singleton
    - Una sola istanza per tutta la vita dell'applicazione
    - Bot Telegram sempre online senza restart
    - Thread-safe per ambienti multi-worker
    
    Separa la logica business dai route Flask per:
    - Maggiore testabilità
    - Riutilizzo del codice
    - Separazione responsabilità
    - Error handling centralizzato
    """
    
    # Singleton state
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Garantisce una sola istanza (thread-safe)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:  # Double-checked locking
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Inizializza servizi dipendenti UNA SOLA VOLTA."""
        # Evita reinizializzazione se già fatto
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        
        logger.debug("Inizializzazione TrainingService (Singleton)")
        
        # Inizializza servizi dipendenti
        self.notion_service = NotionService()
        self.telegram_service = TelegramService(
            token=proteus.get('TELEGRAM.BOT_TOKEN'),
            notion_service=self.notion_service,
            groups_config_path=proteus.get('TELEGRAM.GROUPS_CONFIG', 'config/telegram_groups.json'),
            templates_config_path=proteus.get('TELEGRAM.TEMPLATES_CONFIG', 'config/message_templates.yaml')
        )
        self.microsoft_service = MicrosoftService()
        
        logger.debug("TrainingService inizializzato con NotionService, TelegramService e MicrosoftService")

    @classmethod
    def get_instance(cls):
        """
        Factory method per ottenere istanza singleton.
        
        Usage nelle routes:
            training_service = TrainingService.get_instance()
        """
        return cls()

    async def generate_preview(self, training_id: str) -> Dict:
        """
        Genera anteprima completa per una formazione.
        
        Validazioni:
        - Formazione deve esistere
        - Stato deve essere "Programmata"
        
        Args:
            training_id: ID della formazione da Notion
            
        Returns:
            Dict con preview data: {
                'training': dict formazione,
                'messages': [{'area': 'IT', 'chat_id': -123, 'message': '...'}, ...],
                'codice_generato': str codice generato,
                'email': {
                    'attendee_emails': ['it@jemore.it', ...],
                    'subject': 'Oggetto email',
                    'body_preview': 'Anteprima corpo email...'
                }
            }
            
        Raises:
            TrainingServiceError: Se formazione non valida per preview
        """
        try:
            # Recupera e valida formazione
            training = await self.notion_service.get_formazione_by_id(training_id)
            if not training:
                raise TrainingServiceError(f"Formazione {training_id} non trovata")
                
            if training.get('Stato') != 'Programmata':
                raise TrainingServiceError(
                    f"Solo formazioni 'Programmata' possono essere processate (stato attuale: {training.get('Stato')})"
                )
            
            # Genera codice
            generated_code = self._generate_training_code(training, write=False)
            
            # Aggiungi codice temporaneamente per preview
            training_preview = training.copy()
            training_preview['Codice'] = generated_code
            # Aggiungi un link fittizio per la preview
            training_preview['Link Teams'] = "https://teams.microsoft.com/link/verra/generato/dopo/conferma"
            
            # Genera messaggi preview Telegram per ogni area
            messages_preview = []
            for area in training.get('Area', []):
                # Verifica se l'area ha un gruppo configurato
                if area in self.telegram_service.groups:
                    chat_id = self.telegram_service.groups[area]
                    # Usa formatters per generare messaggio
                    message = self.telegram_service.formatter.format_training_message(training_preview, group_key=area)
                    messages_preview.append({
                        'area': area, # Chiave tecnica (es. 'IT')
                        'display_name': area, # Nome per l'utente
                        'chat_id': chat_id,
                        'message': message
                    })
            
            # Aggiungi anche main_group se presente
            if 'main_group' in self.telegram_service.groups and training.get('Periodo') != 'OUT':
                main_chat_id = self.telegram_service.groups['main_group']
                main_message = self.telegram_service.formatter.format_training_message(training_preview, group_key='main_group')
                messages_preview.append({
                    'area': 'main_group', # Chiave tecnica corretta per il backend
                    'display_name': 'Gruppo Generale', # Nome leggibile
                    'chat_id': main_chat_id,
                    'message': main_message
                })
            
            # Genera preview email usando MicrosoftService
            email_preview = None
            try:
                # Ottieni destinatari email dalle aree
                attendee_emails = self.microsoft_service.calendar_operations._get_attendee_emails_for_areas(
                    training.get('Area', [])
                )
                
                # Genera subject e body preview
                subject = self.microsoft_service.email_formatter.format_subject(training_preview)
                body_preview = self.microsoft_service.email_formatter.format_calendar_body(training_preview)
                                
                email_preview = {
                    'attendee_emails': attendee_emails,
                    'subject': subject,
                    'body_preview': body_preview  
                }
                
                logger.debug(f"Email preview generata - Destinatari: {len(attendee_emails)}")
                
            except Exception as e:
                logger.warning(f"Impossibile generare preview email: {e}")
                # Preview email opzionale - se fallisce, procedi comunque
                email_preview = {
                    'error': str(e),
                    'attendee_emails': [],
                    'subject': 'N/A',
                    'body_preview': 'Errore generazione preview'
                }
            
            preview_data = {
                'training': training,
                'messages': messages_preview,
                'codice_generato': generated_code,
                'email': email_preview
            }
            
            logger.info(
                f"Preview generata: {training.get('Nome', 'N/A')} | "
                f"Telegram: {len(messages_preview)} messaggi | "
                f"Email: {len(email_preview.get('attendee_emails', []))} destinatari"
            )
            return preview_data
            
        except NotionServiceError as e:
            logger.error(f"Errore Notion in preview {training_id}: {e}")
            raise TrainingServiceError(f"Errore accesso dati: {e}")
        except Exception as e:
            logger.error(f"Errore imprevisto in preview {training_id}: {e}")
            raise TrainingServiceError(f"Errore interno: {e}")
    
    async def send_training_notification(self, training_id: str, custom_messages: Dict[str, str] = None, custom_email_body: str = None, progress=None) -> Dict:
        """
        Workflow completo per invio comunicazione formazione.
        
        Steps atomici:
        1. Valida formazione (stato "Programmata")
        2. Genera codice univoco
        3. Crea evento Teams e invia email (Microsoft Graph API) - FAIL-FAST se fallisce
        4. Aggiorna stato Notion -> "Calendarizzata" con codice e link Teams
        5. Invia messaggi Telegram ai gruppi target
        
        Args:
            training_id: ID della formazione da Notion
            custom_messages: Dizionario {group_key: messaggio} personalizzato (opzionale)
            custom_email_body: Corpo email HTML personalizzato (opzionale)
            
        Returns:
            Dict con risultati operazione: {
                'codice_generato': str,
                'teams_link': str,
                'attendee_emails': List[str],
                'telegram_results': dict,
                'nuovo_stato': str
            }
            
        Raises:
            TrainingServiceError: Se operazione fallisce
        """
        try:
            logger.info(f"Avvio workflow calendarizzazione | Training ID: {training_id}")
            
            # 1. Valida formazione
            training = await self.notion_service.get_formazione_by_id(training_id)
            if not training:
                raise TrainingServiceError(f"Formazione {training_id} non trovata")
                
            if training.get('Stato') != 'Programmata':
                raise TrainingServiceError("Formazione già processata o stato non valido")
            
            if progress:
                progress.step('validated')

            # 2. Genera codice
            generated_code = self._generate_training_code(training)
            
            # Aggiungi codice alla formazione per passarlo a Microsoft
            training['Codice'] = generated_code
            
            # External effect: record BEFORE sending. After interruption its
            # outcome is unknown, so a blind retry is prohibited.
            if progress:
                progress.step('microsoft_attempted')
            # 3. Crea evento Teams + invia email (FAIL-FAST se fallisce)
            try:
                microsoft_result = await self._create_teams_meeting(training, custom_email_body)
                teams_link = microsoft_result['teams_link']
                attendee_emails = microsoft_result['attendee_emails']
                if progress:
                    progress.step('notion_pending', metadata={
                        'code': generated_code, 'teams_link': teams_link
                    })
            except MicrosoftServiceError as e:
                # FAIL-FAST: Se Microsoft fallisce, non proseguiamo
                logger.error(f"FAIL-FAST: Integrazione Microsoft fallita per {training_id}: {e}")
                raise TrainingServiceError(f"Impossibile creare evento Teams: {e}")
            
            # 4. Aggiorna Notion con codice + link Teams + stato
            await self.notion_service.update_formazione(training_id, {
                'Codice': generated_code,
                'Link Teams': teams_link,
                'Stato': 'Calendarizzata'
            })
            
            if progress:
                progress.step('telegram_not_started')

            # 5. Recupera formazione aggiornata per invio Telegram
            updated_training = await self.notion_service.get_formazione_by_id(training_id)
            
            # Sostituisci il link fittizio nei custom_messages con quello reale
            if custom_messages:
                dummy_link = "https://teams.microsoft.com/link/verra/generato/dopo/conferma"
                real_link = teams_link if teams_link else ""
                for key in custom_messages:
                    if custom_messages[key]:
                        custom_messages[key] = custom_messages[key].replace(dummy_link, real_link)
            
            # Delivery cannot be retried blindly: a network timeout does not
            # prove that Telegram did not accept the message.
            if progress:
                progress.step('telegram_attempted')
            send_results = await self.telegram_service.send_training_notification(updated_training, custom_messages)
            if not all(send_results.values()):
                raise TrainingServiceError(
                    'Invio Telegram parziale o incerto: verifica i gruppi prima di riprovare.'
                )
            
            result = {
                'codice_generato': generated_code,
                'teams_link': teams_link,
                'attendee_emails': attendee_emails,
                'telegram_results': send_results,
                'nuovo_stato': 'Calendarizzata'
            }
            
            logger.info(
                f"Workflow calendarizzazione completato: {updated_training.get('Nome', 'N/A')} | "
                f"Codice: {generated_code} | Email: {len(attendee_emails)} | Telegram: {len(send_results)} gruppi"
            )
            if progress:
                progress.complete()
            return result
            
        except NotionServiceError as e:
            logger.error(f"Errore Notion in send {training_id}: {e}")
            raise TrainingServiceError(f"Errore aggiornamento dati: {e}")
        except MicrosoftServiceError:
            # Già loggato e wrappato sopra, propaga
            raise
        except TrainingServiceError:
            # Già loggato, propaga
            raise
        except Exception as e:
            logger.error(f"Errore imprevisto in send {training_id}: {e}")
            raise TrainingServiceError(f"Errore invio: {e}")
    
    async def generate_feedback_preview(self, training_id: str) -> Dict:
        """
        Genera anteprima richiesta feedback senza inviare nulla.
        
        Validazioni:
        - Formazione deve esistere
        - Stato deve essere "Calendarizzata"
        - Deve avere un codice generato
        
        Args:
            training_id: ID della formazione da Notion
            
        Returns:
            Dict con struttura:
            {
                'training': {...},
                'messages': [{'area': 'IT', 'chat_id': -123, 'message': '...'}, ...]
            }
            
        Raises:
            TrainingServiceError: Se formazione non valida per feedback
        """
        try:
            # 1️⃣ Recupera dati formazione
            training = await self.notion_service.get_formazione_by_id(training_id)
            if not training:
                raise TrainingServiceError(f"Formazione {training_id} non trovata")
            
            # ⚠️ Validazione: deve essere Calendarizzata
            if training.get('Stato') != 'Calendarizzata':
                raise TrainingServiceError(
                    f"La formazione deve essere 'Calendarizzata'. Stato attuale: {training.get('Stato')}"
                )
            
            # ⚠️ Validazione: deve avere codice
            if not training.get('Codice'):
                raise TrainingServiceError("La formazione non ha un codice generato")
            
            # 2️⃣ Genera messaggi preview (senza invio)
            messages_preview = []
            
            # Genera link feedback temporaneo per preview
            feedback_link = self._generate_feedback_link()
            
            # ⚠️ IMPORTANTE: Feedback va SOLO ai gruppi area (NO main_group)
            # Ottieni target groups e rimuovi main_group
            all_target_groups = self.telegram_service._get_target_groups(training)
            target_groups = [group for group in all_target_groups if group != 'main_group']
            
            for group_key in target_groups:
                if group_key in self.telegram_service.groups:
                    chat_id = self.telegram_service.groups[group_key]
                    # Usa formatter esistente per feedback (richiede feedback_link e group_key)
                    message = self.telegram_service.formatter.format_feedback_message(
                        training, 
                        feedback_link, 
                        group_key=group_key
                    )
                    messages_preview.append({
                        'area': group_key,
                        'chat_id': chat_id,
                        'message': message
                    })
            
            logger.info(f"Preview feedback generata: {training.get('Nome', 'N/A')} | Messaggi: {len(messages_preview)}")
            
            return {
                'training': training,
                'messages': messages_preview
            }
            
        except TrainingServiceError:
            raise
        except Exception as e:
            logger.error(f"Errore in generate_feedback_preview: {e}")
            raise TrainingServiceError(f"Errore generazione preview feedback: {e}")
    
    async def send_feedback_request(self, training_id: str, custom_messages: Dict[str, str] = None) -> Dict:
        """
        Invia richiesta feedback post-formazione con supporto a messaggi personalizzati.
        
        Args:
            training_id: ID della formazione da Notion
            custom_messages: Dizionario {group_key: messaggio} personalizzato (opzionale)
            
        Returns:
            Dict con risultati operazione
        """
        try:
            logger.info(f"Avvio workflow feedback | Training ID: {training_id}")
            
            training = await self.notion_service.get_formazione_by_id(training_id)
            if not training:
                raise TrainingServiceError(f"Formazione {training_id} non trovata")

            if training.get('Stato') != 'Calendarizzata':
                raise TrainingServiceError(f"Formazione non ancora calendarizzata. Stato attuale: {training.get('Stato')}")

            feedback_link = self._generate_feedback_link()

            # Passiamo i messaggi personalizzati (se presenti)
            send_results = await self.telegram_service.send_feedback_notification(training, feedback_link, custom_messages)

            await self.notion_service.update_formazione(training_id, {
                'Stato': 'Conclusa'
            })

            result = {
                'feedback_link': feedback_link,
                'telegram_results': send_results,
                'nuovo_stato': 'Conclusa'
            }
            
            logger.info(f"Workflow feedback completato: {training.get('Nome', 'N/A')} | Gruppi notificati: {len(send_results)}")
            return result
            
        except NotionServiceError as e:
            logger.error(f"Errore Notion in feedback {training_id}: {e}")
            raise TrainingServiceError(f"Errore aggiornamento dati: {e}")
        except Exception as e:
            logger.error(f"Errore imprevisto in feedback {training_id}: {e}", exc_info=True)
            raise TrainingServiceError(f"Errore invio feedback: {e}")
    
    # === PRIVATE UTILITY METHODS ===
    
    def _normalize_area(self, area: str) -> str:
        """
        Normalizza l'area rimuovendo il suffisso "in prova".
        
        Mapping:
        - "IT" -> "IT"
        - "IT in prova" -> "IT"
        - "HR" -> "HR"
        - "HR in prova" -> "HR"
        - "All" -> "All"
        - "Test" -> "Test"
        - etc.
        
        Args:
            area: Area originale (può contenere "in prova")
            
        Returns:
            str: Area normalizzata senza suffisso
        """
        # Mapping esplicito per casi speciali
        area_mapping = {
            'IT in prova': 'IT',
            'HR in prova': 'HR',
            'R&D in prova': 'R&D',
            'Marketing in prova': 'Marketing',
            'Commerciale in prova': 'Commerciale',
            'Legale in prova': 'Legale',
            'In prova': 'All',  # "In prova" generico mappa ad "All"
        }
        
        # Controlla se c'è un mapping esplicito
        if area in area_mapping:
            normalized = area_mapping[area]
            logger.debug(f"Area normalizzata: '{area}' '{normalized}'")
            return normalized
        
        # Se non c'è mapping, ritorna l'area originale (es: IT, HR, All, Test)
        return area

    def _generate_training_code(self, training: Dict, write: bool = True) -> str:
        """
        Genera codice formazione univoco.
        Il booleano 'write' indica se salvare il contatore (True per invio reale, False per preview).
        
        Formato: {Area}-{Nome}-{Anno}-{Periodo}-{Sequenza}
        Esempio: IT-Security_Training-2024-SPRING-01
        """
        # File per il contatore di sequenza
        base_dir = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        counter_file = os.path.join(base_dir, 'sequence_counter.txt')

        # Leggi il contatore corrente, incrementalo e salvalo
        try:
            with open(counter_file, 'r') as f:
                current_sequence = int(f.read().strip())
        except FileNotFoundError:
            current_sequence = 0

        next_sequence = current_sequence + 1

        # Salva il contatore aggiornato solo se in modalità reale
        if write:
            with open(counter_file, 'w') as f:
                f.write(str(next_sequence))

        # Area può essere lista o stringa - gestisci entrambi i casi
        area_raw = training.get('Area', ['IT'])
        if isinstance(area_raw, list):
            # Prendi il primo elemento dalla lista e normalizzalo
            area = self._normalize_area(area_raw[0]) if area_raw else 'IT'
        else:
            # Normalizza l'area singola
            area = self._normalize_area(area_raw)
        
        nome = training.get('Nome', 'Formazione').replace(' ', '_').replace('-', '_')
        periodo = training.get('Periodo', 'ONCE')
        anno = str(datetime.now().year)
        sequenza = str(next_sequence).zfill(2)
        
        code = f"{area}-{nome}-{anno}-{periodo}-{sequenza}"
        logger.debug(f"Codice generato: {code}")
        return code
    
    async def _create_teams_meeting(self, training: Dict, custom_body: str = None) -> Dict:
        """
        Crea meeting Teams tramite Microsoft Graph API.
        
        Integrazione reale con MicrosoftService per:
        - Creare evento calendario
        - Generare Teams meeting link
        - Inviare email a mailing list area
        
        Args:
            training: Dict con dati formazione
            custom_body: Corpo email personalizzato (opzionale)
            
        Returns:
            Dict con link e info evento
                
        Raises:
            MicrosoftServiceError: Se creazione evento fallisce
        """
        try:
            # Chiama MicrosoftService passando il corpo personalizzato se presente
            result = await self.microsoft_service.create_training_event(training, custom_body)
            
            logger.info(
                f"Integrazione Microsoft completata | "
                f"Teams Link generato | "
                f"Email inviate a {len(result['attendee_emails'])} destinatari"
            )
            
            return result
            
        except MicrosoftServiceError as e:
            logger.error(f"Errore Microsoft Graph API: {e}")
            raise  # Propaga l'errore per fail-fast
        except Exception as e:
            logger.error(f"Errore imprevisto in creazione Teams meeting: {e}")
            raise MicrosoftServiceError(f"Errore creazione evento: {e}")
    
    def _generate_feedback_link(self) -> str:
        """
        Alla fine inviamo sempre lo stesso link di feedback.
        Sarà l'utente ad inserire il codice formazione nel form.

        Returns:
            str: Link di feedback
        """

        feedback_link = proteus.get('APP.LINKS.FEEDBACK_FORM')
        return feedback_link

    async def sync_attendance_from_teams(self, training_id: str) -> dict:
        """
        Recupera il report presenze da Teams e lo salva in Notion per una formazione conclusa.
        
        Args:
            training_id: ID della formazione in Notion
            
        Returns:
            Dict con i risultati del sync
        """
        try:
            logger.info(f"Avvio sincronizzazione presenze Teams | Training ID: {training_id}")
            
            # 1. Recupera dati formazione
            training = await self.notion_service.get_formazione_by_id(training_id)
            if not training:
                raise TrainingServiceError(f"Formazione {training_id} non trovata")
                
            join_url = training.get('Link Teams')
            if not join_url:
                raise TrainingServiceError("Impossibile sincronizzare: Link Teams non presente per questa formazione")
                
            # 2. Recupera presenze da Microsoft Graph
            attendance_data = await self.microsoft_service.get_meeting_attendance(join_url)
            
            # get_meeting_attendance restituisce un dizionario
            participants = attendance_data.get('participants', [])
            duration = attendance_data.get('duration', 0.0)
            total_participants = attendance_data.get('total_participants', 0)
            
            if not participants:
                logger.warning(f"Nessun partecipante trovato nel report Teams per {training.get('Nome')}")
                # Aggiorniamo comunque Notion passando campi vuoti/azzerati
                await self.notion_service.update_formazione(training_id, {
                    'Partecipanti': [],
                    'Numero Partecipanti': 0,
                    'Durata': 0.0
                })
                return {
                    'status': 'success',
                    'count': 0,
                    'message': 'Nessun partecipante rilevato nel report di Teams. I campi di presenza sono stati azzerati.'
                }
                
            # 3. Salva i partecipanti, numero partecipanti e durata in Notion
            updates = {
                'Partecipanti': participants,
                'Numero Partecipanti': total_participants,
                'Durata': duration
            }
            success = await self.notion_service.update_formazione(training_id, updates)
            
            if not success:
                raise TrainingServiceError("Errore durante il salvataggio dei partecipanti su Notion")
                
            logger.info(f"Sincronizzazione presenze completata | Rilevati {len(participants)} partecipanti | Durata: {duration}h")
            return {
                'status': 'success',
                'count': len(participants),
                'participants': participants,
                'message': f"Sincronizzazione completata: {len(participants)} partecipanti trovati (Durata: {duration}h)."
            }
            
        except NotionServiceError as e:
            logger.error(f"Errore Notion nel sync presenze {training_id}: {e}")
            raise TrainingServiceError(f"Errore aggiornamento Notion: {e}")
        except MicrosoftServiceError as e:
            logger.error(f"Errore Microsoft Graph nel sync presenze {training_id}: {e}")
            raise TrainingServiceError(f"Errore Microsoft Graph: {e}")
        except Exception as e:
            logger.error(f"Errore imprevisto nel sync presenze {training_id}: {e}", exc_info=True)
            raise TrainingServiceError(f"Errore sincronizzazione: {e}")

    async def run_system_diagnostics(self) -> Dict:
        """
        Esegue la diagnostica completa di tutti i servizi del sistema.
        Questo metodo viene condiviso sia dalla rotta web di diagnostica 
        sia dal monitoraggio periodico automatico del bot Telegram.
        
        Returns:
            Dict con i risultati diagnostici di Notion, Microsoft Graph e Telegram.
        """
        # 1. Notion Diagnostics
        try:
            notion_conn = await self.notion_service.diagnostics.test_connection()
            notion_struct = await self.notion_service.diagnostics.validate_database_structure()
        except Exception as e:
            logger.error(f"Errore diagnostica Notion: {e}")
            notion_conn = {'connection_ok': False, 'error': str(e)}
            notion_struct = {'valid': False, 'missing_fields': [], 'incorrect_types': [], 'warnings': []}
            
        # 2. Microsoft Graph Diagnostics
        try:
            microsoft_info = self.microsoft_service.get_diagnostics_info()
        except Exception as e:
            logger.error(f"Errore diagnostica Microsoft: {e}")
            microsoft_info = {'authenticated': False, 'error': str(e)}
            
        # Calcolo scadenza secret da ENV
        secret_expiry_str = proteus.get('MICROSOFT.SECRET_EXPIRY')
        secret_days_remaining = 9999
        secret_expiry_formatted = "N/A"
        if secret_expiry_str:
            try:
                from datetime import datetime
                expiry_date = datetime.strptime(secret_expiry_str.strip(), '%Y-%m-%d')
                now = datetime.now()
                delta = expiry_date - now
                secret_days_remaining = max(0, delta.days)
                secret_expiry_formatted = expiry_date.strftime('%Y-%m-%d')
            except Exception as parse_err:
                logger.error(f"Errore parsing MICROSOFT.SECRET_EXPIRY '{secret_expiry_str}': {parse_err}")
                secret_expiry_formatted = f"Errore parsing ({secret_expiry_str})"
                secret_days_remaining = -1
                
        # 3. Telegram Diagnostics
        try:
            telegram_info = self.telegram_service.check_connection()
        except Exception as e:
            logger.error(f"Errore diagnostica Telegram: {e}")
            telegram_info = {'configured': False, 'connected': False, 'error': str(e)}
            
        return {
            'notion': {
                'connection_ok': notion_conn.get('connection_ok', False),
                'database_accessible': notion_conn.get('database_accessible', False),
                'response_time_ms': notion_conn.get('response_time_ms'),
                'database_title': notion_conn.get('database_info', {}).get('title', 'N/A'),
                'properties_count': notion_conn.get('database_info', {}).get('properties_count', 0),
                'valid_structure': notion_struct.get('valid', False),
                'missing_fields': notion_struct.get('missing_fields', []),
                'incorrect_types': notion_struct.get('incorrect_types', []),
                'warnings': notion_struct.get('warnings', []),
                'error': notion_conn.get('error')
            },
            'microsoft': {
                'authenticated': microsoft_info.get('authenticated', False),
                'user_resolved': microsoft_info.get('user_resolved', False),
                'user_guid': microsoft_info.get('user_guid'),
                'token_info': microsoft_info.get('token_info'),
                'secret_expiry': secret_expiry_formatted,
                'secret_days_remaining': secret_days_remaining,
                'error': microsoft_info.get('error'),
                'configured_email': self.microsoft_service.graph_client.user_email
            },
            'telegram': {
                'configured': telegram_info.get('configured', False),
                'connected': telegram_info.get('connected', False),
                'bot_info': telegram_info.get('bot_info'),
                'groups_count': len(self.telegram_service.groups),
                'error': telegram_info.get('error')
            }
        }

