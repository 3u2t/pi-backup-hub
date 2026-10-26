import contextlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC).isoformat()


def atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, indent=2).encode()
    with open(temp, 'wb') as output:
        os.chmod(temp, 0o600)
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


DEFAULTS = {
    'timezone': 'Europe/Berlin',
    'drive': {'enabled': False, 'folder': 'Pi-Backup-Hub', 'schedule': 'daily', 'time': '03:00',
              'weekdays': [0, 1, 2, 3, 4, 5, 6], 'interval_hours': 24,
              'upload_mib': 0, 'exclude': [], 'retention_enabled': False,
              'keep_daily': 14, 'keep_weekly': 8, 'keep_monthly': 12},
    'photos': {'enabled': False, 'poll_seconds': 60, 'include_videos': True,
               'upload_mib': 0, 'album_name': 'Immich – automatische Sicherung'},
    'recovery_key_saved': False,
    'repository_ready': False,
}


class Store:
    def __init__(self, directory=None):
        self.root = Path(directory or os.environ.get('HUB_DATA', '/data'))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        for name in ['runtime', 'staging', 'cache', 'restore-tests']:
            (self.root/name).mkdir(exist_ok=True, mode=0o700)
        key = self.root/'vault.key'
        if not key.exists():
            atomic(key, Fernet.generate_key())
        self.fernet = Fernet(key.read_bytes())
        self.secret_file = self.root/'vault.enc'
        if not self.secret_file.exists():
            self.save_secrets({'session_key': secrets.token_hex(32), 'restic_password': secrets.token_urlsafe(36)})
        settings_file = self.root/'settings.json'
        if not settings_file.exists():
            atomic(settings_file, copy.deepcopy(DEFAULTS))
        auth_file = self.root/'auth.json'
        if not auth_file.exists():
            password = secrets.token_urlsafe(18)
            atomic(auth_file, {'username': 'admin', 'password_hash': generate_password_hash(password)})
            atomic(self.root/'admin-password.txt', ('Benutzer: admin\nPasswort: '+password+'\n').encode())
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
              status TEXT NOT NULL,created TEXT NOT NULL,started TEXT,finished TEXT,summary TEXT,progress TEXT);
            CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER,
              created TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media(asset_id TEXT NOT NULL,checksum TEXT NOT NULL,
              kind TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,created TEXT,updated TEXT,
              size INTEGER,width INTEGER,height INTEGER,algorithm TEXT NOT NULL DEFAULT 'sha1',
              verified_mtime INTEGER,status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,next_retry REAL NOT NULL DEFAULT 0,error TEXT,
              session_url TEXT,session_at REAL,granularity INTEGER,uploaded INTEGER NOT NULL DEFAULT 0,
              upload_token TEXT,token_at REAL,media_id TEXT,remote_name TEXT,finished TEXT,
              PRIMARY KEY(asset_id,checksum));
            CREATE INDEX IF NOT EXISTS media_status ON media(status,next_retry);
            CREATE TABLE IF NOT EXISTS oauth_flows(id TEXT PRIMARY KEY,purpose TEXT NOT NULL,
              token_hash TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            ''')
            db.execute("UPDATE jobs SET status='interrupted',finished=?,summary='Durch Neustart unterbrochen. Daten bleiben erhalten.' WHERE status IN ('running','queued')", (now(),))
            db.execute("UPDATE jobs SET status='paused',finished=NULL,summary='Pausiert. Bereits gespeicherte Daten werden beim Fortsetzen wiederverwendet.' WHERE status='pausing'")
            db.execute("UPDATE media SET status='pending' WHERE status='uploading'")
            db.execute("UPDATE media SET status='needs_review',error='Import durch Neustart unterbrochen. Bitte Google-Status abgleichen.' WHERE status='creating'")
        baseline = self.root/'baseline-assets.json'
        if baseline.exists() and self.get('baseline_imported') is None:
            items = json.loads(baseline.read_text())
            with self.db() as db:
                db.executemany('''INSERT OR IGNORE INTO media(asset_id,checksum,kind,name,path,created,updated,status,finished)
                  VALUES(?,?,?,?,?,?,?,'baseline',?)''',
                  [(x['id'],x['checksum'],x['type'],x['name'],x['path'],x.get('created'),x.get('updated'),now()) for x in items])
            self.set('baseline_imported', {'count': len(items), 'at': now()})

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.root/'hub.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def secrets(self):
        with self.lock:
            return json.loads(self.fernet.decrypt(self.secret_file.read_bytes()))

    def save_secrets(self, value):
        with self.lock:
            atomic(self.secret_file, self.fernet.encrypt(json.dumps(value).encode()))

    def update_secrets(self, **values):
        with self.lock:
            data = self.secrets()
            data.update(values)
            self.save_secrets(data)

    def settings(self):
        with self.lock:
            result = copy.deepcopy(DEFAULTS)
            saved = json.loads((self.root/'settings.json').read_text())
            for key, value in saved.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key].update(value)
                else:
                    result[key] = value
            return result

    def save_settings(self, value):
        with self.lock:
            atomic(self.root/'settings.json', value)

    def bootstrap(self):
        p = self.root/'bootstrap.json'
        return json.loads(p.read_text()) if p.exists() else {'path_mappings': []}

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))

    def log(self, job, message, level='info'):
        with self.db() as db:
            db.execute('INSERT INTO logs(job_id,created,level,message) VALUES(?,?,?,?)', (job,now(),level,str(message)[:2500]))
            db.execute('DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0)-15000 FROM logs)')


def validate_settings(value, old):
    result = copy.deepcopy(old)
    timezone = value.get('timezone', old['timezone'])
    try:
        ZoneInfo(timezone)
    except Exception:
        raise ValueError('Ungültige Zeitzone.')
    result['timezone'] = timezone
    for section in ['drive','photos']:
        incoming = value.get(section,{})
        if not isinstance(incoming,dict):
            raise ValueError('Ungültige Einstellungen.')
        for name in result[section]:
            if name in incoming:
                result[section][name] = incoming[name]
    drive, photos = result['drive'], result['photos']
    for obj,keys in [(drive,['enabled','retention_enabled']),(photos,['enabled','include_videos'])]:
        if any(not isinstance(obj[k],bool) for k in keys):
            raise ValueError('Schalter müssen wahr oder falsch sein.')
    folder = str(drive['folder']).strip().strip('/')
    if not folder or len(folder)>180 or ':' in folder or '\\' in folder or any(x in ['.','..',''] for x in folder.split('/')):
        raise ValueError('Drive-Ziel muss ein relativer Ordnername ohne .. oder Doppelpunkt sein.')
    if any(ord(c)<32 for c in folder):
        raise ValueError('Ungültiger Drive-Ordnername.')
    drive['folder'] = folder
    if drive['schedule'] not in ['daily','weekly','interval']:
        raise ValueError('Ungültiger Zeitplan.')
    try:
        dt.datetime.strptime(drive['time'],'%H:%M')
    except Exception:
        raise ValueError('Uhrzeit muss HH:MM sein.')
    if not isinstance(drive['weekdays'],list) or any(type(x) is not int or x not in range(7) for x in drive['weekdays']):
        raise ValueError('Mindestens einen gültigen Wochentag wählen.')
    if not drive['weekdays']:
        if drive['schedule']=='weekly':
            raise ValueError('Mindestens einen Wochentag wählen.')
        drive['weekdays']=[0,1,2,3,4,5,6]
    for obj,key,low,high in [(drive,'interval_hours',1,720),(drive,'keep_daily',1,365),
        (drive,'keep_weekly',1,104),(drive,'keep_monthly',1,120),(photos,'poll_seconds',15,3600)]:
        if type(obj[key]) is not int or not low<=obj[key]<=high:
            raise ValueError(f'{key}: Wert muss zwischen {low} und {high} liegen.')
    for obj in [drive,photos]:
        if type(obj['upload_mib']) not in [int,float] or not 0<=obj['upload_mib']<=1000:
            raise ValueError('Upload-Limit muss zwischen 0 und 1048,576 Megabyte pro Sekunde liegen.')
    if not isinstance(drive['exclude'],list) or len(drive['exclude'])>100:
        raise ValueError('Maximal 100 Ausnahmen sind erlaubt.')
    for item in drive['exclude']:
        if not isinstance(item,str) or not item or len(item)>300 or item.startswith('/') or '\n' in item or '..' in item.split('/'):
            raise ValueError('Ausnahmen müssen relative Pfade oder Muster sein.')
    if not isinstance(photos['album_name'],str) or not 1<=len(photos['album_name'])<=100:
        raise ValueError('Albumname: 1 bis 100 Zeichen.')
    if folder != old['drive']['folder']:
        result['repository_ready'] = False
        result['recovery_key_saved'] = False
        drive['enabled'] = False
    return result


def schedule_due(settings, last_slot, last_started, moment=None):
    moment = moment or dt.datetime.now(UTC)
    local = moment.astimezone(ZoneInfo(settings['timezone']))
    drive = settings['drive']
    if not drive['enabled']:
        return None
    if drive['schedule'] == 'interval':
        last = dt.datetime.fromisoformat(last_started) if last_started else None
        if last is None or (moment-last).total_seconds()>=drive['interval_hours']*3600:
            return f'interval:{int(moment.timestamp())}'
        return None
    if drive['schedule']=='weekly' and local.weekday() not in drive['weekdays']:
        return None
    hour,minute = map(int,drive['time'].split(':'))
    due = local.replace(hour=hour,minute=minute,second=0,microsecond=0)
    slot = local.date().isoformat()+':'+drive['time']
    return slot if local>=due and slot!=last_slot else None
