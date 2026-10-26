import copy
import datetime as dt
import re

from hub.app import create_app
from hub.core import Store, UTC
from hub.google import Google, SCOPES
from hub.quotas import QuotaMonitor, apply_usage, definitions, quota_day


def metric(service, name, unit, value, display='Requests'):
    return {'metric':service+'/'+name,'displayName':display,'consumerQuotaLimits':[
        {'name':'limit-'+name+unit,'unit':unit,'quotaBuckets':[{'effectiveLimit':str(value)}]}]}


def usage(service, name, points):
    return {'resource':{'labels':{'service':service}},'metric':{'labels':{'quota_metric':service+'/'+name}},
            'points':[{'interval':{'endTime':end},'value':{'int64Value':str(value)}} for end,value in points]}


def test_google_day_handles_pacific_dst_and_is_not_berlin_day():
    start,end=quota_day(dt.datetime(2026,11,1,12,tzinfo=UTC))
    assert start.hour==7 and end.hour==8
    assert end-start==dt.timedelta(hours=25)


def test_effective_limit_and_per_user_scope_are_preserved():
    item=metric('drive.googleapis.com','units','1/min/{project}',1000000,'Units')
    item['consumerQuotaLimits'][0]['quotaBuckets'][0].update(defaultLimit='2000000')
    item['consumerQuotaLimits'].append({'name':'user','unit':'1/min/{project}/{user}',
                                        'quotaBuckets':[{'effectiveLimit':'325000'}]})
    rows=definitions([item],'drive')
    series=[usage('drive.googleapis.com','units',[('2026-10-26T18:59:00Z',3200)])]
    apply_usage(rows,series,'drive.googleapis.com',dt.datetime(2026,10,26,19,tzinfo=UTC))
    assert rows[0]['limit']==1000000 and rows[0]['used']==3200
    assert rows[1]['used'] is None and rows[1]['usage_status']=='per_user_unavailable'


def test_daily_usage_excludes_previous_day_and_other_api():
    rows=definitions([metric('photoslibrary.googleapis.com','requests','1/d/{project}',10000)],'photos')
    series=[usage('photoslibrary.googleapis.com','requests',[
        ('2026-10-26T07:00:00Z',900),('2026-10-26T07:01:00Z',3),('2026-10-26T10:00:00Z',7)]),
        usage('drive.googleapis.com','requests',[('2026-10-26T10:00:00Z',7000)])]
    apply_usage(rows,series,'photoslibrary.googleapis.com',dt.datetime(2026,10,26,12,tzinfo=UTC))
    assert rows[0]['used']==10
    assert rows[0]['reset_at']=='2026-10-27T07:00:00+00:00'


def test_minutes_use_latest_bucket_and_missing_samples_are_unknown():
    rows=definitions([metric('drive.googleapis.com','units','1/min/{project}',1000000,'Units')],'drive')
    series=[usage('drive.googleapis.com','units',[
        ('2026-10-26T18:57:00Z',1000),('2026-10-26T18:58:00Z',2000)])]
    apply_usage(rows,series,'drive.googleapis.com',dt.datetime(2026,10,26,19,tzinfo=UTC))
    assert rows[0]['used']==2000
    apply_usage(rows,[], 'drive.googleapis.com',dt.datetime(2026,10,26,19,tzinfo=UTC))
    assert rows[0]['used'] is None


def test_monitor_uses_read_scopes_and_keeps_dashboard_requests_cached(tmp_path):
    store=Store(tmp_path/'state')
    store.update_secrets(google_client={'project_id':'test-project'},monitoring_oauth={'refresh_token':'private'})
    calls=[]
    class Response:
        def __init__(self,value):self.value=value
        def json(self):return self.value
    class FakeGoogle:
        def connected(self,purpose):return True
        def request(self,purpose,method,url,**kwargs):
            calls.append((purpose,method,url,kwargs))
            assert purpose=='monitoring' and method=='GET'
            if 'consumerQuotaMetrics' in url:
                service='drive.googleapis.com' if 'drive.googleapis.com' in url else 'photoslibrary.googleapis.com'
                return Response({'metrics':[metric(service,'requests','1/d/{project}',10000)]})
            return Response({'timeSeries':[usage('photoslibrary.googleapis.com','requests',[('2026-10-26T10:00:00Z',12)])]})
    monitor=QuotaMonitor(store,FakeGoogle())
    current=dt.datetime(2026,10,26,12,tzinfo=UTC)
    monitor.refresh(current)
    monitor.refresh(current+dt.timedelta(seconds=5))
    for _ in range(5):snapshot=monitor.snapshot()
    assert len(calls)==3 and snapshot['status']=='ok'
    assert snapshot['services']['photos']['limits'][0]['used']==12
    assert store.get('quota_monitor_reads')['series']==1
    assert set(SCOPES['monitoring'])=={'openid','email','https://www.googleapis.com/auth/monitoring.read',
                                         'https://www.googleapis.com/auth/cloud-platform.read-only'}


def test_monitoring_disconnect_does_not_change_backups_or_tokens(tmp_path,monkeypatch):
    monkeypatch.setenv('HUB_ALLOWED_HOSTS','localhost')
    app=create_app(start_workers=False,directory=tmp_path/'state')
    client=app.test_client()
    assert client.post('/api/quotas/refresh',json={}).status_code==401
    page=client.get('/login')
    csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
    password=(tmp_path/'state/admin-password.txt').read_text().split('Passwort: ')[1].strip()
    client.post('/login',data={'username':'admin','password':password,'csrf':csrf})
    csrf=re.search(r'name="csrf-token" content="([^"]+)"',client.get('/').text).group(1)
    store=app.extensions['store']
    store.update_secrets(drive_oauth={'refresh_token':'drive-private'},photos_oauth={'refresh_token':'photos-private'},
                         monitoring_oauth={'refresh_token':'monitor-private'})
    before=copy.deepcopy(store.settings())
    assert client.post('/api/quotas/refresh',json={}).status_code==403
    assert client.post('/api/quotas/refresh',json={},headers={'X-CSRF-Token':csrf}).status_code==200
    assert client.post('/api/oauth/monitoring/disconnect',json={},headers={'X-CSRF-Token':csrf}).status_code==200
    assert store.settings()==before
    assert Google(store).connected('drive') and Google(store).connected('photos')
    assert not Google(store).connected('monitoring')
    assert 'private' not in client.get('/api/state').text
