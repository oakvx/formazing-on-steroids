#!/usr/bin/env python3
"""
🚀 Formazing - App Gestione Formazioni
Entry point principale per l'applicazione Flask

UTILIZZO:
python run.py

ACCESSO:
http://localhost:5000
Username/Password: configurabili in config.py
"""

from app import create_app
from config import proteus

# Crea l'applicazione Flask
app = create_app()

if __name__ == '__main__':
    port = proteus.get('FLASK.PORT', 5000, cast=int)
    debug = proteus.get('FLASK.DEBUG_MODE', False, cast=bool)
    host = proteus.get('FLASK.HOST', '127.0.0.1')
    if debug and host not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('Il debugger Flask deve rimanere su loopback: disattiva DEBUG_MODE prima di esporre il servizio.')
    
    print("=" * 60)
    print("🚀 FORMAZING - APP GESTIONE FORMAZIONI")
    print("=" * 60)
    print(f"📍 URL: http://localhost:{port}")
    print(f"🔐 Auth: Basic Auth richiesta")
    print(f"🏠 Home: Dashboard formazioni")
    print("=" * 60)
    
    # Avvia il server Flask
    app.run(
        host=host,
        port=port,
        debug=debug,
        threaded=True
    )