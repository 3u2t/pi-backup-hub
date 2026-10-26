import datetime as dt
import gzip
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

from .core import UTC, atomic, now, schedule_due
from .google import GoogleError
from .telemetry import UploadMeter


class Cancelled(RuntimeError):
    pass


class Paused(RuntimeError):
    pass


class Engine:
    def __init__(self, store, google):
        self.store, self.google = store, google
        self.source=Path(os.environ.get('HUB_SOURCE','/source'))
        self.jobs=queue.Queue()
        self.stop=threading.Event()
        self.scan_event=threading.Event()
        self.reconcile_event=threading.Event()
        self.cancelled=set()
        self.submit_lock=threading.Lock()
        self.threads=[]
        self.upload_meter=UploadMeter()

    def start(self):
        self.store.set('reconcile_running',False)
        for function in [self._job_loop,self._photos_loop,self._schedule_loop,self._network_loop]:
            thread=threading.Thread(target=function,daemon=True,name=function.__name__)
            thread.start(); self.threads.append(thread)

    def _network_loop(self):
        while not self.stop.is_set():
            self.upload_meter.sample()
            self.stop.wait(2)

    def source_info(self):
        bootstrap=self.store.bootstrap()
        marker=self.source/'.pi-backup-hub-source.json'
        try:
            identity=json.loads(marker.read_text())
            if not bootstrap.get('source_uuid') or identity.get('uuid')!=bootstrap['source_uuid']:
                raise ValueError('Die eingebundene Festplatte passt nicht zur eingerichteten Quelle.')
            if self.source.stat().st_dev==self.store.root.stat().st_dev and not bootstrap.get('allow_same_device',False):
                raise ValueError('Die HDD wurde nicht korrekt eingebunden; Quelle liegt auf dem Systemlaufwerk.')
            disk=shutil.disk_usage(self.source)
            return {'ok':True,'label':bootstrap.get('source_label','HDD'),'total':disk.total,'used':disk.used,
                    'free':disk.free,'uuid':identity['uuid']}
        except (OSError,ValueError) as error:
            return {'ok':False,'error':'Festplatte nicht verfügbar oder falsch eingebunden. '+str(error)[:180]}

    def require_source(self):
        result=self.source_info()
        if not result['ok']:
            raise RuntimeError(result['error'])

    def busy(self):
        with self.store.db() as db:
            return db.execute("SELECT id FROM jobs WHERE status IN ('queued','running','pausing','paused') LIMIT 1").fetchone() is not None

    def pause(self, job):
        with self.submit_lock, self.store.db() as db:
            row=db.execute('SELECT kind,status FROM jobs WHERE id=?',(job,)).fetchone()
            if not row or row['kind']!='backup' or row['status'] not in ['queued','running']:
                raise ValueError('Nur eine laufende HDD-Sicherung kann pausiert werden.')
            db.execute("UPDATE jobs SET status='pausing',summary=? WHERE id=?",('Pause wird vorbereitet. Bereits übertragene Daten werden gesichert.',job))

    def resume(self, job):
        with self.submit_lock:
            with self.store.db() as db:
                row=db.execute('SELECT kind,status FROM jobs WHERE id=?',(job,)).fetchone()
                if not row or row['kind']!='backup' or row['status']!='paused':
                    raise ValueError('Diese HDD-Sicherung ist nicht pausiert.')
                if db.execute("SELECT id FROM jobs WHERE id!=? AND status IN ('queued','running','pausing','paused')",(job,)).fetchone():
                    raise ValueError('Eine andere Drive-Aufgabe läuft bereits.')
                db.execute("UPDATE jobs SET status='queued',started=NULL,finished=NULL,summary=NULL,progress=NULL WHERE id=?",(job,))
            self.jobs.put((job,'backup'))

    def cancel(self, job):
        with self.submit_lock, self.store.db() as db:
            row=db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()
            if not row or row['status'] not in ['queued','running','pausing','paused']:
                raise ValueError('Diese Aufgabe läuft nicht mehr.')
            if row['status']=='paused':
                db.execute("UPDATE jobs SET status='cancelled',finished=?,summary=? WHERE id=?",(now(),'Vom Benutzer abgebrochen.',job))
            else:
                self.cancelled.add(job)

    def _pause_pending(self, job):
        with self.store.db() as db:
            row=db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()
            return bool(row and row['status']=='pausing')

    def submit(self, kind):
        if kind not in ['backup','init','check','snapshots','restore_test','drive_test']:
            raise ValueError('Unbekannte Aufgabe.')
        with self.submit_lock:
            if self.busy():
                raise ValueError('Eine Drive-Aufgabe läuft bereits. Bitte warten oder abbrechen.')
            with self.store.db() as db:
                job=db.execute("INSERT INTO jobs(kind,status,created) VALUES(?,'queued',?)",(kind,now())).lastrowid
            self.jobs.put((job,kind))
            return job

    def log(self, job, message, level='info'):
        message=str(message)
        secure=self.store.secrets()
        hidden=[secure.get('restic_password','')]
        for field in ['google_client','drive_oauth','photos_oauth']:
            value=secure.get(field,{}) or {}
            hidden.extend(value.get(k,'') for k in ['client_secret','access_token','refresh_token'])
        for secret in hidden:
            if secret:
                message=message.replace(secret,'[geschützt]')
        message=re.sub(r'Bearer\s+\S+','Bearer [geschützt]',message)
        self.store.log(job,message,level)

    def _job_loop(self):
        while not self.stop.is_set():
            try:
                job,kind=self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if job in self.cancelled:
                    raise Cancelled()
                with self.submit_lock:
                    if self._pause_pending(job):
                        raise Paused()
                    with self.store.db() as db:
                        db.execute("UPDATE jobs SET status='running',started=? WHERE id=?",(now(),job))
                self.log(job,{'backup':'HDD-Sicherung startet.','init':'Verschlüsselten Sicherungsspeicher vorbereiten.',
                    'check':'Sicherungsspeicher prüfen.','snapshots':'Sicherungsstände laden.',
                    'restore_test':'Immich-Datenbanksicherung testweise in einen neuen Ordner zurückholen.',
                    'drive_test':'Google-Drive-Verbindung prüfen.'}[kind])
                summary=getattr(self,'_job_'+kind)(job)
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET status='success',finished=?,summary=? WHERE id=?",(now(),summary,job))
                self.log(job,summary)
            except Paused:
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET status='paused',finished=NULL,summary=? WHERE id=?",('Pausiert. Bereits gespeicherte Daten werden beim Fortsetzen wiederverwendet.',job))
                self.log(job,'HDD-Sicherung pausiert. Beim Fortsetzen werden Dateien erneut geprüft und gespeicherte Daten wiederverwendet.')
            except Cancelled:
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET status='cancelled',finished=?,summary=? WHERE id=?",(now(),'Vom Benutzer abgebrochen.',job))
                self.log(job,'Aufgabe abgebrochen. Bereits vorhandene Sicherungen bleiben erhalten.','warning')
            except Exception as error:
                message=str(error)[:1800]
                self.log(job,message,'error')
                with self.store.db() as db:
                    db.execute("UPDATE jobs SET status='failed',finished=?,summary=? WHERE id=?",(now(),message,job))
            finally:
                self.cancelled.discard(job)
                self.jobs.task_done()

    def _run(self, args, job, env=None, collect=False):
        result=[]
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                                 env=env,start_new_session=True,bufsize=1)
        lines=queue.Queue()
        def read():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        threading.Thread(target=read,daemon=True).start()
        ended=False
        last_progress=0
        progress={}
        pause_started=None
        next_pause_check=0
        backup=args[:2]==['restic','backup']
        try:
            while not ended:
                if job in self.cancelled or self.stop.is_set():
                    os.killpg(process.pid,signal.SIGTERM)
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL); process.wait()
                    raise Cancelled()
                moment=time.monotonic()
                if backup and pause_started is None and moment>=next_pause_check:
                    next_pause_check=moment+0.5
                    if self._pause_pending(job):
                        try:
                            process.send_signal(signal.SIGINT)
                            pause_started=moment
                        except ProcessLookupError:
                            pass
                if pause_started is not None and moment-pause_started>120:
                    os.killpg(process.pid,signal.SIGTERM)
                    process.wait(timeout=10)
                    raise Paused()
                try:
                    line=lines.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    ended=True; continue
                if collect:
                    result.append(line)
                    if sum(len(x) for x in result)>12*1024*1024:
                        raise RuntimeError('Antwort der Sicherungssoftware ist unerwartet groß.')
                else:
                    try:
                        value=json.loads(line)
                    except ValueError:
                        self.log(job,line.strip())
                        continue
                    if value.get('message_type')=='status':
                        progress.update(value)
                        progress['updated_at']=now()
                        if time.monotonic()-last_progress>2:
                            with self.store.db() as db:
                                db.execute('UPDATE jobs SET progress=? WHERE id=?',(json.dumps(progress),job))
                            last_progress=time.monotonic()
                    elif value.get('message_type')=='verbose_status':
                        if value.get('action')=='scan_finished':
                            progress['scan_complete']=True
                            progress['total_bytes']=value.get('data_size',progress.get('total_bytes',0))
                            progress['total_files']=value.get('total_files',progress.get('total_files',0))
                            progress['updated_at']=now()
                            with self.store.db() as db:
                                db.execute('UPDATE jobs SET progress=? WHERE id=?',(json.dumps(progress),job))
                    else:
                        self.log(job,json.dumps(value,ensure_ascii=False))
                        if value.get('snapshot_id'):
                            self.store.set('last_snapshot_id',value['snapshot_id'])
            code=process.wait()
            if pause_started is not None and code!=0:
                raise Paused()
            if code!=0:
                detail=''.join(result)[-1800:] if collect else 'Details stehen im Protokoll.'
                if code==3 and args[0]=='restic':
                    raise RuntimeError('Einige Quelldateien konnten nicht vollständig gelesen werden. Die Sicherung gilt als unvollständig. '+detail)
                raise RuntimeError(Path(args[0]).name+' meldet Fehler '+str(code)+': '+detail)
            return ''.join(result)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid,signal.SIGTERM); process.wait(timeout=10)
                except (ProcessLookupError,subprocess.TimeoutExpired):
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
            process.stdout.close()

    def _restic_env(self):
        config=self.google.rclone_config()
        password=self.store.root/'runtime'/'restic-password'
        atomic(password,self.store.secrets()['restic_password'].encode())
        env=dict(os.environ)
        env.update(RESTIC_REPOSITORY='rclone:drive:'+self.store.settings()['drive']['folder'],
                   RESTIC_PASSWORD_FILE=str(password),RESTIC_CACHE_DIR=str(self.store.root/'cache'),
                   RCLONE_CONFIG=str(config),RCLONE_TRANSFERS='2',RCLONE_DRIVE_CHUNK_SIZE='16Mi')
        return env

    def _job_drive_test(self, job):
        config=self.google.rclone_config()
        raw=self._run(['rclone','--config',str(config),'about','drive:','--json'],job,collect=True)
        self.store.set('drive_quota',json.loads(raw))
        return 'Google Drive ist erreichbar.'

    def _job_init(self, job):
        if not self.store.settings().get('recovery_key_saved'):
            raise ValueError('Bitte zuerst den Wiederherstellungsschlüssel herunterladen und bestätigen.')
        env=self._restic_env()

        try:
            self._run(['restic','cat','config'],job,env=env,collect=True)
        except RuntimeError:
            self._run(['restic','init','--repository-version','2'],job,env=env)
        settings=self.store.settings(); settings['repository_ready']=True; self.store.save_settings(settings)
        return 'Verschlüsselter Drive-Sicherungsspeicher ist bereit.'

    def _stage(self, job):
        if shutil.disk_usage(self.store.root).free<700*1024**2:
            raise RuntimeError('Zu wenig freier Speicher auf dem Systemlaufwerk. Mindestens rund 734 Megabyte werden benötigt. Keine Sicherung gestartet.')
        staging=self.store.root/'staging'
        immich=staging/'immich'; immich.mkdir(exist_ok=True,mode=0o700)
        temporary=immich/'database.dump.tmp'
        self._run(['pg_dump','--format=custom','--compress=6','--no-owner','--no-privileges',
                   '--file',str(temporary)],job)
        if temporary.stat().st_size<1000:
            raise RuntimeError('Immich-Datenbanksicherung ist unerwartet klein.')
        self._run(['pg_restore','--list',str(temporary)],job,collect=True)
        os.replace(temporary,immich/'database.dump')
        atomic(immich/'restore-mapping.json',self.store.bootstrap())
        hub=staging/'backup-hub'; hub.mkdir(exist_ok=True,mode=0o700)
        for name in ['settings.json','vault.enc','vault.key','auth.json','bootstrap.json','postgres.pgpass']:
            source=self.store.root/name
            if source.is_file():
                shutil.copy2(source,hub/name)
                os.chmod(hub/name,0o600)
        with self.store.db() as db:
            import sqlite3
            destination=sqlite3.connect(hub/'hub.sqlite3')
            try:
                db.backup(destination)
            finally:
                destination.close()
        atomic(staging/'RESTORE.txt',(
            'Pi Backup Hub - file backup\n\n'
            '/source is the root of the source disk.\n'
            '/immich-config contains the Immich deployment configuration.\n'
            '/immich-extra and /immich-external contain additional media outside the source disk.\n'
            '/data/staging/immich/database.dump is a PostgreSQL custom-format dump.\n'
            'Restore files into a new directory, then prepare a compatible Immich installation.\n'
            'Check the Immich restore instructions before importing with pg_restore.\n'
            'restore-mapping.json records the configured paths and Immich version.\n'
            'Do not import into a running database without reviewing the restore procedure.\n'
            'This backup also contains the private credentials used by the backup app.\n'
            'The separately saved recovery key is required to open the restic repository.\n'
        ).encode())


    def _job_backup(self, job):
        if self._pause_pending(job):
            raise Paused()
        self.require_source()
        settings=self.store.settings()
        if not settings['repository_ready']:
            raise ValueError('Zuerst den Drive-Sicherungsspeicher vorbereiten.')
        env=self._restic_env()
        self.store.set('last_backup_started',now())
        self.log(job,'Frischen Immich-Datenbank-Dump erstellen; danach HDD und zusätzliche Immich-Daten sichern.')
        self._stage(job)
        if self._pause_pending(job):
            raise Paused()
        self.require_source()
        args=['restic','backup','--json','--verbose=2','--host','raspberrypi','--tag','pi-backup-hub','--one-file-system',
              '--compression','auto']
        if settings['drive']['upload_mib']:
            args+=['--limit-upload',str(int(settings['drive']['upload_mib']*1024))]
        for pattern in settings['drive']['exclude']:
            args+=['--exclude','/source/'+pattern]
        args+=[str(self.source),str(self.store.root/'staging')]
        for extra in ['/immich-config','/immich-extra','/immich-external']:
            if Path(extra).exists():
                args.append(extra)
        self._run(args,job,env=env)
        self.store.set('last_backup_success',now())
        snapshots=json.loads(self._run(['restic','snapshots','--json'],job,env=env,collect=True))
        self.store.set('snapshots',snapshots)
        if settings['drive']['retention_enabled']:
            self.log(job,'Aktivierte Aufbewahrungsregeln werden nach erfolgreicher Sicherung angewendet.')
            d=settings['drive']
            self._run(['restic','forget','--host','raspberrypi','--tag','pi-backup-hub',
                '--keep-daily',str(d['keep_daily']),'--keep-weekly',str(d['keep_weekly']),
                '--keep-monthly',str(d['keep_monthly']),'--prune'],job,env=env)
        return 'HDD und Immich-Datenbank wurden erfolgreich in Google Drive gesichert.'

    def _job_check(self, job):
        self._run(['restic','check'],job,env=self._restic_env())
        self.store.set('last_repository_check',now())
        return 'Sicherungsstruktur und Verweise sind geprüft. Eine vollständige Inhaltsprüfung aller Daten war nicht Teil dieses Laufs.'

    def _job_snapshots(self, job):
        values=json.loads(self._run(['restic','snapshots','--json'],job,env=self._restic_env(),collect=True))
        self.store.set('snapshots',values)
        return str(len(values))+' Sicherungsstände gefunden.'

    def _job_restore_test(self, job):
        target=self.store.root/'restore-tests'/dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%S')
        target.mkdir(mode=0o700)
        include=str(self.store.root/'staging'/'immich'/'database.dump')
        self._run(['restic','restore','latest','--host','raspberrypi','--tag','pi-backup-hub',
                   '--target',str(target),'--include',include],job,env=self._restic_env())
        restored=target/include.lstrip('/')
        if not restored.is_file() or restored.stat().st_size<1000:
            raise RuntimeError('Die Datenbankdatei konnte nicht aus dem Backup wiederhergestellt werden.')
        self._run(['pg_restore','--list',str(restored)],job,collect=True)
        self.store.set('last_restore_test',{'at':now(),'file':str(restored),'bytes':restored.stat().st_size})
        return 'Datenbankdatei aus dem letzten Backup erfolgreich zurückgeholt und als PostgreSQL-Archiv geprüft. Die laufende Datenbank wurde nicht verändert.'

    def _schedule_loop(self):
        while not self.stop.wait(20):
            try:
                settings=self.store.settings()
                if not settings['repository_ready'] or not self.google.connected('drive') or self.busy():
                    continue
                anchors=[x for x in [self.store.get('last_backup_started'),self.store.get('last_schedule_activated')] if x]
                slot=schedule_due(settings,self.store.get('last_schedule_slot'),max(anchors) if anchors else None)
                if slot:
                    self.submit('backup')
                    self.store.set('last_schedule_slot',slot)
            except Exception as error:
                self.store.set('scheduler_error',str(error)[:300])

    def media_path(self, original):
        original=PurePosixPath(original)
        if not original.is_absolute() or '..' in original.parts:
            raise ValueError('Ungültiger Immich-Dateipfad.')
        for mapping in sorted(self.store.bootstrap().get('path_mappings',[]),key=lambda x:len(x['container']),reverse=True):
            try:
                relative=original.relative_to(mapping['container'])
            except ValueError:
                continue
            base=Path(mapping['local']).resolve()
            candidate=(base/str(relative)).resolve()
            if base not in candidate.parents:
                raise ValueError('Immich-Datei liegt außerhalb des freigegebenen Speicherordners.')
            return candidate
        raise ValueError('Für diesen Immich-Speicherpfad ist kein Lesezugriff eingerichtet.')

    def scan(self):
        self.require_source()
        cursor=self.store.get('immich_cursor',{'updated':'1970-01-01T00:00:00+00:00','id':'00000000-0000-0000-0000-000000000000'})
        added=0
        with psycopg.connect(connect_timeout=10,row_factory=dict_row,options='-c default_transaction_read_only=on') as connection:
            while not self.stop.is_set():
                rows=connection.execute('''SELECT a.id::text AS id,encode(a.checksum,'hex') AS checksum,
                  a.type AS kind,a."originalFileName" AS name,a."originalPath" AS path,
                  a."createdAt" AS created,a."updatedAt" AS updated,e."fileSizeInByte" AS size,a.width,a.height,
                  a."checksumAlgorithm" AS algorithm
                  FROM asset a LEFT JOIN asset_exif e ON e."assetId"=a.id
                  WHERE a."deletedAt" IS NULL AND NOT a."isOffline"
                  AND (a."updatedAt",a.id)>(%s::timestamptz,%s::uuid)
                  ORDER BY a."updatedAt",a.id LIMIT 500''',(cursor['updated'],cursor['id'])).fetchall()
                if not rows:
                    break
                with self.store.db() as db:
                    for row in rows:
                        inserted=db.execute('''INSERT OR IGNORE INTO media(asset_id,checksum,kind,name,path,created,updated,size,width,height,algorithm)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(row['id'],row['checksum'],row['kind'],row['name'],row['path'],
                          row['created'].isoformat(),row['updated'].isoformat(),row['size'],row['width'],row['height'],row['algorithm'])).rowcount
                        added+=inserted
                        db.execute('''UPDATE media SET path=?,name=?,updated=?,size=?,width=?,height=?,algorithm=?
                          WHERE asset_id=? AND checksum=?''',(row['path'],row['name'],row['updated'].isoformat(),
                          row['size'],row['width'],row['height'],row['algorithm'],row['id'],row['checksum']))
                last=rows[-1]
                cursor={'updated':last['updated'].isoformat(),'id':last['id']}
                self.store.set('immich_cursor',cursor)
                if len(rows)<500:
                    break
        self.store.set('last_immich_scan',{'at':now(),'new':added,'ok':True})
        return added

    def _photos_loop(self):
        next_scan=0
        while not self.stop.is_set():
            try:
                if self.reconcile_event.is_set():
                    self.reconcile_event.clear()
                    self.store.set('reconcile_running',True)
                    try:
                        resolved=self.reconcile()
                        self.store.set('last_reconcile',{'at':now(),'resolved':resolved,'ok':True})
                    except Exception as error:
                        self.store.set('last_reconcile',{'at':now(),'ok':False,'error':str(error)[:500]})
                    finally:
                        self.store.set('reconcile_running',False)
                if time.time()>=next_scan or self.scan_event.is_set():
                    self.scan_event.clear()
                    try:
                        self.scan()
                    except Exception as error:
                        self.store.set('last_immich_scan',{'at':now(),'ok':False,'error':str(error)[:500]})
                    next_scan=time.time()+self.store.settings()['photos']['poll_seconds']
                settings=self.store.settings()
                if not settings['photos']['enabled'] or not self.google.connected('photos'):
                    self.stop.wait(2); continue
                self.require_source()
                with self.store.db() as db:
                    media=db.execute("SELECT * FROM media WHERE status IN ('pending','retry') AND next_retry<=? AND (kind='IMAGE' OR ?=1) ORDER BY created,asset_id LIMIT 1",(time.time(),int(settings['photos']['include_videos']))).fetchone()
                if media is None:
                    self.stop.wait(2); continue
                self._upload(dict(media))
            except Exception as error:
                self.store.set('photos_error',{'at':now(),'message':str(error)[:500]})
                self.stop.wait(10)

    def _media_update(self, media, **values):
        allowed={'status','attempts','next_retry','error','session_url','session_at','granularity','uploaded',
                 'upload_token','token_at','media_id','remote_name','finished','size','verified_mtime'}
        assert set(values)<=allowed
        with self.store.db() as db:
            query='UPDATE media SET '+','.join(key+'=?' for key in values)+' WHERE asset_id=? AND checksum=?'
            db.execute(query,(*values.values(),media['asset_id'],media['checksum']))
        media.update(values)

    def _sealed(self, value):
        return self.store.fernet.encrypt(value.encode()).decode()

    def _unsealed(self, value):
        return self.store.fernet.decrypt(value.encode()).decode()

    def _album(self):
        title=self.store.settings()['photos']['album_name']
        saved=self.store.get('photos_album')
        if saved and saved.get('name')==title and saved.get('count',0)<19500:
            return saved
        response=self.google.request('photos','POST','https://photoslibrary.googleapis.com/v1/albums',
                                      json={'album':{'title':title+' '+dt.datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}})
        value={'id':response.json()['id'],'name':title,'count':0}
        self.store.set('photos_album',value)
        return value

    @staticmethod
    def remote_filename(media):
        name=Path(media['name']).name
        suffix=Path(name).suffix.lower()
        stem=name[:-len(suffix)] if suffix else name
        stem=re.sub(r'[\x00-\x1f/\\]','_',stem)
        while len(stem.encode('utf-8'))>150:
            stem=stem[:-1]
        return stem+'__'+media['asset_id']+'_'+media['checksum'][:12]+suffix

    def _upload(self, media):
        try:
            path=self.media_path(media['path'])
            if not path.is_file():
                raise RuntimeError('Originaldatei ist momentan nicht vorhanden.')
            size=path.stat().st_size
            if media['size'] is not None and size!=media['size']:
                raise RuntimeError('Originaldatei stimmt noch nicht mit der Immich-Dateigröße überein; später erneut versuchen.')
            if size==0 or size>(200_000_000 if media['kind']=='IMAGE' else 20_000_000_000):
                self._media_update(media,status='unsupported',error='Dateigröße wird von der Google-Fotos-API nicht unterstützt.'); return
            if media['kind']=='IMAGE' and media['width'] and media['height']:
                if media['width']<=256 or media['height']<=256 or media['width']*media['height']>200_000_000:
                    self._media_update(media,status='unsupported',error='Bildabmessungen liegen außerhalb der Google-Fotos-Vorgaben.'); return
            before=path.stat()
            if media.get('verified_mtime')!=before.st_mtime_ns:
                digest=hashlib.new(media.get('algorithm','sha1'))
                with path.open('rb') as content:
                    for block in iter(lambda:content.read(4*1024**2),b''):
                        digest.update(block)
                after=path.stat()
                if after.st_size!=before.st_size or after.st_mtime_ns!=before.st_mtime_ns:
                    raise RuntimeError('Originaldatei hat sich während der Prüfung verändert.')
                if digest.hexdigest()!=media['checksum']:
                    self._media_update(media,status='source_mismatch',error='Die Originaldatei stimmt nicht mit der Immich-Prüfsumme überein. Bitte Quelle prüfen.'); return
                self._media_update(media,verified_mtime=after.st_mtime_ns)
            self._media_update(media,status='uploading',attempts=media['attempts']+1,error=None,
                               size=size,remote_name=media['remote_name'] or self.remote_filename(media))
            mime=mimetypes.guess_type(str(path))[0] or ('image/jpeg' if media['kind']=='IMAGE' else 'video/mp4')
            token=self._unsealed(media['upload_token']) if media['upload_token'] and time.time()-(media['token_at'] or 0)<23*3600 else None
            if not token:
                session=self._unsealed(media['session_url']) if media['session_url'] and time.time()-(media['session_at'] or 0)<6*86400 else None
                offset=0
                if session:
                    try:
                        response=self.google.request('photos','POST',session,headers={'X-Goog-Upload-Command':'query','Content-Length':'0'},data=b'',retry=False)
                        upload_status=response.headers.get('X-Goog-Upload-Status','active')
                        if upload_status=='final' and response.text.strip():
                            token=response.text.strip()
                        elif upload_status!='active':
                            session=None
                        else:
                            offset=int(response.headers.get('X-Goog-Upload-Size-Received','0'))
                    except GoogleError as error:
                        if error.status in [400,404,410]:
                            session=None
                        else:
                            raise
                if not session and not token:
                    response=self.google.request('photos','POST','https://photoslibrary.googleapis.com/v1/uploads',
                       headers={'X-Goog-Upload-Command':'start','X-Goog-Upload-Protocol':'resumable',
                                'X-Goog-Upload-Content-Type':mime,'X-Goog-Upload-Raw-Size':str(size),
                                'Content-Length':'0'},data=b'')
                    session=response.headers.get('X-Goog-Upload-URL')
                    if not session or urlparse(session).scheme!='https' or not (urlparse(session).hostname or '').endswith('.googleapis.com'):
                        raise RuntimeError('Google hat keine gültige Upload-Sitzung geliefert.')
                    granularity=int(response.headers.get('X-Goog-Upload-Chunk-Granularity','262144'))
                    if granularity<1 or granularity>32*1024**2:
                        raise RuntimeError('Ungültige Google-Upload-Blockgröße.')
                    self._media_update(media,session_url=self._sealed(session),session_at=time.time(),granularity=granularity,uploaded=0)
                if not token:
                    granularity=media['granularity'] or 262144
                    chunk_size=max(granularity,(8*1024**2//granularity)*granularity)
                    with path.open('rb') as source:
                        if offset<0 or offset>size:
                            raise RuntimeError('Ungültiger Upload-Fortschritt von Google.')
                        source.seek(offset)
                        if offset==size:
                            try:
                                response=self.google.request('photos','POST',session,headers={
                                    'X-Goog-Upload-Command':'upload, finalize','X-Goog-Upload-Offset':str(offset),
                                    'Content-Length':'0'},data=b'',retry=False)
                                token=response.text.strip()
                            except GoogleError:
                                self._media_update(media,session_url=None)
                                raise
                        while offset<size:
                            if self.stop.is_set() or not self.store.settings()['photos']['enabled']:
                                self._media_update(media,status='pending'); return
                            start=time.monotonic()
                            block=source.read(min(chunk_size,size-offset))
                            if not block:
                                raise RuntimeError('Originaldatei wurde während des Uploads verkürzt.')
                            final=offset+len(block)==size
                            response=self.google.request('photos','POST',session,
                                headers={'X-Goog-Upload-Command':'upload, finalize' if final else 'upload',
                                         'X-Goog-Upload-Offset':str(offset),'Content-Length':str(len(block))},
                                data=block,retry=False)
                            offset+=len(block)
                            self._media_update(media,uploaded=offset)
                            self.store.set('photos_progress',{'asset_id':media['asset_id'],'name':media['name'],
                                                            'bytes':offset,'total':size,'at':now()})
                            limit=self.store.settings()['photos']['upload_mib']
                            if limit:
                                self.stop.wait(max(0,len(block)/(limit*1024**2)-(time.monotonic()-start)))
                            if final:
                                token=response.text.strip()
                                if not token or len(token)>16000:
                                    raise RuntimeError('Google hat kein gültiges Upload-Token geliefert.')
            self._media_update(media,upload_token=self._sealed(token),token_at=time.time())
            album=self._album()

            self._media_update(media,status='creating')
            try:
                response=self.google.request('photos','POST','https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate',
                    json={'albumId':album['id'],'newMediaItems':[{'simpleMediaItem':{
                        'uploadToken':token,'fileName':media['remote_name']}}]},retry=False)
            except GoogleError as error:
                if error.status in [400,401,403,429]:
                    self._media_update(media,status='retry',next_retry=time.time()+300,error=str(error))
                else:
                    self._media_update(media,status='needs_review',error='Google könnte den Upload bereits übernommen haben. Bitte „Google-Status abgleichen“ verwenden.')
                return
            results=response.json().get('newMediaItemResults',[])
            if len(results)!=1 or not results[0].get('mediaItem',{}).get('id'):
                code=results[0].get('status',{}).get('code') if results else None
                status='unsupported' if code==3 else 'needs_review' if not code else 'retry'
                self._media_update(media,status=status,next_retry=time.time()+300,
                                   error='Google konnte die Datei nicht importieren (Status '+str(code)+').')
                return
            self._media_update(media,status='uploaded',media_id=results[0]['mediaItem']['id'],finished=now(),
                               session_url=None,upload_token=None,error=None)
            album['count']+=1; self.store.set('photos_album',album)
            self.store.set('last_photos_upload',now())
            self.store.set('photos_error',None)
        except Exception as error:
            if media.get('status')=='creating':
                self._media_update(media,status='needs_review',error='Import-Antwort unklar. Bitte den Google-Status abgleichen.')
            else:
                self._media_update(media,status='retry',error=str(error)[:500],next_retry=time.time()+min(6*3600,30*2**min(media.get('attempts',1),9)))

    def reconcile(self):

        found={}; page=None
        while True:
            params={'pageSize':100}
            if page: params['pageToken']=page
            result=self.google.request('photos','GET','https://photoslibrary.googleapis.com/v1/mediaItems',params=params).json()
            for item in result.get('mediaItems',[]):
                found[item.get('filename','')]=item['id']
            page=result.get('nextPageToken')
            if not page: break
        resolved=0
        with self.store.db() as db:
            for row in db.execute("SELECT asset_id,checksum,remote_name FROM media WHERE status IN ('needs_review','creating')").fetchall():
                if row['remote_name'] in found:
                    db.execute("UPDATE media SET status='uploaded',media_id=?,finished=?,error=NULL,session_url=NULL,upload_token=NULL WHERE asset_id=? AND checksum=?",(found[row['remote_name']],now(),row['asset_id'],row['checksum']))
                    resolved+=1
        return resolved
