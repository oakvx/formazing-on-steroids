#!/usr/bin/env python3
"""
🏗️ Factory Flask App per Formazing

Configurazione centralizzata dell'applicazione Flask con:
- Basic Authentication per sicurezza
- Template engine Jinja2 
- Static files (CSS, JS, images)
- Error handling centralizzato
- Logging configurato
"""

from flask import Flask, session, request
from flask_caching import Cache
from config import proteus, setup_logging
from app.security import csrf_token, verify_csrf
import logging

# Inizializza il sistema di Caching
cache = Cache(config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 600})

# Logger per app factory
logger = logging.getLogger(__name__)

def create_app():
    """
    Factory pattern per creare l'applicazione Flask.
    
    Returns:
        Flask: Applicazione configurata e pronta
    """
    # Configura logging PRIMA di tutto (setup centralizzato via Proteus)
    setup_logging()
    logger.info("Inizializzazione Flask app Formazing...")
    
    # Crea l'app Flask
    app = Flask(__name__)
    
    # Carica configurazione critica per Flask da Proteus
    secret = proteus.get('FLASK.SECRET_KEY')
    # Flask's cookie session is signed using this secret. The previous
    # 'dev-secret-key' default allowed anyone to forge the admin flag.
    if not isinstance(secret, str) or len(secret) < 32 or secret in (
        'dev-secret-key', 'change-me', 'changeme',
    ):
        raise RuntimeError('FLASK__SECRET_KEY must be a random string of at least 32 characters.')
    app.config.update(
        SECRET_KEY=secret,
        DEBUG=proteus.get('FLASK.DEBUG_MODE', False, cast=bool),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=proteus.get('FLASK.SESSION_COOKIE_SECURE', False, cast=bool),
        MAX_CONTENT_LENGTH=512 * 1024,
    )

    @app.before_request
    def protect_unsafe_requests():
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            verify_csrf()

    @app.after_request
    def apply_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy'] = (
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"
        )
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        return response
    
    # Inizializza la cache con l'app
    cache.init_app(app)

    
    # --- CONTEXT PROCESSOR GLOBALE ---
    @app.context_processor
    def inject_user_data():
        """Rende i dati utente disponibili in tutti i template Jinja."""
        user = session.get('user')
        return {
            'current_user': user,
            'is_admin': session.get('is_admin', False),
            'app_name': 'Formazing',
            'csrf_token': csrf_token
        }

    # Registra le rotte (Blueprint principale)
    from app.routes import main
    app.register_blueprint(main)
    logger.info("Routes registrate (Blueprint 'main')")
    
    # 🎯 Inizializza Servizi Singleton all'avvio
    logger.info("Inizializzazione Servizi Singleton...")
    
    from app.services.auth_sso import AuthService
    AuthService.get_instance()
    
    from app.services.training_service import TrainingService
    training_service = TrainingService.get_instance()
    logger.info("TrainingService pronto (bot Telegram configurato)")
    
    # ✨ Filtri Jinja2 personalizzati
    @app.template_filter('format_area')
    def format_area_filter(area):
        """
        Formatta campo Area per visualizzazione in template.
        
        Gestisce sia liste che stringhe:
        - ['IT', 'R&D'] → 'IT, R&D'
        - ['IT'] → 'IT'
        - 'IT' → 'IT'
        - [] → 'N/A'
        """
        if isinstance(area, list):
            return ', '.join(area) if area else 'N/A'
        elif isinstance(area, str):
            return area if area else 'N/A'
        else:
            return 'N/A'
    
    @app.template_filter('area_color')
    def area_color_filter(area):
        """
        Ritorna la classe colore CSS associata all'area.
        """
        if not area or area == 'N/A':
            return 'bg-secondary'
            
        area_upper = area.upper()
        
        if 'IT' in area_upper:
            return 'bg-primary'
        if 'R&D' in area_upper:
            return 'bg-danger'
        if 'MARKETING' in area_upper:
            return 'bg-purple'
        if 'COMMERCIALE' in area_upper or 'SALES' in area_upper:
            return 'text-bg-warning'
        if 'LEGAL' in area_upper or 'LEGALE' in area_upper:
            return 'bg-brown'
        if 'HR' in area_upper:
            return 'bg-success'
        if 'ALL' in area_upper or 'TUTTI' in area_upper:
            return 'bg-pink'
        if 'TEST' in area_upper:
            return 'bg-info'
            
        return 'bg-secondary'
    
    logger.info("Filtri Jinja2 personalizzati registrati")
    
    # Error handlers
    @app.errorhandler(404)
    def not_found_error(error):
        from flask import request
        logger.warning(f"404 - Risorsa non trovata: {request.path}")
        return {'error': 'Risorsa non trovata'}, 404
    
    @app.errorhandler(500) 
    def internal_error(error):
        logger.error(f"500 - Errore interno: {error}")
        return {'error': 'Errore interno del server'}, 500
    
    logger.info("Error handlers configurati")
    logger.info("Flask app creata con successo e pronta all'uso")
    
    return app