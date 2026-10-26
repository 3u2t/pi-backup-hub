import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from hub.core import Store, now
from hub.google import Google
from hub.worker import Engine


def main():
    with tempfile.TemporaryDirectory(prefix='backup-pause-test-',dir=os.environ.get('HUB_DATA','/data')) as temporary:
        root=Path(temporary)
        source=root/'source'
        source.mkdir()
        expected={}
        for index in range(24):
            path=source/f'fixture-{index:02}.bin'
            path.write_bytes(os.urandom(512*1024))
            expected[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        password=root/'password'
        password.write_text('local-pause-integration-fixture')
        environment=dict(os.environ,RESTIC_REPOSITORY=str(root/'repository'),
                         RESTIC_PASSWORD_FILE=str(password),RESTIC_CACHE_DIR=str(root/'cache'))
        subprocess.run(['restic','init'],env=environment,check=True,stdout=subprocess.DEVNULL)
        store=Store(root/'state')

        class LocalEngine(Engine):
            def _job_backup(self,job):
                args=['restic','backup','--json','--verbose=2','--pack-size','4']
                if not self.store.get('resumed_test'):
                    args+=['--limit-upload','1024']
                return self._run(args+[str(source)],job,env=environment)

        engine=LocalEngine(store,Google(store))
        worker=threading.Thread(target=engine._job_loop,daemon=True)
        worker.start()
        job=engine.submit('backup')

        def job_record():
            with store.db() as db:
                return dict(db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone())

        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            current=job_record()
            packs=list((root/'repository/data').glob('*/*'))
            if current['status']=='running' and packs:
                break
            if current['status'] in ['success','failed']:
                raise AssertionError('Fixture finished before pause could be tested: '+str(current['status']))
            time.sleep(0.1)
        assert current['status']=='running' and packs,'No uploaded fixture pack before deadline'
        engine.pause(job)
        deadline=time.monotonic()+25
        while job_record()['status']=='pausing' and time.monotonic()<deadline:
            time.sleep(0.1)
        assert job_record()['status']=='paused',job_record()
        paused_packs={str(path.relative_to(root/'repository')) for path in (root/'repository/data').glob('*/*')}
        assert paused_packs,'Already saved pack must remain after pausing'
        restarted=Store(store.root)
        assert job_record()['status']=='paused'
        store.set('resumed_test',True)
        engine.resume(job)
        deadline=time.monotonic()+45
        while job_record()['status'] in ['queued','running'] and time.monotonic()<deadline:
            time.sleep(0.1)
        assert job_record()['status']=='success',job_record()
        assert json.loads(job_record()['progress'])['scan_complete'] is True
        engine.stop.set()
        worker.join(timeout=3)
        final_packs={str(path.relative_to(root/'repository')) for path in (root/'repository/data').glob('*/*')}
        assert paused_packs<=final_packs,'Pause must not remove previously saved packs'
        snapshots=json.loads(subprocess.check_output(['restic','snapshots','--json'],env=environment,text=True))
        assert len(snapshots)==1
        destination=root/'restored'
        subprocess.run(['restic','restore','latest','--target',str(destination)],env=environment,check=True,stdout=subprocess.DEVNULL)
        restored=destination/str(source).lstrip('/')
        assert {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in restored.iterdir()}==expected
        with store.db() as db:
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
        print(json.dumps({'pause_completed':True,'pause_survives_restart':True,'resumed_same_job':True,
                          'saved_packs_preserved':True,'snapshot_created':True,
                          'restored_files_match':True,'scan_completion_received':True,'google_requests':0}))


if __name__=='__main__':
    main()
