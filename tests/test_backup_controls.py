import io
import json
import re
import signal
import threading
import time

import pytest

from hub.app import create_app
from hub.core import Store, now
from hub.google import Google
from hub.telemetry import UploadMeter
from hub.worker import Engine, Paused


def engine_and_job(tmp_path, status='running', kind='backup'):
    store=Store(tmp_path/'state')
    engine=Engine(store,Google(store))
    with store.db() as db:
        job=db.execute('INSERT INTO jobs(kind,status,created,progress) VALUES(?,?,?,?)',
                       (kind,status,now(),json.dumps({'bytes_done':1234}))).lastrowid
    return engine,store,job


def record(store,job):
    with store.db() as db:
        return dict(db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone())


def test_pause_reserves_repository_until_resumed_or_cancelled(tmp_path):
    engine,store,job=engine_and_job(tmp_path)
    engine.pause(job)
    assert record(store,job)['status']=='pausing'
    assert engine.busy()
    with pytest.raises(ValueError):
        engine.submit('check')
    with pytest.raises(ValueError):
        engine.resume(job)


@pytest.mark.parametrize('status',['success','failed','paused','pausing'])
def test_only_active_backup_can_be_paused(tmp_path,status):
    engine,store,job=engine_and_job(tmp_path,status)
    with pytest.raises(ValueError):
        engine.pause(job)
    assert record(store,job)['status']==status


def test_repository_check_cannot_be_paused(tmp_path):
    engine,store,job=engine_and_job(tmp_path,kind='check')
    with pytest.raises(ValueError):
        engine.pause(job)


def test_paused_job_survives_restart_and_resume_reuses_its_id(tmp_path):
    engine,store,job=engine_and_job(tmp_path,status='paused')
    restarted=Store(store.root)
    restored=Engine(restarted,Google(restarted))
    assert restored.busy()
    restored.resume(job)
    assert restored.jobs.get_nowait()==(job,'backup')
    resumed=record(restarted,job)
    assert resumed['status']=='queued' and resumed['progress'] is None
    with restarted.db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_restart_during_pause_keeps_backup_paused(tmp_path):
    engine,store,job=engine_and_job(tmp_path,status='pausing')
    restarted=Store(store.root)
    assert record(restarted,job)['status']=='paused'
    assert record(restarted,job)['finished'] is None


def test_cancel_paused_job_releases_repository(tmp_path):
    engine,store,job=engine_and_job(tmp_path,status='paused')
    engine.cancel(job)
    assert record(store,job)['status']=='cancelled'
    assert not engine.busy()


def test_queued_pause_does_not_start_backup_process(tmp_path):
    engine,store,job=engine_and_job(tmp_path,status='queued')
    engine.jobs.put((job,'backup'))
    engine.pause(job)
    engine._job_backup=lambda job:pytest.fail('A queued pause must not start the backup')
    worker=threading.Thread(target=engine._job_loop,daemon=True)
    worker.start()
    deadline=time.monotonic()+3
    while record(store,job)['status']!='paused' and time.monotonic()<deadline:
        time.sleep(0.01)
    engine.stop.set()
    worker.join(timeout=2)
    assert record(store,job)['status']=='paused'


def test_pause_signals_restic_before_marking_it_paused(tmp_path,monkeypatch):
    engine,store,job=engine_and_job(tmp_path,status='pausing')
    class Process:
        pid=123
        def __init__(self):
            self.stdout=io.StringIO('')
            self.signals=[]
        def send_signal(self,value):
            self.signals.append(value)
        def wait(self,timeout=None):
            return 130
        def poll(self):
            return 130 if self.signals else None
    process=Process()
    monkeypatch.setattr('hub.worker.subprocess.Popen',lambda *args,**kwargs:process)
    with pytest.raises(Paused):
        engine._run(['restic','backup','--json'],job)
    assert process.signals==[signal.SIGINT]


def test_upload_meter_measures_network_bytes_not_processed_source_size():
    counter=[0]
    clock=[0]
    meter=UploadMeter(lambda:counter[0],lambda:clock[0])
    meter.sample()
    assert meter.snapshot()['bytes_per_second'] is None
    counter[0]=4_000_000
    clock[0]=2
    meter.sample()
    assert meter.snapshot()['bytes_per_second']==2_000_000
    clock[0]=9
    assert meter.snapshot()['bytes_per_second'] is None
    counter[0]=10
    meter.sample()
    assert meter.snapshot()['bytes_per_second'] is None


def test_small_temporary_filesystem_is_rejected_before_backup(tmp_path,monkeypatch):
    engine,store,job=engine_and_job(tmp_path)
    class Space:
        free=128*1024**2
    monkeypatch.setattr('hub.worker.shutil.disk_usage',lambda path:Space())
    with pytest.raises(RuntimeError,match='temporäre Backup-Arbeitsbereich'):
        engine.require_temp_space()
    Space.free=512*1024**2
    engine.require_temp_space()


def test_full_restic_temporary_area_is_explained_in_job_error(tmp_path,monkeypatch):
    engine,store,job=engine_and_job(tmp_path)
    class Process:
        pid=123
        stdout=io.StringIO(json.dumps({'message_type':'exit_error','code':1,'message':'Fatal: unable to save snapshot: Write: write /tmp/restic-temp-pack-123: no space left on device'})+'\n')
        def wait(self,timeout=None):return 1
        def poll(self):return 1
    monkeypatch.setattr('hub.worker.subprocess.Popen',lambda *args,**kwargs:Process())
    with pytest.raises(RuntimeError,match='temporäre Backup-Arbeitsbereich voll'):
        engine._run(['restic','backup','--json'],job)


def test_pause_api_requires_login_csrf_and_correct_job_type(tmp_path,monkeypatch):
    monkeypatch.setenv('HUB_ALLOWED_HOSTS','localhost')
    app=create_app(start_workers=False,directory=tmp_path/'app')
    client=app.test_client()
    assert client.post('/api/jobs/1/pause').status_code==401
    response=client.get('/login')
    csrf=re.search(r'name="csrf" value="([^"]+)"',response.text).group(1)
    password=(tmp_path/'app/admin-password.txt').read_text().split('Passwort: ')[1].strip()
    client.post('/login',data={'username':'admin','password':password,'csrf':csrf})
    response=client.get('/')
    csrf=re.search(r'name="csrf-token" content="([^"]+)"',response.text).group(1)
    with app.extensions['store'].db() as db:
        job=db.execute("INSERT INTO jobs(kind,status,created) VALUES('backup','running',?)",(now(),)).lastrowid
    assert client.post(f'/api/jobs/{job}/pause').status_code==403
    assert client.post(f'/api/jobs/{job}/pause',headers={'X-CSRF-Token':csrf}).status_code==200
    with app.extensions['store'].db() as db:
        assert db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0]=='pausing'
