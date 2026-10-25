import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import threading

import pytest

from hub.app import create_app
from hub.core import DEFAULTS, Store, atomic, schedule_due, validate_settings
from hub.google import Google, GoogleError
from hub.worker import Engine


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/'state')


def test_schedule_runs_once_across_repeated_dst_hour():
    settings=copy.deepcopy(DEFAULTS)
    settings['drive'].update(enabled=True,time='02:30')
    first=dt.datetime(2026,10,25,0,35,tzinfo=dt.timezone.utc)
    second=dt.datetime(2026,10,25,1,35,tzinfo=dt.timezone.utc)
    slot=schedule_due(settings,None,None,first)
    assert slot=='2026-10-25:02:30'
    assert schedule_due(settings,slot,None,second) is None


def test_weekly_and_interval_schedule():
    settings=copy.deepcopy(DEFAULTS)
    settings['drive'].update(enabled=True,schedule='weekly',weekdays=[0],time='03:00')
    sunday=dt.datetime(2026,10,25,12,tzinfo=dt.timezone.utc)
    assert schedule_due(settings,None,None,sunday) is None
    settings['drive'].update(schedule='interval',interval_hours=24)
    assert schedule_due(settings,None,(sunday-dt.timedelta(hours=23)).isoformat(),sunday) is None
    assert schedule_due(settings,None,(sunday-dt.timedelta(hours=25)).isoformat(),sunday).startswith('interval:')


@pytest.mark.parametrize('folder',['../elsewhere','/../elsewhere','drive:other','a\nb','a//b'])
def test_repository_path_rejects_traversal_and_ini_injection(folder):
    with pytest.raises(ValueError):
        validate_settings({'drive':{'folder':folder}},copy.deepcopy(DEFAULTS))


def test_repository_change_requires_reinitialization_and_key_confirmation():
    old=copy.deepcopy(DEFAULTS)
    old.update(repository_ready=True,recovery_key_saved=True)
    old['drive']['enabled']=True
    new=validate_settings({'drive':{'folder':'A-new-target'}},old)
    assert not new['repository_ready'] and not new['recovery_key_saved'] and not new['drive']['enabled']


@pytest.mark.parametrize('schedule',['daily','interval'])
def test_switching_from_empty_weekdays_to_another_schedule_is_allowed(schedule):
    new=validate_settings({'drive':{'schedule':schedule,'weekdays':[]}},copy.deepcopy(DEFAULTS))
    assert new['drive']['schedule']==schedule


def setup_source(store,tmp_path):
    source=tmp_path/'source'; source.mkdir()
    atomic(source/'.pi-backup-hub-source.json',{'uuid':'test-uuid'})
    atomic(store.root/'bootstrap.json',{'source_uuid':'test-uuid','allow_same_device':True,
        'path_mappings':[{'container':'/usr/src/app/upload','local':str(source)}]})
    engine=Engine(store,Google(store)); engine.source=source
    return engine,source


def test_missing_or_wrong_drive_fails_closed(store,tmp_path):
    engine,source=setup_source(store,tmp_path)
    assert engine.source_info()['ok']
    atomic(source/'.pi-backup-hub-source.json',{'uuid':'different-drive'})
    with pytest.raises(RuntimeError):engine.require_source()
    (source/'.pi-backup-hub-source.json').unlink()
    assert not engine.source_info()['ok']


def test_media_path_stays_within_explicit_readonly_mappings(store,tmp_path):
    engine,source=setup_source(store,tmp_path)
    assert engine.media_path('/usr/src/app/upload/user/image.jpg')==source/'user/image.jpg'
    for name in ['/etc/passwd','/usr/src/app/upload/../../private','relative/file']:
        with pytest.raises(ValueError):engine.media_path(name)


def test_restart_does_not_blindly_repeat_uncertain_google_import(store):
    with store.db() as db:
        db.execute("INSERT INTO media(asset_id,checksum,kind,name,path,status) VALUES('id','abc','IMAGE','a.jpg','/a','creating')")
    restarted=Store(store.root)
    with restarted.db() as db:
        row=db.execute('SELECT status,error FROM media').fetchone()
    assert row['status']=='needs_review' and row['error']


def add_media(store,source,content=b'test-original-contents'):
    path=source/'original.jpg'; path.write_bytes(content)
    with store.db() as db:
        db.execute('''INSERT INTO media(asset_id,checksum,kind,name,path,size,width,height)
            VALUES(?,?,?,?,?,?,1000,1000)''',('11111111-1111-1111-1111-111111111111',hashlib.sha1(content).hexdigest(),
            'IMAGE','original.jpg','/usr/src/app/upload/original.jpg',len(content)))
        return dict(db.execute('SELECT * FROM media').fetchone())


class Response:
    def __init__(self,value=None,headers=None,text=''):
        self.value=value; self.headers=headers or {}; self.text=text
    def json(self):return self.value


class PhotosMock:
    def __init__(self,ambiguous=False,network_after_bytes=False,empty_result=False):
        self.ambiguous=ambiguous; self.network_after_bytes=network_after_bytes; self.empty_result=empty_result
        self.uploaded=b''; self.creates=0; self.started=0; self.remote_name=None
    def request(self,purpose,method,url,**kwargs):
        assert purpose=='photos'
        if url.endswith('/uploads'):
            self.started+=1
            return Response(headers={'X-Goog-Upload-URL':'https://photoslibrary.googleapis.com/v1/session',
                                     'X-Goog-Upload-Chunk-Granularity':'256'})
        if url.endswith('/session'):
            command=kwargs['headers']['X-Goog-Upload-Command']
            if command=='query':
                return Response(headers={'X-Goog-Upload-Status':'final'},text='upload-token')
            self.uploaded+=kwargs['data']
            if self.network_after_bytes:
                self.network_after_bytes=False
                raise GoogleError('Connection interrupted after server accepted the chunk')
            return Response(text='upload-token')
        if url.endswith('/albums'):
            return Response({'id':'album-id'})
        if url.endswith('/mediaItems:batchCreate'):
            self.creates+=1
            self.remote_name=kwargs['json']['newMediaItems'][0]['simpleMediaItem']['fileName']
            if self.ambiguous:
                raise GoogleError('Connection interrupted after import could have committed')
            if self.empty_result:
                return Response({})
            return Response({'newMediaItemResults':[{'mediaItem':{'id':'remote-id'}}]})
        if url.endswith('/mediaItems'):
            return Response({'mediaItems':[{'filename':self.remote_name,'id':'remote-id'}]})
        raise AssertionError(url)


def test_original_upload_preserves_bytes_and_records_completion(store,tmp_path):
    engine,source=setup_source(store,tmp_path)
    media=add_media(store,source)
    settings=store.settings();settings['photos']['enabled']=True;store.save_settings(settings)
    fake=PhotosMock();engine.google=fake
    before=(source/'original.jpg').read_bytes()
    engine._upload(media)
    with store.db() as db:row=db.execute('SELECT * FROM media').fetchone()
    assert row['status']=='uploaded' and row['media_id']=='remote-id'
    assert fake.uploaded==before==(source/'original.jpg').read_bytes()
    assert fake.creates==1 and media['asset_id'] in fake.remote_name


def test_network_interruption_resumes_without_reuploading_accepted_bytes(store,tmp_path):
    engine,source=setup_source(store,tmp_path);media=add_media(store,source)
    settings=store.settings();settings['photos']['enabled']=True;store.save_settings(settings)
    fake=PhotosMock(network_after_bytes=True);engine.google=fake
    engine._upload(media)
    assert media['status']=='retry' and fake.creates==0
    engine._upload(media)
    assert media['status']=='uploaded' and fake.started==1 and fake.creates==1
    assert fake.uploaded==(source/'original.jpg').read_bytes()


def test_ambiguous_import_waits_for_reconciliation(store,tmp_path):
    engine,source=setup_source(store,tmp_path);media=add_media(store,source)
    settings=store.settings();settings['photos']['enabled']=True;store.save_settings(settings)
    fake=PhotosMock(ambiguous=True);engine.google=fake
    engine._upload(media)
    assert media['status']=='needs_review' and fake.creates==1
    assert engine.reconcile()==1
    with store.db() as db:assert db.execute('SELECT status FROM media').fetchone()['status']=='uploaded'
    assert fake.creates==1


def test_empty_import_response_does_not_trigger_blind_duplicate(store,tmp_path):
    engine,source=setup_source(store,tmp_path);media=add_media(store,source)
    settings=store.settings();settings['photos']['enabled']=True;store.save_settings(settings)
    fake=PhotosMock(empty_result=True);engine.google=fake
    engine._upload(media)
    assert media['status']=='needs_review' and fake.creates==1


def test_corrupt_original_is_not_sent_to_google(store,tmp_path):
    engine,source=setup_source(store,tmp_path);media=add_media(store,source)
    (source/'original.jpg').write_bytes(b'x'*media['size'])
    fake=PhotosMock();engine.google=fake
    engine._upload(media)
    assert media['status']=='source_mismatch' and fake.started==0


def test_baseline_is_imported_once_and_secrets_are_not_plaintext(tmp_path):
    directory=tmp_path/'state';directory.mkdir()
    atomic(directory/'baseline-assets.json',[{'id':'id','checksum':'abc','type':'IMAGE','name':'a.jpg','path':'/a'}])
    first=Store(directory);first.update_secrets(photos_oauth={'refresh_token':'private-refresh-token'})
    second=Store(directory)
    with second.db() as db:assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==1
    assert second.get('baseline_imported')['count']==1
    assert b'private-refresh-token' not in (directory/'vault.enc').read_bytes()


def test_login_csrf_and_state_do_not_expose_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('HUB_ALLOWED_HOSTS','localhost')
    app=create_app(start_workers=False,directory=tmp_path/'app')
    client=app.test_client()
    assert client.get('/api/state').status_code==401
    login=client.get('/login');token=re.search(r'name="csrf" value="([^"]+)"',login.text).group(1)
    password=(tmp_path/'app/admin-password.txt').read_text().split('Passwort: ')[1].strip()
    assert client.post('/login',data={'username':'admin','password':password}).status_code==403
    assert client.post('/login',data={'username':'admin','password':password,'csrf':token}).status_code==302
    assert client.post('/api/settings',json={}).status_code==403
    store=app.extensions['store'];store.update_secrets(google_client={'client_id':'test','client_secret':'never-send-this-secret'},
        drive_oauth={'refresh_token':'never-send-this-token','email':'test@example.com'})
    response=client.get('/api/state')
    assert response.status_code==200
    assert 'never-send-this' not in response.text and password not in response.text
    assert client.get('/api/state',headers={'Host':'attacker.example'}).status_code==400


def test_google_client_requires_desktop_type_and_rejects_control_characters(store):
    google=Google(store)
    with pytest.raises(ValueError):google.configure_client({'web':{}})
    with pytest.raises(ValueError):google.configure_client({'installed':{'client_id':'123.apps.googleusercontent.com','client_secret':'bad\nsecret'}})


def test_pairing_proof_rejects_unknown_flow_without_contacting_google(store):
    google=Google(store)
    with pytest.raises(ValueError):google.finish_flow({'flow_id':'unknown','pairing_token':'wrong','tokens':{}})


def test_concurrent_oauth_callbacks_do_not_overwrite_the_successful_connection(store,monkeypatch):
    google=Google(store)
    google.configure_client({'installed':{'client_id':'123.apps.googleusercontent.com','client_secret':'desktop-secret'}})
    flow=google.new_flow('drive')
    barrier=threading.Barrier(2)
    class OAuthResponse:
        status_code=200
        def __init__(self,value):self.value=value
        def json(self):return self.value
    def get(url,**kwargs):
        if url.endswith('/tokeninfo'):
            return OAuthResponse({'aud':'123.apps.googleusercontent.com','scope':'https://www.googleapis.com/auth/drive.file'})
        token=kwargs['headers']['Authorization'].split()[-1]
        barrier.wait(timeout=5)
        return OAuthResponse({'email':token+'@example.com'})
    monkeypatch.setattr('hub.google.requests.get',get)
    successes=[];errors=[]
    def connect(token):
        try:
            value=google.finish_flow({'flow_id':flow['flow_id'],'pairing_token':flow['pairing_token'],
                                     'tokens':{'access_token':token,'refresh_token':'refresh-'+token}})
            successes.append((token,value['email']))
        except ValueError as error:
            errors.append(str(error))
    threads=[threading.Thread(target=connect,args=(token,)) for token in ['account-a','account-b']]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=10)
    assert len(successes)==len(errors)==1
    assert store.secrets()['drive_oauth']['access_token']==successes[0][0]
    assert store.get('drive_account')==successes[0][1]
