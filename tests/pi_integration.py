import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import psycopg

from hub.core import Store, atomic, now
from hub.google import Google
from hub.worker import Engine


def main():
    assert os.statvfs('/source').f_flag & os.ST_RDONLY, 'Production HDD is not mounted read-only'
    with psycopg.connect(connect_timeout=10) as connection:
        user,read_only,insert_allowed,create_allowed=connection.execute("""SELECT current_user,
          current_setting('default_transaction_read_only'),has_table_privilege(current_user,'public.asset','INSERT'),
          has_schema_privilege(current_user,'public','CREATE')""").fetchone()
    assert user=='immich_backup_hub' and read_only=='on' and not insert_allowed and not create_allowed
    results={'database_role':user,'database_read_only':True,'production_hdd_read_only':True}
    with tempfile.TemporaryDirectory(prefix='hub-selftest-',dir='/data') as temporary:
        root=Path(temporary)
        source=root/'fixture';source.mkdir()
        content=b'Pi Backup Hub restore test\n'+os.urandom(128*1024)
        filename=source/'original-test.bin';filename.write_bytes(content)
        atomic(source/'.pi-backup-hub-source.json',{'uuid':'selftest'})
        store=Store(root/'state')
        atomic(store.root/'bootstrap.json',{'source_uuid':'selftest','allow_same_device':True,
              'path_mappings':[{'container':'/fixture','local':str(source)}]})
        settings=store.settings();settings['recovery_key_saved']=True;store.save_settings(settings)
        engine=Engine(store,Google(store));engine.source=source
        password_file=root/'test-recovery-key';password_file.write_text('private-integration-test-key')
        environment=dict(os.environ,RESTIC_REPOSITORY=str(root/'repository'),
                         RESTIC_PASSWORD_FILE=str(password_file),RESTIC_CACHE_DIR=str(root/'cache'))
        engine._restic_env=lambda:environment
        with store.db() as db:
            job=db.execute("INSERT INTO jobs(kind,status,created) VALUES('selftest','running',?)",(now(),)).lastrowid
        engine._job_init(job)
        engine._job_backup(job)
        engine._job_check(job)
        engine._job_restore_test(job)
        restored=root/'restored-files'
        engine._run(['restic','restore','latest','--target',str(restored),'--include',str(filename)],job,env=environment)
        restored_file=restored/str(filename).lstrip('/')
        assert restored_file.read_bytes()==content
        wrong=root/'wrong-key';wrong.write_text('incorrect-key')
        denied=subprocess.run(['restic','snapshots'],env=dict(environment,RESTIC_PASSWORD_FILE=str(wrong)),
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        assert denied.returncode!=0
        dump=store.root/'staging/immich/database.dump'
        results.update(local_snapshot_created=True,fixture_restored_identically=True,
                       actual_immich_dump_created=True,dump_bytes=dump.stat().st_size,
                       database_archive_retrieved=True,wrong_recovery_key_rejected=True,
                       snapshots=len(store.get('snapshots',[])))
    results['temporary_test_directory_removed']=not root.exists()
    print(json.dumps(results))


if __name__=='__main__':main()
