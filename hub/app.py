import base64
import datetime as dt
import functools
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from .core import Store, atomic, now, validate_settings
from .google import Google
from .worker import Engine


def create_app(start_workers=True, directory=None):
    os.umask(0o077)
    store=Store(directory)
    google=Google(store)
    engine=Engine(store,google)
    app=Flask(__name__,template_folder='../templates',static_folder='../static')
    app.config.update(SECRET_KEY=store.secrets()['session_key'],MAX_CONTENT_LENGTH=1024*1024,
                      SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=8))
    app.extensions.update(store=store,google=google,engine=engine)
    failures={}; auth_lock=threading.Lock()
    allowed_hosts=set(os.environ.get('HUB_ALLOWED_HOSTS','localhost,127.0.0.1').split(','))

    def csrf():
        if 'csrf' not in session:
            session['csrf']=secrets.token_urlsafe(32)
        return session['csrf']

    @app.before_request
    def protect():
        host=request.host.split(':')[0].lower()
        if host not in allowed_hosts:
            return jsonify(error='Dieser Hostname ist für die Backup-App nicht freigegeben.'),400
        public=request.path in ['/login','/health','/api/oauth/finish'] or request.path.startswith('/static/')
        if not public and not session.get('authenticated'):
            return (jsonify(error='Bitte zuerst anmelden.'),401) if request.path.startswith('/api/') else redirect('/login')
        if request.method in ['POST','PUT','PATCH','DELETE'] and request.path!='/api/oauth/finish':
            token=request.headers.get('X-CSRF-Token') or request.form.get('csrf','')
            if not token or not secrets.compare_digest(str(session.get('csrf','')),str(token)):
                return jsonify(error='Die Sitzung ist abgelaufen. Bitte die Seite neu laden.'),403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control']='no-store'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)),400

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error='Die Datei oder Anfrage ist zu groß.'),413

    @app.errorhandler(Exception)
    def unexpected(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error,HTTPException):
            return jsonify(error=error.description),error.code

        return jsonify(error='Die Anfrage konnte nicht abgeschlossen werden. Bitte Verbindungsstatus und Protokoll prüfen.'),500

    @app.get('/health')
    def health():
        return jsonify(status='ok',version='1.0.0')

    @app.route('/login',methods=['GET','POST'])
    def login():
        message=None
        if request.method=='POST':
            ip=request.remote_addr
            with auth_lock:
                attempts=[x for x in failures.get(ip,[]) if time.time()-x<900]
            if len(attempts)>=12:
                return render_template('login.html',csrf=csrf(),error='Zu viele Versuche. Bitte 15 Minuten warten.'),429
            auth=json.loads((store.root/'auth.json').read_text())
            if request.form.get('username')==auth['username'] and check_password_hash(auth['password_hash'],request.form.get('password','')):
                session.clear(); session['authenticated']=True; session.permanent=True; csrf()
                with auth_lock: failures.pop(ip,None)
                return redirect('/')
            with auth_lock: failures[ip]=attempts+[time.time()]
            message='Benutzername oder Passwort stimmt nicht.'
        return render_template('login.html',csrf=csrf(),error=message)

    @app.post('/api/logout')
    def logout():
        session.clear(); return jsonify(ok=True)

    @app.get('/')
    def index():
        return render_template('index.html',csrf=csrf())

    @app.get('/api/state')
    def state():
        config=store.settings()
        secure=store.secrets()
        with store.db() as db:
            counts={row['status']:row['n'] for row in db.execute('SELECT status,count(*) n FROM media GROUP BY status')}
            jobs=[dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 20')]
            recent=[dict(row) for row in db.execute('''SELECT asset_id,kind,name,status,error,attempts,uploaded,size,finished
                FROM media WHERE status!='baseline' ORDER BY COALESCE(finished,created) DESC LIMIT 30''')]
            review=db.execute("SELECT count(*) n FROM media WHERE status='needs_review'").fetchone()['n']
        for job in jobs:
            if job['progress']:
                try: job['progress']=json.loads(job['progress'])
                except ValueError: job['progress']=None
        connections={name:{'connected':google.connected(name),'email':(secure.get(name+'_oauth') or {}).get('email'),
                           'connected_at':(secure.get(name+'_oauth') or {}).get('connected_at')} for name in ['drive','photos']}
        return jsonify(settings=config,source=engine.source_info(),connections=connections,
            client_configured=bool(secure.get('google_client')),project_id=(secure.get('google_client') or {}).get('project_id'),
            photos_counts=counts,media=recent,jobs=jobs,needs_review=review,
            last_scan=store.get('last_immich_scan'),baseline=store.get('baseline_imported'),
            photos_progress=store.get('photos_progress'),photos_error=store.get('photos_error'),
            last_backup_success=store.get('last_backup_success'),last_backup_started=store.get('last_backup_started'),
            last_photos_upload=store.get('last_photos_upload'),snapshots=store.get('snapshots',[]),
            last_repository_check=store.get('last_repository_check'),last_restore_test=store.get('last_restore_test'),
            drive_quota=store.get('drive_quota'),timezone=config['timezone'],server_time=now(),
            source_path=store.bootstrap().get('host_source'),version='1.0.0')

    @app.post('/api/settings')
    def settings():
        old=store.settings(); value=validate_settings(request.get_json() or {},old)
        if engine.busy() and (value['drive']!=old['drive'] or value['timezone']!=old['timezone']):
            raise ValueError('Während einer Drive-Aufgabe kann der Sicherungsplan nicht geändert werden.')
        if value['drive']['enabled'] and (not google.connected('drive') or not value['repository_ready'] or not value['recovery_key_saved']):
            raise ValueError('Zuerst Google Drive verbinden, den Schlüssel sichern und den Sicherungsspeicher vorbereiten.')
        if value['photos']['enabled'] and not google.connected('photos'):
            raise ValueError('Zuerst Google Fotos verbinden. Neue Dateien werden bereits vorgemerkt.')
        if value['drive']['enabled'] and not old['drive']['enabled']:
            local=dt.datetime.now(ZoneInfo(value['timezone']))
            hour,minute=map(int,value['drive']['time'].split(':'))
            if (local.hour,local.minute)>=(hour,minute):
                store.set('last_schedule_slot',local.date().isoformat()+':'+value['drive']['time'])
            store.set('last_schedule_activated',now())
        store.save_settings(value)
        engine.scan_event.set()
        return jsonify(ok=True)

    @app.post('/api/password')
    def password():
        value=request.get_json() or {}
        auth=json.loads((store.root/'auth.json').read_text())
        if not check_password_hash(auth['password_hash'],value.get('current','')):
            raise ValueError('Das bisherige Passwort stimmt nicht.')
        new=value.get('new','')
        if not isinstance(new,str) or len(new)<12 or len(new)>200:
            raise ValueError('Das neue Passwort muss 12 bis 200 Zeichen haben.')
        auth['password_hash']=generate_password_hash(new)
        atomic(store.root/'auth.json',auth)
        first=store.root/'admin-password.txt'
        if first.exists(): first.unlink()
        return jsonify(ok=True)

    @app.post('/api/client')
    def client():
        google.configure_client(request.get_json() or {})
        return jsonify(ok=True)

    @app.post('/api/oauth/<purpose>/helper')
    def helper(purpose):
        config=google.new_flow(purpose)
        config['base_url']=os.environ.get('HUB_PUBLIC_URL',request.url_root.rstrip('/'))
        code=(Path(__file__).parent.parent/'google_anmelden.py').read_text(encoding='utf-8')
        code=code.replace('__PAIRING_CONFIGURATION__',base64.urlsafe_b64encode(json.dumps(config).encode()).decode())
        return send_file(io.BytesIO(code.encode()),as_attachment=True,download_name='google-'+purpose+'-anmelden.py',mimetype='text/x-python')

    @app.post('/api/oauth/finish')
    def finish_oauth():
        value=google.finish_flow(request.get_json() or {})
        if value['purpose']=='drive':
            config=store.settings(); config['repository_ready']=False; config['drive']['enabled']=False; store.save_settings(config)
        return jsonify(value)

    @app.post('/api/oauth/<purpose>/disconnect')
    def disconnect(purpose):
        if purpose not in ['drive','photos']:
            raise ValueError('Unbekannte Verbindung.')
        if engine.busy() and purpose=='drive':
            raise ValueError('Zuerst die laufende Drive-Aufgabe beenden.')
        config=store.settings(); config[purpose]['enabled']=False; store.save_settings(config)
        store.update_secrets(**{purpose+'_oauth':None})
        return jsonify(ok=True)

    @app.get('/api/recovery-key')
    def recovery_key():
        config=store.settings()
        text=('PI BACKUP HUB – WIEDERHERSTELLUNGSSCHLÜSSEL\n\n'
              'Diesen Schlüssel getrennt vom Raspberry Pi aufbewahren.\n'
              'Ohne ihn kann die verschlüsselte Sicherung nicht geöffnet werden.\n\n'
              'Restic-Passwort: '+store.secrets()['restic_password']+'\n'
              'Google-Drive-Ordner: '+config['drive']['folder']+'\n'
              'Google-Konto: '+str(store.get('drive_account','noch nicht verbunden'))+'\n\n'
              'Wiederherstellung: rclone für dieses Google-Konto einrichten.\n'
              'Repository: rclone:drive:'+config['drive']['folder']+'\n'
              'Mit restic snapshots die Sicherungsstände anzeigen und restic restore\n'
              'in einen NEUEN Ordner verwenden. Hinweise liegen auch in jeder Sicherung.\n')
        return send_file(io.BytesIO(text.encode()),as_attachment=True,download_name='Pi-Backup-Wiederherstellungsschluessel.txt',mimetype='text/plain')

    @app.post('/api/recovery-key/saved')
    def key_saved():
        if (request.get_json() or {}).get('confirmed') is not True:
            raise ValueError('Bitte bestätigen, dass der Schlüssel außerhalb des Pi gespeichert wurde.')
        config=store.settings(); config['recovery_key_saved']=True; store.save_settings(config)
        return jsonify(ok=True)

    @app.post('/api/jobs/<kind>')
    def job(kind):
        return jsonify(id=engine.submit(kind)),202

    @app.post('/api/jobs/<int:job_id>/cancel')
    def cancel(job_id):
        with store.db() as db:
            row=db.execute("SELECT id FROM jobs WHERE id=? AND status IN ('queued','running')",(job_id,)).fetchone()
        if not row:
            raise ValueError('Diese Aufgabe läuft nicht mehr.')
        engine.cancelled.add(job_id)
        return jsonify(ok=True)

    @app.get('/api/logs')
    def logs():
        with store.db() as db:
            values=[dict(row) for row in db.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 200')]
        return jsonify(logs=values)

    @app.post('/api/photos/scan')
    def scan():
        engine.scan_event.set()
        return jsonify(ok=True)

    @app.post('/api/photos/retry')
    def retry():
        with store.db() as db:
            db.execute("UPDATE media SET status='pending',next_retry=0,error=NULL WHERE status='retry'")
        engine.scan_event.set()
        return jsonify(ok=True)

    @app.post('/api/photos/reconcile')
    def reconcile():
        if not google.connected('photos'):
            raise ValueError('Zuerst Google Fotos verbinden.')
        if engine.store.get('reconcile_running',False):
            raise ValueError('Ein Abgleich läuft bereits.')
        engine.reconcile_event.set()
        return jsonify(ok=True)

    if start_workers:
        engine.start()
    return app
