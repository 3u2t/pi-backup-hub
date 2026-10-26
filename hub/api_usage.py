import datetime as dt
import json
import math
import re
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .core import UTC, now


class ApiUsage:
    def __init__(self, store):
        self.store=store
        self.last_cleanup=0
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS api_usage(service TEXT NOT NULL,bucket INTEGER NOT NULL,requests INTEGER NOT NULL,PRIMARY KEY(service,bucket))')
            db.execute('CREATE INDEX IF NOT EXISTS api_usage_expiry ON api_usage(bucket)')
            db.execute('INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)',('api_usage_started',json.dumps(now())))

    def record(self, service, count=1, moment=None):
        if service not in ['drive','photos'] or count<=0:
            return
        moment=time.time() if moment is None else moment
        with self.store.db() as db:
            self._add(db,service,int(count),int(moment))
            if moment-self.last_cleanup>3600:
                db.execute('DELETE FROM api_usage WHERE bucket<?',(int(moment)-172800,))
                self.last_cleanup=moment

    def _add(self, db, service, count, bucket):
        db.execute('INSERT INTO api_usage(service,bucket,requests) VALUES(?,?,?) ON CONFLICT(service,bucket) DO UPDATE SET requests=requests+excluded.requests',
                   (service,bucket,count))

    def observe_rclone(self, metrics, moment=None):
        moment=time.time() if moment is None else moment
        identity=re.search(r'^process_start_time_seconds\s+(\S+)',metrics,re.M)
        if not identity:
            return False
        total=0
        for line in metrics.splitlines():
            match=re.fullmatch(r'rclone_http_status_code\{([^}]+)\}\s+(\S+)',line)
            if not match:
                continue
            labels=dict(re.findall(r'(\w+)="([^"\\]*)"',match[1]))
            if labels.get('host','').split(':')[0] not in ['www.googleapis.com','drive.googleapis.com','content.googleapis.com']:
                continue
            value=float(match[2])
            if math.isfinite(value) and value>=0:
                total+=int(value)
        with self.store.db() as db:
            row=db.execute('SELECT value FROM kv WHERE key=?',('api_rclone_position',)).fetchone()
            previous=json.loads(row['value']) if row else {}
            delta=total-previous.get('total',0) if previous.get('instance')==identity[1] and total>=previous.get('total',0) else total
            if delta:
                self._add(db,'drive',delta,int(moment))
            position={'instance':identity[1],'total':total,'seen_at':dt.datetime.fromtimestamp(moment,UTC).isoformat()}
            db.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('api_rclone_position',json.dumps(position)))
        return True

    def snapshot(self, current=None):
        current=current or dt.datetime.now(UTC)
        local=current.astimezone(ZoneInfo('America/Los_Angeles'))
        start=local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(UTC)
        result={'started_at':self.store.get('api_usage_started'),'services':{},'measured_at':current.isoformat()}
        with self.store.db() as db:
            for service in ['photos','drive']:
                minute=db.execute('SELECT COALESCE(sum(requests),0) n FROM api_usage WHERE service=? AND bucket>? AND bucket<=?',
                                  (service,current.timestamp()-60,current.timestamp())).fetchone()['n']
                day=db.execute('SELECT COALESCE(sum(requests),0) n FROM api_usage WHERE service=? AND bucket>=? AND bucket<=?',
                               (service,start.timestamp(),current.timestamp())).fetchone()['n']
                result['services'][service]={'minute':minute,'day':day}
        result['drive_observation']=self.store.get('api_rclone_position')
        return result


def counts_as_api(purpose, url):
    parsed=urlparse(url)
    return purpose in ['photos','drive'] and (parsed.hostname or '').endswith('.googleapis.com')
