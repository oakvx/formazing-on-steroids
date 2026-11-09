#!/usr/bin/env python3
"""
🌐 Routes Flask per Formazing

Gestisce tutte le pagine web dell'applicazione:
- Homepage con login
- Dashboard formazioni  
- API endpoints per operazioni
- Pagine di gestione e preview
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from app import cache
from app.services.auth_sso import AuthService, login_required, admin_required
from app.services.notion import NotionServiceError
from app.services.training_service import TrainingService, TrainingServiceError
from app.services.analytics_service import AnalyticsService
from app.services.log_service import LogService
from app.services.operation_journal import OperationJournal, DuplicateOperation, InvalidTransition
from app.services.operation_recovery import reconcile_notion, resume_unsent_telegram
from app.services.schedule_service import build_calendar, all_trainings, filter_trainings, potential_conflicts, STATES
from config import proteus
from app.security import allowed_domain, safe_local_path
import secrets
import hmac
import time
import logging
import yaml
import os
from datetime import datetime, date, timedelta

# Logger per routes (configurazione centralizzata già attiva)
logger = logging.getLogger(__name__)

# Blueprint principale per le routes
main = Blueprint('main', __name__)

# --- ROTTE DI AUTENTICAZIONE SSO ---

@main.route('/login')
def login():
    """Avvia il flusso di login Microsoft."""
    auth_service = AuthService.get_instance()
    # MSAL must echo the nonce on callback. A response not initiated by
    # this browser cannot create an authenticated Flask session.
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    session['oauth_started'] = int(time.time())
    auth_url = auth_service.build_auth_url(state)
    return redirect(auth_url)


@main.route('/auth/callback')
def auth_callback():
    """Riceve il codice da Microsoft e completa l'autenticazione."""
    received = request.args.get('state', '')
    expected = session.pop('oauth_state', None)
    started = session.pop('oauth_started', 0)
    if (not isinstance(expected, str) or not isinstance(received, str) or
            not hmac.compare_digest(received, expected) or
            not isinstance(started, int) or
            not 0 <= time.time() - started <= 600):
        logger.warning("Microsoft callback rejected: missing, expired or mismatched OAuth state")
        return "Richiesta di autenticazione non valida o scaduta.", 400
    code = request.args.get('code')
    if not code:
        flash("Errore durante il login: codice mancante.", "error")
        return redirect(url_for('main.home'))
    
    auth_service = AuthService.get_instance()
    result = auth_service.get_token_from_code(code)
    
    if "error" in result:
        logger.error(f"Errore MSAL callback: {result.get('error_description')}")
        flash(f"Errore login: {result.get('error')}", "error")
        return redirect(url_for('main.home'))
    
    # Estraiamo i dati utente dal token (ID Token)
    id_token_claims = result.get("id_token_claims")
    if not id_token_claims:
        flash("Impossibile recuperare i dati utente.", "error")
        return redirect(url_for('main.home'))
        
    email = id_token_claims.get('preferred_username', '').lower()
    
    # 1. Validazione Dominio
    if not allowed_domain(email, proteus.get('AUTH.ALLOWED_DOMAINS', 'jemore.it')):
        logger.warning("Microsoft callback rejected: unauthorized account domain")
        return render_template('pages/error.html', 
                             message="Dominio non autorizzato. Usa l'account JEMORE.",
                             title="Accesso Negato")
    
    # 2. Setup Sessione: drop any pre-authentication state before issuing the new cookie.
    session.clear()
    session['user'] = id_token_claims
    
    # 3. RBAC: Controllo se Admin
    admin_users = proteus.get('AUTH.ADMIN_USERS', '').split(',')
    is_admin = email in [a.casefold().strip() for a in admin_users]
    session['is_admin'] = is_admin
    
    logger.info(f"Utente loggato: {email} | Admin: {is_admin}")
    flash(f"Benvenuto, {id_token_claims.get('name')}!", "success")
    
    return redirect(url_for('main.dashboard'))


@main.route('/logout')
def logout():
    """Effettua il logout svuotando la sessione."""
    session.clear()
    
    auth_service = AuthService.get_instance()
    logout_url = auth_service.build_logout_url(url_for('main.home', _external=True))
    flash("Logout effettuato con successo.", "info")
    
    return redirect(logout_url)


# --- ROTTE DELL'APPLICAZIONE ---

@main.route('/')
def home():
    """
    Homepage con form di login.
    Se già autenticato, redirect alla dashboard.
    """
    if session.get('user'):
        return redirect(url_for('main.dashboard'))
    return render_template('pages/login.html', 
                         title='Formazing - Gestione Formazioni',
                         app_name='Formazing')


@main.route('/dashboard')
@login_required
async def dashboard():
    """Dashboard principale con formazioni organizzate per status (Flask Async)."""
    try:
        force_refresh = request.args.get('force_refresh') == '1'
        cache_key = 'dashboard_data_notion'
        
        if force_refresh:
            logger.debug("Richiesto ricaricamento forzato dei dati da Notion")
            cache.delete(cache_key)
            return redirect(url_for('main.dashboard'))
        
        # Prova a recuperare i DATI dalla cache
        dashboard_data = cache.get(cache_key)
        
        if dashboard_data:
            logger.debug("Dati dashboard recuperati dalla cache")
        else:
            logger.debug("Dati non in cache. Caricamento da Notion...")
            training_service = TrainingService.get_instance()
            # CHIAMATA OTTIMIZZATA: Singola richiesta globale
            dashboard_data = await training_service.notion_service.get_dashboard_data()
            # Salva i dati grezzi in cache per 10 minuti
            cache.set(cache_key, dashboard_data, timeout=600)

        q = request.args.get('q', '').strip()
        area = request.args.get('area', '').strip()
        status = request.args.get('status', '').strip()
        all_rows = all_trainings(dashboard_data)
        filtered = filter_trainings(all_rows, query=q, area=area, status=status)
        formazioni_programmata = [row for row in filtered if row.get('Stato') == 'Programmata']
        formazioni_calendarizzata = [row for row in filtered if row.get('Stato') == 'Calendarizzata']
        formazioni_conclusa = [row for row in filtered if row.get('Stato') == 'Conclusa']
        
        stats = {
            'programmata': len(formazioni_programmata),
            'calendarizzata': len(formazioni_calendarizzata),
            'conclusa': len(formazioni_conclusa),
        }
        stats['totale'] = stats['programmata'] + stats['calendarizzata'] + stats['conclusa']
        
        # Renderizza il template OGNI VOLTA (così i messaggi flash sono dinamici)
        return render_template('pages/dashboard.html',
                             formazioni_programmata=formazioni_programmata,
                             formazioni_calendarizzata=formazioni_calendarizzata,
                             formazioni_conclusa=formazioni_conclusa,
                             stats=stats,
                             total_unfiltered=len(all_rows),
                             filter_q=q, filter_area=area, filter_status=status,
                             available_areas=sorted(
                                 {str(a) for row in all_rows for a in
                                  (row.get('Area') if isinstance(row.get('Area'), list)
                                   else [row.get('Area')] if row.get('Area') else [])
                                  if a}, key=str.casefold),
                             conflict_ids={
                                 event[id_key]
                                 for event in potential_conflicts(all_rows)
                                 for id_key in ('left_id', 'right_id')
                             },
                             title='Dashboard - Formazing')
                             
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    except NotionServiceError as e:
        # Errore specifico NotionService
        logger.error(f"NotionService error nella dashboard: {e}", exc_info=True)
        flash(f"Errore servizio Notion: {e}", 'error')
        return redirect(url_for('main.home'))
        
    except Exception as e:
        # Errore generico
        logger.error(f"Errore imprevisto nella dashboard: {e}", exc_info=True)
        flash(f"Errore imprevisto: {e}", 'error')
        return redirect(url_for('main.home'))



# --- CALENDARIO E RICERCA (solo lettura, Notion e' la fonte dati) ---

@main.route('/calendar')
@login_required
async def calendar_page():
    month = request.args.get('month', '')
    query = request.args.get('q', '').strip()
    area = request.args.get('area', '').strip()
    status = request.args.get('status', '').strip()
    try:
        snapshot = cache.get('dashboard_data_notion')
        if snapshot is None:
            snapshot = await TrainingService.get_instance().notion_service.get_dashboard_data()
            cache.set('dashboard_data_notion', snapshot, timeout=600)
        model = build_calendar(
            snapshot, month, query=query, area=area, status=status,
        )
        return render_template('pages/calendar.html', model=model,
                               title='Calendario formazioni - Formazing')
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        logger.exception('Impossibile caricare il calendario')
        flash('Calendario non disponibile. Verifica la connessione Notion.', 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/participants/<training_id>')
@admin_required
async def participants_page(training_id):
    """Read-only people/attendance snapshot, explicitly restricted to admins."""
    try:
        training = await TrainingService.get_instance().notion_service.get_formazione_by_id(training_id)
        if not training:
            return jsonify({'error': 'Formazione non trovata'}), 404
        return render_template('pages/participants.html',
                               training=training, title='Partecipanti - Formazing')
    except Exception:
        logger.exception('Errore caricamento partecipanti')
        flash('Dati partecipanti non disponibili.', 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/guida')
@login_required
def guida():
    """Pagina Tutorial e FAQ con dati da Proteus."""
    # Recuperiamo le FAQ dal namespace 'app.guide' caricato in config.py
    faqs = proteus.get('app.guide.faqs', [])
    
    if not faqs:
        logger.warning("FAQ non trovate in Proteus namespace 'app.guide.faqs'")
        
    return render_template('pages/guida.html', 
                         title='Guida - Formazing',
                         faqs=faqs)


@main.route('/diagnostica')
@admin_required
async def diagnostica():
    """Pagina di stato e diagnostica in tempo reale del sistema."""
    try:
        training_service = TrainingService.get_instance()
        diagnostics_data = await training_service.run_system_diagnostics()
        
        return render_template('pages/diagnostica.html',
                             title='Stato del Sistema - Formazing',
                             data=diagnostics_data)
                             
    except Exception as e:
        logger.error(f"Errore caricamento pagina diagnostica: {e}", exc_info=True)
        flash(f"Impossibile caricare la diagnostica: {e}", 'error')
        return redirect(url_for('main.dashboard'))



@main.route('/analytics')
@login_required
async def analytics():
    """Pagina dedicata alle statistiche e grafici delle formazioni."""
    try:
        force_refresh = request.args.get('force_refresh') == '1'
        cache_key = 'dashboard_data_notion'
        
        if force_refresh:
            cache.delete(cache_key)
            return redirect(url_for('main.analytics'))

        logger.debug("Accesso alla pagina Analytics")
        
        # Prova a recuperare i DATI dalla cache
        dashboard_data = cache.get(cache_key)
        
        if not dashboard_data:
            logger.debug("Dati non in cache. Caricamento da Notion per analytics...")
            training_service = TrainingService.get_instance()
            dashboard_data = await training_service.notion_service.get_dashboard_data()
            cache.set(cache_key, dashboard_data, timeout=600)
            
        # Elaborazione tramite il nuovo AnalyticsService
        analytics_service = AnalyticsService()
        analytics_data = analytics_service.get_analytics_data(dashboard_data)
        
        # Recupero link dashboard esterna (Excel) da Proteus
        feedback_url = proteus.get('APP.LINKS.FEEDBACK_DASHBOARD')
        
        return render_template('pages/analytics.html',
                             data=analytics_data,
                             feedback_dashboard_url=feedback_url,
                             title='Analytics - Formazing')
                             
    except Exception as e:
        logger.error(f"Errore caricamento analytics: {e}", exc_info=True)
        flash(f"Errore caricamento grafici: {e}", 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/loading')
def loading():
    """Pagina di transizione per il caricamento."""
    next_url = safe_local_path(request.args.get('next'))
    
    # Se un utente accede direttamente senza parametri, mostriamo messaggi simpatici
    if not next_url:
        import random
        easter_eggs = [
            ("Ehi, cosa ci fai qui?", "Non c'è niente da caricare... ti riporto alla Dashboard!"),
            ("Ti sei perso?", "Questa è la 'Stanza del Caricamento', ma sembra sia vuota. Torniamo indietro..."),
            ("Cercando il senso della vita...", "Non l'ho trovato, ma ho trovato la strada per la Dashboard."),
            ("Pausa caffè per il server?", "Il server sta prendendo un espresso, ti riporto alla Dashboard intanto.")
        ]
        title, message = random.choice(easter_eggs)
        next_url = url_for('main.dashboard')
    else:
        title = request.args.get('title', 'Elaborazione in corso...')[:120]
        message = request.args.get('message', 'Attendere prego')[:240]
    
    return render_template('pages/loading.html', 
                         next_url=next_url,
                         loading_title=title,
                         loading_message=message,
                         title='Caricamento - Formazing')


# === PAGINE PREVIEW CON FORM CONFERMA ===

@main.route('/preview/notification/<training_id>')
@admin_required
async def preview_notification_page(training_id):
    """Pagina preview calendarizzazione con form conferma."""
    try:
        logger.info(f"Preview calendarizzazione richiesta | Training ID: {training_id}")
        
        # Usa Singleton TrainingService
        training_service = TrainingService.get_instance()
        preview_data = await training_service.generate_preview(training_id)
        
        logger.info(f"Preview generata | Formazione: {preview_data['training'].get('Nome', 'N/A')}")
        
        # Renderizza template preview
        return render_template('pages/preview.html',
                             preview=preview_data,
                             action_type='notification',
                             action_title='Calendarizzazione Formazione',
                             action_icon='📅',
                             training_id=training_id,
                             title=f"Preview - {preview_data['training']['Nome']}")
        
    except TrainingServiceError as e:
        logger.error(f"Errore preview calendarizzazione | Training ID: {training_id} | Error: {e}")
        flash(f'Errore: {e}', 'error')
        return redirect(url_for('main.dashboard'))
    except Exception as e:
        logger.error(f"Errore imprevisto preview calendarizzazione | Training ID: {training_id} | Error: {e}", exc_info=True)
        flash(f'Errore imprevisto: {e}', 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/preview/feedback/<training_id>')
@admin_required
async def preview_feedback_page(training_id):
    """Pagina preview richiesta feedback con form conferma."""
    try:
        logger.info(f"Preview feedback richiesta | Training ID: {training_id}")
        
        # Usa Singleton TrainingService
        training_service = TrainingService.get_instance()
        preview_data = await training_service.generate_feedback_preview(training_id)
        
        logger.info(f"Preview feedback generata | Formazione: {preview_data['training'].get('Nome', 'N/A')}")
        
        # Renderizza template preview
        return render_template('pages/preview.html',
                             preview=preview_data,
                             action_type='feedback',
                             action_title='Richiesta Feedback',
                             action_icon='📝',
                             training_id=training_id,
                             title=f"Preview Feedback - {preview_data['training']['Nome']}")
        
    except TrainingServiceError as e:
        logger.error(f"Errore preview feedback | Training ID: {training_id} | Error: {e}")
        flash(f'Errore: {e}', 'error')
        return redirect(url_for('main.dashboard'))
    except Exception as e:
        logger.error(f"Errore imprevisto preview feedback | Training ID: {training_id} | Error: {e}", exc_info=True)
        flash(f'Errore imprevisto: {e}', 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/confirm/notification/<training_id>', methods=['POST'])
@admin_required
async def confirm_notification(training_id):
    """Conferma ed esegue calendarizzazione con supporto a testi personalizzati."""
    attempt = None
    try:
        logger.info(f"Conferma calendarizzazione | Training ID: {training_id}")
        
        # 1. Recupero testi personalizzati dal form
        custom_email_body = request.form.get('email_body')
        
        # Recupero messaggi Telegram (dinamici per numero di gruppi)
        custom_messages = {}
        for key in request.form:
            if key.startswith('telegram_msg_'):
                index = key.split('_')[-1]
                # Recuperiamo la chiave del gruppo corrispondente (es. 'IT', 'main_group')
                # Inviata come input hidden nel template
                group_key = request.form.get(f'telegram_group_{index}')
                if group_key:
                    custom_messages[group_key] = request.form.get(key)
        
        logger.debug(f"Testi personalizzati ricevuti: Email={bool(custom_email_body)}, Telegram={len(custom_messages)} gruppi")

        # 2. Esecuzione tramite Service
        journal = OperationJournal.configured()
        actor = session['user'].get('preferred_username', '').casefold()
        attempt = journal.begin(training_id, 'notification', actor)
        training_service = TrainingService.get_instance()
        result = await training_service.send_training_notification(
            training_id, 
            custom_messages=custom_messages,
            custom_email_body=custom_email_body,
            progress=attempt
        )
        
        logger.info(f"Calendarizzazione completata | ID: {training_id} | Codice: {result.get('codice_generato', 'N/A')}")
        
        # Invalida la cache dei dati dashboard
        cache.delete('dashboard_data_notion')
        
        flash('Comunicazione inviata con successo! La formazione è stata calendarizzata.', 'success')
        return redirect(url_for('main.dashboard'))
        
    except DuplicateOperation as e:
        flash(str(e), 'warning')
        return redirect(url_for('main.operations'))
    except TrainingServiceError as e:
        if attempt:
            attempt.review()
        logger.error(f"Errore conferma calendarizzazione: {e}")
        flash(f'Errore: {e}', 'error')
        return redirect(url_for('main.dashboard'))
    except Exception as e:
        if attempt:
            attempt.review()
        logger.error(f"Errore imprevisto conferma calendarizzazione: {e}", exc_info=True)
        flash(f'Errore imprevisto: {e}', 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/confirm/feedback/<training_id>', methods=['POST'])
@admin_required
async def confirm_feedback(training_id):
    """Conferma ed esegue invio feedback con supporto a messaggi personalizzati."""
    attempt = None
    try:
        logger.info(f"Conferma invio feedback | Training ID: {training_id}")
        
        # 1. Recupero messaggi Telegram personalizzati
        custom_messages = {}
        for key in request.form:
            if key.startswith('telegram_msg_'):
                index = key.split('_')[-1]
                group_key = request.form.get(f'telegram_group_{index}')
                if group_key:
                    custom_messages[group_key] = request.form.get(key)

        # 2. Esecuzione tramite Service
        journal = OperationJournal.configured()
        actor = session['user'].get('preferred_username', '').casefold()
        attempt = journal.begin(training_id, 'feedback', actor)
        training_service = TrainingService.get_instance()
        result = await training_service.send_feedback_request(
            training_id,
            custom_messages=custom_messages,
            progress=attempt
        )
        
        logger.info(f"Feedback inviato con successo | ID: {training_id}")
        
        # Invalida la cache dei dati dashboard
        cache.delete('dashboard_data_notion')
        
        flash('Richiesta feedback inviata con successo! La formazione è stata conclusa.', 'success')
        return redirect(url_for('main.dashboard'))
        
    except DuplicateOperation as e:
        flash(str(e), 'warning')
        return redirect(url_for('main.operations'))
    except TrainingServiceError as e:
        if attempt:
            attempt.review()
        logger.error(f"Errore conferma feedback: {e}")
        flash(f'Errore: {e}', 'error')
        return redirect(url_for('main.dashboard'))
    except Exception as e:
        if attempt:
            attempt.review()
        logger.error(f"Errore imprevisto conferma feedback: {e}", exc_info=True)
        flash(f'Errore imprevisto: {e}', 'error')
        return redirect(url_for('main.dashboard'))


@main.route('/sync-attendance/<training_id>', methods=['POST'])
@admin_required
async def sync_attendance(training_id):
    """Sincronizza manualmente i partecipanti da Teams per una formazione conclusa."""
    try:
        logger.info(f"Richiesta sincronizzazione partecipanti | ID: {training_id}")
        
        training_service = TrainingService.get_instance()
        result = await training_service.sync_attendance_from_teams(training_id)
        
        # Invalida la cache dei dati dashboard
        cache.delete('dashboard_data_notion')
        
        flash(result['message'], 'success')
        return redirect(url_for('main.dashboard'))
        
    except TrainingServiceError as e:
        logger.error(f"Errore sincronizzazione partecipanti | ID: {training_id} | Error: {e}")
        flash(f"Errore sincronizzazione: {e}", 'error')
        return redirect(url_for('main.dashboard'))
    except Exception as e:
        logger.error(f"Errore imprevisto sincronizzazione partecipanti | ID: {training_id} | Error: {e}", exc_info=True)
        flash(f"Errore imprevisto durante la sincronizzazione: {e}", 'error')
        return redirect(url_for('main.dashboard'))



# --- CENTRO OPERAZIONI: journal persistente / recovery selettivo ---

@main.route('/operations')
@admin_required
def operations():
    journal = OperationJournal.configured()
    operation_id = request.args.get('id', '')[:64]
    selected = journal.get(operation_id) if operation_id else None
    events = journal.events(operation_id) if selected else []
    return render_template(
        'pages/operations.html',
        records=journal.recent(),
        selected=selected,
        events=events,
        title='Centro Operazioni - Formazing',
    )


@main.route('/operations/<operation_id>/reconcile-notion', methods=['POST'])
@admin_required
async def reconcile_operation_notion(operation_id):
    """Retry only Notion after checking its remote state and persisted receipt."""
    actor = session['user'].get('preferred_username', '').casefold()
    try:
        journal = OperationJournal.configured()
        await reconcile_notion(
            journal, operation_id, actor, TrainingService.get_instance()
        )
        cache.delete('dashboard_data_notion')
        flash('Notion riconciliato. Verifica lo stato e i passaggi ancora pendenti.', 'success')
    except (InvalidTransition, ValueError) as exc:
        flash(str(exc), 'warning')
    except Exception:
        logger.exception('Riconciliazione Notion non completata')
        flash('Operazione bloccata: controlla la situazione su Notion e nei provider.', 'error')
    return redirect(url_for('main.operations', id=operation_id))


@main.route('/operations/<operation_id>/resume-telegram', methods=['POST'])
@admin_required
async def resume_operation_telegram(operation_id):
    """Never resend a batch whose outcome is already attempted or unknown."""
    actor = session['user'].get('preferred_username', '').casefold()
    try:
        journal = OperationJournal.configured()
        results = await resume_unsent_telegram(
            journal, operation_id, actor, TrainingService.get_instance()
        )
        flash(f'Invio completato per {len(results)} gruppi. Nessun batch ripetuto.', 'success')
    except (InvalidTransition, ValueError) as exc:
        flash(str(exc), 'warning')
    except Exception:
        logger.exception('Invio Telegram da recovery non completato')
        flash('Invio incerto. Verifica direttamente Telegram prima di qualsiasi intervento.', 'error')
    return redirect(url_for('main.operations', id=operation_id))


# --- API ENDPOINT: LOG VIEWER ---

@main.route('/api/logs')
@admin_required
def api_logs():
    """
    Endpoint JSON che restituisce le righe di log nell'intervallo temporale
    specificato dai parametri query 'start' e 'end'.

    Query params:
        start (str, optional): Datetime ISO inizio intervallo (es. '2026-08-01T00:00').
                               Default: inizio del giorno corrente.
        end   (str, optional): Datetime ISO fine intervallo (es. '2026-08-07T23:59').
                               Default: fine del giorno corrente.

    Returns:
        JSON: { "logs": [...], "count": N, "start": "...", "end": "..." }
              oppure { "error": "..." } in caso di parametri non validi.
    """
    # --- Parsing dei parametri di input ---
    today = date.today()
    default_start = datetime(today.year, today.month, today.day, 0, 0, 0)
    default_end   = datetime(today.year, today.month, today.day, 23, 59, 59)

    start_str = request.args.get('start', '').strip()
    end_str   = request.args.get('end', '').strip()

    # Formati accettati dai datetime-local HTML: 'YYYY-MM-DDTHH:MM' o 'YYYY-MM-DD HH:MM'
    datetime_formats = ['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']

    start_dt = default_start
    if start_str:
        for fmt in datetime_formats:
            try:
                start_dt = datetime.strptime(start_str, fmt)
                break
            except ValueError:
                continue
        else:
            logger.warning(f"Parametro 'start' non valido: '{start_str}'")
            return jsonify({'error': f"Formato 'start' non valido: '{start_str}'. Usa YYYY-MM-DDTHH:MM"}), 400

    end_dt = default_end
    if end_str:
        for fmt in datetime_formats:
            try:
                end_dt = datetime.strptime(end_str, fmt)
                break
            except ValueError:
                continue
        else:
            logger.warning(f"Parametro 'end' non valido: '{end_str}'")
            return jsonify({'error': f"Formato 'end' non valido: '{end_str}'. Usa YYYY-MM-DDTHH:MM"}), 400

    if start_dt > end_dt:
        return jsonify({'error': "'start' non può essere successivo a 'end'"}), 400

    # Bound both the time window and the returned response. Without limits an
    # admin request could exhaust worker memory by loading many rotated files.
    if end_dt - start_dt > timedelta(days=31):
        return jsonify({'error': 'Intervallo massimo: 31 giorni.'}), 400

    level = request.args.get('level', 'ALL').upper()
    query = request.args.get('q', '').strip()
    limit_raw = request.args.get('limit', '200')
    if level not in {'ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
        return jsonify({'error': 'Livello di log non valido.'}), 400
    if len(query) > 100:
        return jsonify({'error': 'Ricerca troppo lunga (massimo 100 caratteri).'}), 400
    if not limit_raw.isdecimal() or not 1 <= int(limit_raw) <= 500:
        return jsonify({'error': 'Il limite deve essere un intero tra 1 e 500.'}), 400

    result = LogService().get_filtered_logs(
        start_dt=start_dt, end_dt=end_dt,
        level=level, query=query, limit=int(limit_raw),
    )
    result.update({
        'start': start_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'end': end_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'level': level,
    })
    return jsonify(result)

