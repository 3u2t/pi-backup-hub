import datetime as dt
import re
import threading
import time
from zoneinfo import ZoneInfo

from .core import UTC, now
from .google import GoogleError
from .api_usage import ApiUsage


SERVICES = {'photos':'photoslibrary.googleapis.com','drive':'drive.googleapis.com'}


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z','+00:00'))


def quota_day(current):
    local=current.astimezone(ZoneInfo('America/Los_Angeles'))
    start=local.replace(hour=0,minute=0,second=0,microsecond=0)
    return start.astimezone(UTC),(start+dt.timedelta(days=1)).astimezone(UTC)


def definitions(metrics, purpose):
    rows=[]
    for metric in metrics:
        metric_id=metric.get('metric','')
        display=metric.get('displayName',metric_id)
        for limit in metric.get('consumerQuotaLimits',[]):
            unit=limit.get('unit','')
            if '/min/' not in unit and '/d/' not in unit:
                continue
            for bucket in limit.get('quotaBuckets',[]):
                if bucket.get('dimensions'):
                    continue
                value=int(bucket['effectiveLimit'])
                title='Medienabrufe' if 'base' in (display+' '+metric_id).lower() else 'Einheiten' if any(word in (display+' '+metric_id).lower() for word in ['unit','query_cost']) else 'Anfragen'
                period='day' if '/d/' in unit else 'minute'
                per_user='{user}' in unit
                rows.append({'id':limit['name'],'metric':metric_id,'title':title,'google_name':display,
                             'period':period,'per_user':per_user,'limit':value,'used':None,'measured_at':None,
                             'usage_status':'per_user_unavailable' if per_user else 'no_data'})
    return sorted(rows,key=lambda row:(row['per_user'],row['title']=='Medienabrufe',row['google_name'].lower().startswith('previous'),row['title']!='Einheiten',row['id']))


def apply_usage(rows, series, service, current):
    day_start,reset_at=quota_day(current)
    grouped={}
    for item in series:
        if item.get('resource',{}).get('labels',{}).get('service')!=service:
            continue
        metric=item.get('metric',{}).get('labels',{}).get('quota_metric')
        points=grouped.setdefault(metric,{})
        for point in item.get('points',[]):
            end=point.get('interval',{}).get('endTime')
            raw=point.get('value',{})
            value=raw.get('int64Value',raw.get('doubleValue'))
            if not end or value is None:
                continue
            value=float(value)
            if value<0:
                continue
            points[end]=points.get(end,0)+value
    for row in rows:
        row['used']=None;row['measured_at']=None
        row['usage_status']='per_user_unavailable' if row['per_user'] else 'no_data'
        if row['per_user']:
            continue
        points=grouped.get(row['metric'],{})
        eligible={end:value for end,value in points.items() if timestamp(end)<=current and
                  (timestamp(end)>day_start if row['period']=='day' else timestamp(end)>current-dt.timedelta(minutes=10))}
        if not eligible:
            continue
        latest=max(eligible,key=timestamp)
        row.update(used=sum(eligible.values()) if row['period']=='day' else eligible[latest],
                   measured_at=latest,usage_status='measured',reset_at=reset_at.isoformat() if row['period']=='day' else None)
    return rows


class QuotaMonitor:
    def __init__(self, store, google):
        self.store,self.google=store,google
        self.lock=threading.Lock()
        self.last_attempt=None
        self.active_until=0
        self.api_usage=ApiUsage(store)

    def snapshot(self):
        self.active_until=time.monotonic()+90
        if not self.google.connected('monitoring'):
            return {'status':'not_connected','services':{},'poll_seconds':60}
        result=self.store.get('api_quota_usage') or {'status':'loading','services':{},'poll_seconds':60}
        if result.get('mode')=='local':
            local=self.api_usage.snapshot()
            result.update(local=local,status='local')
            result.pop('error',None)
            for purpose,service in result.get('services',{}).items():
                for row in service.get('limits',[]):
                    row.update(used=None,measured_at=None,scope='app',usage_status='no_data')
                    if row['title']=='Einheiten':
                        row['usage_status']='units_unavailable'
                    elif row['per_user']:
                        row['usage_status']='per_user_unavailable'
                    elif row['title']=='Medienabrufe':
                        row['usage_status']='not_used'
                    elif purpose=='photos' or local.get('drive_observation'):
                        row.update(used=local['services'][purpose][row['period']],measured_at=local['measured_at'],usage_status='local',
                                   reset_at=quota_day(dt.datetime.now(UTC))[1].isoformat() if row['period']=='day' else None)
        return result

    def active(self):
        return time.monotonic()<self.active_until

    def _pages(self, url, key, params, metered=False):
        values=[]
        for _ in range(8):
            if metered:
                month=dt.datetime.now(UTC).strftime('%Y-%m')
                reads=self.store.get('quota_monitor_reads',{})
                if reads.get('month')!=month:
                    reads={'month':month,'series':0}
                if reads['series']>=750000:
                    raise GoogleError('Das monatliche Leselimit der Verbrauchsanzeige ist erreicht.')
            response=self.google.request('monitoring','GET',url,params=params,timeout=(10,30),retry=False).json()
            items=response.get(key,[])
            if metered:
                reads['series']+=max(1,len(items))
                self.store.set('quota_monitor_reads',reads)
            values.extend(items)
            token=response.get('nextPageToken')
            if not token:
                return values
            params={**params,'pageToken':token}
        raise GoogleError('Google liefert zu viele Messwerte. Bitte den Projektumfang prüfen.')

    def refresh(self, current=None):
        current=current or dt.datetime.now(UTC)
        if not self.google.connected('monitoring'):
            return
        with self.lock:
            if self.last_attempt and (current-self.last_attempt).total_seconds()<60:
                return
            self.last_attempt=current
            project=self.store.secrets().get('google_client',{}).get('project_id','')
            if not re.fullmatch(r'[a-z][a-z0-9-]{4,62}',project):
                self.store.set('api_quota_usage',{'status':'error','error':'Die Google-Client-Datei enthält keine gültige Projekt-ID.','services':{}})
                return
            blocked=self.store.get('quota_monitor_billing_required')==project
            previous=self.store.get('api_quota_usage') or {}
            if blocked and previous.get('mode')=='local' and previous.get('fetched_at') and (current-timestamp(previous['fetched_at'])).total_seconds()<3600:
                return
            result={'status':'ok','fetched_at':now(),'services':{},'poll_seconds':60,'project_id':project}
            errors=[]
            for purpose,service in SERVICES.items():
                try:
                    metrics=self._pages(f'https://serviceusage.googleapis.com/v1beta1/projects/{project}/services/{service}/consumerQuotaMetrics',
                                        'metrics',{'view':'FULL','pageSize':100})
                    result['services'][purpose]={'limits':definitions(metrics,purpose)}
                except (GoogleError,ValueError,KeyError) as error:
                    errors.append(self._error(error))
                    result['services'][purpose]={'limits':[],'error':self._error(error)}
            if blocked:
                result.update(status='local',mode='local')
            elif any(service['limits'] for service in result['services'].values()):
                day_start,_=quota_day(current)
                start=min(day_start,current-dt.timedelta(minutes=15))
                params={'filter':'metric.type="serviceruntime.googleapis.com/quota/rate/net_usage" AND resource.type="consumer_quota" AND (resource.labels.service="drive.googleapis.com" OR resource.labels.service="photoslibrary.googleapis.com")',
                        'interval.startTime':start.isoformat(),'interval.endTime':current.isoformat(),'view':'FULL','pageSize':10000,
                        'aggregation.alignmentPeriod':'60s','aggregation.perSeriesAligner':'ALIGN_SUM',
                        'aggregation.crossSeriesReducer':'REDUCE_SUM',
                        'aggregation.groupByFields':['resource.labels.service','metric.labels.quota_metric']}
                try:
                    series=self._pages(f'https://monitoring.googleapis.com/v3/projects/{project}/timeSeries','timeSeries',params,metered=True)
                    for purpose,value in result['services'].items():
                        apply_usage(value['limits'],series,SERVICES[purpose],current)
                except (GoogleError,ValueError,KeyError) as error:
                    if isinstance(error,GoogleError) and error.reason=='billing_required':
                        self.store.set('quota_monitor_billing_required',project)
                        result.update(status='local',mode='local')
                    else:
                        errors.append(self._error(error))
            if errors:
                result.update(status='error',error=' '.join(dict.fromkeys(errors)))
            result['fetched_at']=now()
            self.store.set('api_quota_usage',result)

    def _error(self, error):
        if isinstance(error,GoogleError):
            if error.status==403:
                return 'Google verweigert den Lesezugriff. Cloud Monitoring API und Service Usage API aktivieren und die Lesefreigabe prüfen.'
            if error.status in [400,401]:
                return 'Die Google-Lesefreigabe muss erneuert werden oder die Projekt-ID ist ungültig.'
            return str(error)
        return 'Google hat unvollständige Verbrauchsdaten geliefert.'
