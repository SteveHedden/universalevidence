"""Isolated host simulation: no Docker daemon, network or production writes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from copy import deepcopy

spec = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'deploy/release.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.calls = []
        old, new = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
        self.p = dict(schema=1, project='fixture', source_checkout=str(self.root), source_commit='c'*40,
                      old_image=old, new_image=new, loader_image='loader:pinned',
                      api_container='api', runtime_bundle=str(self.root/'runtime'),
                      current_compose=str(self.root/'current.json'), candidate_compose=str(self.root/'candidate.json'),
                      receipt_dir=str(self.root/'receipt'), static_root=str(self.root/'stage'),
                      webroot=str(self.root/'web'), lock_file=str(self.root/'lock'), health_attempts=1, health_interval=0)
        (self.root/'runtime').write_bytes(b'private bundle')
        self.p['runtime_sha256'] = r.sha(self.root/'runtime')
        self.conf = {'name':'fixture', 'services':{'api':{'image':old,'environment':{'SECRET':'preserved'},'volumes':['data:/data:ro']},'loader':{'image':'loader:pinned'},'fuseki':{'image':'db:pinned'}},'volumes':{'data':{'name':'existing'}}}
        self.save_config('current_compose', self.conf)
        candidate = deepcopy(self.conf);candidate['services']['api']['image'] = new
        self.save_config('candidate_compose', candidate)
        (self.root/'web/assets').mkdir(parents=True)
        (self.root/'stage/assets').mkdir(parents=True)
        (self.root/'web/index.html').write_bytes(b'old page')
        (self.root/'web/assets/old.js').write_bytes(b'keep old')
        (self.root/'stage/index.html').write_bytes(b'new page')
        (self.root/'stage/assets/new.js').write_bytes(b'new asset')
        self.p['static']={name:{'kind':kind,'sha256':r.sha(self.root/'stage'/name),'before_sha256':r.sha(self.root/'web'/name) if (self.root/'web'/name).exists() else None} for name,kind in [('index.html','page'),('assets/new.js','asset')]}
        labels={'com.docker.compose.config-hash':'baseline-hash','com.docker.compose.project':'fixture','com.docker.compose.project.config_files':self.p['current_compose']}
        self.api={'Id':'api-id','Image':old,'Config':{'Env':['SECRET=preserved'],'Labels':labels},'Mounts':[{'Source':'data','Destination':'/data','RW':False}],'State':{'Health':{'Status':'healthy'},'StartedAt':'original','Status':'running'}}
        self.db={'Id':'db-id','Image':'db:pinned','State':{'StartedAt':'unchanged','Status':'running'}}
        self.fail_health = False

    def save_config(self, key, value):
        path=Path(self.p[key]);path.write_text(json.dumps(value));path.chmod(0o600)
        self.p[key+'_sha256']=r.sha(path)

    def fake_run(self, args):
        self.calls.append(args)
        if args[0]=='git':
            return self.p['source_commit'] if 'rev-parse' in args else ''
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':self.p['new_image'],'Config':{'Labels':{'org.opencontainers.image.revision':self.p['source_commit'],'com.universalevidence.runtime-sha256':self.p['runtime_sha256']}}}])
        if args[:2]==['docker','ps']:return 'api-id\ndb-id\n'
        if args[:2]==['docker','inspect']:
            return json.dumps([self.db if args[2]=='db-id' else self.api])
        if args[:2]==['docker','compose']:
            path=Path(args[5]);conf=json.loads(path.read_text())
            if args[6:] == ['config','--hash','api']:return 'api baseline-hash'
            if args[6]=='config':return json.dumps(conf)
            self.assertEqual(args[6:],['up','-d','--no-deps','--no-build','api'])
            self.api['Image']=conf['services']['api']['image']
            self.api['State']['Health']['Status']='unhealthy' if self.fail_health and self.api['Image']==self.p['new_image'] else 'healthy'
            return ''
        raise AssertionError(args)

    def release(self, apply=True):return r.Release(self.p, self.fake_run).execute(apply)
    def assert_no_activation(self):self.assertFalse(any('up' in c for c in self.calls))

    def test_dry_run(self):
        self.assertEqual(self.release(False)['status'],'validated');self.assert_no_activation()
        self.assertFalse(Path(self.p['receipt_dir']).exists())

    def test_success_preserves_data_and_publishes_assets_first(self):
        writes=[];atomic=r.atomic_bytes
        def capture(data,path,mode=0o644):
            if Path(path).is_relative_to(self.root/'web'):writes.append(str(Path(path).relative_to(self.root/'web')))
            return atomic(data,path,mode)
        with patch.object(r,'atomic_bytes',side_effect=capture):self.assertEqual(self.release()['status'],'deployed')
        self.assertEqual(writes,['assets/new.js','index.html'])
        self.assertEqual((self.root/'web/assets/old.js').read_bytes(),b'keep old')
        self.assertEqual(self.db['State']['StartedAt'],'unchanged')
        self.assertEqual((self.root/'receipt/compose.rollback.json').stat().st_mode & 0o777,0o600)

    def test_wrong_baseline(self):
        self.api['Image']='different'
        with self.assertRaises(r.ReleaseError):self.release()
        self.assert_no_activation()

    def test_running_configuration_drift(self):
        self.api['Config']['Labels']['com.docker.compose.config-hash']='other'
        with self.assertRaises(r.ReleaseError):self.release()
        self.assert_no_activation()

    def test_missing_bundle(self):
        self.p['runtime_bundle']=str(self.root/'missing')
        with self.assertRaises(OSError):self.release()
        self.assert_no_activation()

    def test_config_drift(self):
        c=json.loads(Path(self.p['candidate_compose']).read_text());c['services']['api']['environment']['SECRET']='changed'
        self.save_config('candidate_compose',c)
        with self.assertRaises(r.ReleaseError):self.release()
        self.assert_no_activation()

    def test_lock_contention(self):
        with r.lock(self.p['lock_file']):
            with self.assertRaises(r.ReleaseError):self.release()
        self.assert_no_activation()

    def test_static_baseline_drift(self):
        (self.root/'web/index.html').write_bytes(b'concurrent edit')
        with self.assertRaises(r.ReleaseError):self.release()
        self.assert_no_activation()

    def test_image_provenance(self):
        original=self.fake_run
        def wrong(args):
            if args[:3]==['docker','image','inspect']:return json.dumps([{'Id':self.p['new_image'],'Config':{'Labels':{}}}])
            return original(args)
        with self.assertRaises(r.ReleaseError):r.Release(self.p,wrong).execute(True)
        self.assert_no_activation()

    def test_health_failure_rolls_back(self):
        self.fail_health=True
        with self.assertRaises(r.ReleaseError):self.release()
        self.assertEqual(self.api['Image'],self.p['old_image'])
        self.assertEqual(json.loads((self.root/'receipt/receipt.json').read_text())['status'],'rolled_back')
        self.assertEqual((self.root/'web/index.html').read_bytes(),b'old page')

    def test_partial_publication_failure_rolls_back(self):
        atomic=r.atomic_bytes;failed=False
        def fail(data,path,mode=0o644):
            nonlocal failed
            atomic(data,path,mode)
            if Path(path)==self.root/'web/index.html' and not failed:
                failed=True;raise OSError('simulated disk failure after page replacement')
        with patch.object(r,'atomic_bytes',side_effect=fail):
            with self.assertRaises(r.ReleaseError):self.release()
        self.assertEqual((self.root/'web/index.html').read_bytes(),b'old page')
        self.assertTrue((self.root/'web/assets/new.js').exists())
        self.assertEqual(self.api['Image'],self.p['old_image'])

    def test_failed_rollback_is_reported(self):
        self.fail_health=True
        original=self.fake_run
        def fail_rollback(args):
            if 'up' in args and str(args[5]).endswith('compose.rollback.json'):
                raise r.ReleaseError('simulated rollback failure')
            return original(args)
        with self.assertRaises(r.ReleaseError):r.Release(self.p,fail_rollback).execute(True)
        receipt=json.loads((self.root/'receipt/receipt.json').read_text())
        self.assertEqual(receipt['status'],'rollback_failed')
        self.assertTrue(receipt['failures'])

    def test_api_environment_drift_rolls_back(self):
        original=self.fake_run
        def mutate(args):
            result=original(args)
            if 'up' in args:
                self.api['Config']['Env']=['SECRET=changed'] if self.api['Image']==self.p['new_image'] else ['SECRET=preserved']
            return result
        with self.assertRaises(r.ReleaseError):r.Release(self.p,mutate).execute(True)
        self.assertEqual(self.api['Image'],self.p['old_image'])
        self.assertEqual(self.api['Config']['Env'],['SECRET=preserved'])

    def test_new_page_removed_on_failure(self):
        (self.root/'stage/new.html').write_bytes(b'new page')
        self.p['static']['new.html']={'kind':'page','sha256':r.sha(self.root/'stage/new.html'),'before_sha256':None}
        atomic=r.atomic_bytes;failed=False
        def fail(data,path,mode=0o644):
            nonlocal failed
            atomic(data,path,mode)
            if Path(path)==self.root/'web/new.html' and not failed:
                failed=True;raise OSError('simulated post-write failure')
        with patch.object(r,'atomic_bytes',side_effect=fail):
            with self.assertRaises(r.ReleaseError):self.release()
        self.assertFalse((self.root/'web/new.html').exists())
        self.assertEqual((self.root/'web/index.html').read_bytes(),b'old page')

    def test_path_traversal_and_symlinks(self):
        for name in ('../escape','/absolute'):
            with self.assertRaises(r.ReleaseError):r.child(self.root,name)
        (self.root/'link').symlink_to(self.root/'stage')
        with self.assertRaises(r.ReleaseError):r.child(self.root,'link/index.html')

    def test_existing_asset_cannot_be_overwritten(self):
        (self.root/'web/assets/new.js').write_bytes(b'old asset')
        self.p['static']['assets/new.js']['before_sha256']=r.sha(self.root/'web/assets/new.js')
        with self.assertRaises(r.ReleaseError):self.release()
        self.assert_no_activation()


if __name__=='__main__':unittest.main()
