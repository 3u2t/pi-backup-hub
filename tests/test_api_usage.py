import datetime as dt

import pytest

from hub.api_usage import ApiUsage
from hub.core import Store, UTC
from hub.google import Google, GoogleError
from hub.quotas import QuotaMonitor, definitions


def test_rclone_counts_exclude_oauth_and_survive_counter_restart(tmp_path):
    store=Store(tmp_path/'state');counter=ApiUsage(store)
    moment=dt.datetime(2026,10,26,20,tzinfo=UTC)
    def metrics(instance,total):
        return f'process_start_time_seconds {instance}\nrclone_http_status_code{{code="200",host="www.googleapis.com",method="GET"}} {total}\nrclone_http_status_code{{code="200",host="oauth2.googleapis.com",method="POST"}} 50\n'
    assert counter.observe_rclone(metrics('100',4),moment.timestamp())
    counter.observe_rclone(metrics('100',4),moment.timestamp()+1)
    counter.observe_rclone(metrics('100',7),moment.timestamp()+2)
    counter=ApiUsage(store)
    counter.observe_rclone(metrics('100',7),moment.timestamp()+3)
    counter.observe_rclone(metrics('200',2),moment.timestamp()+4)
    snapshot=counter.snapshot(moment+dt.timedelta(seconds=5))
    assert snapshot['services']['drive']['minute']==9
    assert snapshot['services']['drive']['day']==9
    assert counter.snapshot(moment+dt.timedelta(seconds=80))['services']['drive']['minute']==0


def test_photo_counter_uses_pacific_day_and_persists(tmp_path):
    store=Store(tmp_path/'state');counter=ApiUsage(store)
    counter.record('photos',3,dt.datetime(2026,10,26,6,59,tzinfo=UTC).timestamp())
    counter.record('photos',2,dt.datetime(2026,10,26,7,1,tzinfo=UTC).timestamp())
    assert ApiUsage(store).snapshot(dt.datetime(2026,10,26,8,tzinfo=UTC))['services']['photos']['day']==2


def test_billing_block_switches_to_local_counts_without_faking_units(tmp_path):
    store=Store(tmp_path/'state')
    store.update_secrets(google_client={'project_id':'test-project'})
    calls=[]
    class Response:
        def json(self):
            return {'metrics':[{'metric':'drive.googleapis.com/total_query_cost','displayName':'Total Query Cost',
                                'consumerQuotaLimits':[{'name':'units','unit':'1/min/{project}','quotaBuckets':[{'effectiveLimit':'1000000'}]}]}]}
    class FakeGoogle:
        def connected(self,purpose):return True
        def request(self,purpose,method,url,**kwargs):
            calls.append(url)
            if 'timeSeries' in url:raise GoogleError('requires billing',403,'billing_required')
            return Response()
    monitor=QuotaMonitor(store,FakeGoogle())
    monitor.refresh()
    monitor.last_attempt=None
    monitor.refresh()
    snapshot=monitor.snapshot()
    assert len(calls)==3 and snapshot['mode']=='local' and snapshot['status']=='local'
    assert snapshot['services']['drive']['limits'][0]['title']=='Einheiten'
    assert snapshot['services']['drive']['limits'][0]['used'] is None
    assert snapshot['services']['drive']['limits'][0]['usage_status']=='units_unavailable'


def test_google_billing_error_is_classified_and_request_count_is_real(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');google=Google(store)
    monkeypatch.setattr(google,'token',lambda *args,**kwargs:'not-a-real-token')
    class Response:
        ok=False;status_code=403
        def json(self):return {'error':{'status':'PERMISSION_DENIED','message':'This API method requires billing to be enabled.'}}
    monkeypatch.setattr('hub.google.requests.request',lambda *args,**kwargs:Response())
    with pytest.raises(GoogleError) as raised:
        google.request('monitoring','GET','https://monitoring.googleapis.com/v3/projects/test-project/timeSeries',retry=False)
    assert raised.value.reason=='billing_required'
    assert google.api_usage.snapshot()['services']['photos']['day']==0
    with pytest.raises(GoogleError):
        google.request('photos','GET','https://photoslibrary.googleapis.com/v1/albums',retry=False)
    assert google.api_usage.snapshot()['services']['photos']['day']==1
