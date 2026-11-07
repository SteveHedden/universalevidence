#!/usr/bin/env python3
"""Host-local, manifest-pinned API/static release. No builds, pulls or data loads."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


class ReleaseError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise ReleaseError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(args):
    # Never echo commands/output: effective Compose and inspect can contain secrets.
    p = subprocess.run(args, capture_output=True, text=True)
    require(p.returncode == 0, 'External command failed (output suppressed)')
    return p.stdout


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def child(root, name):
    p = Path(name)
    require(bool(name) and not p.is_absolute() and '..' not in p.parts, 'Unsafe static path')
    result = Path(root) / p
    require(not any(q.is_symlink() for q in [result, *result.parents]), 'Symlink path rejected')
    require(result.resolve().is_relative_to(Path(root).resolve()), 'Path escapes root')
    return result


def atomic_bytes(data, path, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.ue-release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def lock(path):
    # The operator supplies the EXISTING shared deployment lock from the runbook.
    with open(path, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError('Deployment lock is held') from None
        yield


class Release:
    def __init__(self, plan, run=command):
        self.p = plan
        self.run = run

    def compose(self, path, *args):
        return self.run(['docker', 'compose', '-p', self.p['project'], '-f', str(path), *args])

    def config(self, path):
        return json.loads(self.compose(path, 'config', '--format', 'json'))

    def inspect(self, name):
        return json.loads(self.run(['docker', 'inspect', name]))[0]

    def containers(self):
        ids = self.run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + self.p['project']]).split()
        return {i: self.inspect(i) for i in ids}

    def preflight(self):
        p = self.p
        require(p['schema'] == 1, 'Unsupported manifest schema')
        require(re.fullmatch(r'[0-9a-f]{40}', p['source_commit']) is not None, 'Full source commit required')
        for key in ('old_image', 'new_image'):
            require(re.fullmatch(r'sha256:[0-9a-f]{64}', p[key]) is not None, 'Immutable image ID required')
        source = p['source_checkout']
        require(self.run(['git', '-C', source, 'rev-parse', 'HEAD']).strip() == p['source_commit'], 'Source commit mismatch')
        require(not self.run(['git', '-C', source, 'status', '--porcelain', '--untracked-files=no']).strip(), 'Tracked source is dirty')
        require(sha(p['runtime_bundle']) == p['runtime_sha256'], 'Private runtime bundle mismatch')
        for field in ('current_compose', 'candidate_compose'):
            f = Path(p[field])
            require(f.is_file() and f.stat().st_mode & 0o077 == 0, 'Compose must be private (0600)')
            require(sha(f) == p[field + '_sha256'], 'Compose file hash mismatch')
        before, candidate = self.config(p['current_compose']), self.config(p['candidate_compose'])
        expected = deepcopy(before)
        expected['services']['api']['image'] = p['new_image']
        # Keep list ordering in Compose: command/entrypoint arrays are order-sensitive.
        require(candidate == expected, 'Effective Compose drift beyond API image')
        require('loader' in before['services'], 'Separately pinned loader is required')
        require(before['services']['loader'].get('image') == p['loader_image'], 'Loader pin mismatch')
        require(not Path(p['receipt_dir']).exists(), 'Receipt directory already exists; inspect before retry')
        api = self.inspect(p['api_container'])
        labels = api['Config']['Labels']
        require(labels['com.docker.compose.project'] == p['project'], 'Wrong Compose project')
        require(labels['com.docker.compose.project.config_files'] == str(Path(p['current_compose']).resolve()), 'Current Compose path differs from running API')
        require(api['Image'] == p['old_image'], 'Running API baseline mismatch')
        config_hash = self.compose(p['current_compose'], 'config', '--hash', 'api').strip().split()
        require(len(config_hash) == 2 and config_hash[0] == 'api' and
                config_hash[1] == labels.get('com.docker.compose.config-hash'), 'Running API differs from effective baseline Compose')
        require(api['State'].get('Health', {}).get('Status') == 'healthy', 'Baseline API unhealthy')
        image = json.loads(self.run(['docker', 'image', 'inspect', p['new_image']]))[0]
        require(image['Id'] == p['new_image'], 'Candidate image missing')
        labels = image['Config'].get('Labels') or {}
        require(labels.get('org.opencontainers.image.revision') == p['source_commit'], 'Image/source provenance mismatch')
        require(labels.get('com.universalevidence.runtime-sha256') == p['runtime_sha256'], 'Image/runtime provenance mismatch')
        require(bool(p['static']), 'Explicit static manifest required')
        staged = {}
        for name, item in p['static'].items():
            require(item['kind'] in ('asset', 'page'), 'Static kind must be asset or page')
            target = child(p['webroot'], name)
            require((sha(target) if target.exists() else None) == item['before_sha256'], 'Static baseline mismatch: ' + name)
            data = child(p['static_root'], name).read_bytes()
            require(hashlib.sha256(data).hexdigest() == item['sha256'], 'Static artifact mismatch: ' + name)
            if item['kind'] == 'asset':
                require(not target.exists() or item['sha256'] == item['before_sha256'], 'Assets must use new names; retain old bytes')
            staged[name] = data
        self.before, self.candidate, self.api = before, candidate, api
        self.rollback = deepcopy(before)
        self.rollback['services']['api']['image'] = p['old_image']
        self.others = {k: v for k, v in self.containers().items() if v['Id'] != api['Id']}
        self.staged = staged

    def activate(self, path):
        self.compose(path, 'up', '-d', '--no-deps', '--no-build', 'api')

    def verify(self, image):
        for _ in range(self.p.get('health_attempts', 60)):
            api = self.inspect(self.p['api_container'])
            if api['Image'] == image and api['State'].get('Health', {}).get('Status') == 'healthy':
                break
            time.sleep(self.p.get('health_interval', 2))
        else:
            raise ReleaseError('API health/image verification failed')
        for key in ('Env',):
            require(canonical(api['Config'].get(key)) == canonical(self.api['Config'].get(key)), 'API environment changed')
        require(canonical(api['Mounts']) == canonical(self.api['Mounts']), 'API mounts changed')
        current = {k: v for k, v in self.containers().items() if v['Id'] != api['Id']}
        require(set(current) == set(self.others), 'Unrelated container membership changed')
        for key, old in self.others.items():
            new = current[key]
            require((new['Image'], new['State']['StartedAt'], new['State']['Status']) ==
                    (old['Image'], old['State']['StartedAt'], old['State']['Status']), 'Unrelated service changed')

    def execute(self, apply=False):
        with lock(self.p['lock_file']):
            self.preflight()
            if not apply:
                return {'status': 'validated', 'production_mutated': False}
            root = Path(self.p['receipt_dir'])
            root.mkdir(mode=0o700)
            # Snapshot effective configs, independent of subsequent input-file edits.
            for name, conf in [('rollback', self.rollback), ('production', self.candidate)]:
                atomic_bytes(json.dumps(conf).encode(), root / ('compose.' + name + '.json'), 0o600)
            require(self.config(root / 'compose.production.json') == self.candidate, 'Relocated Compose differs')
            require(self.config(root / 'compose.rollback.json') == self.rollback, 'Relocated rollback Compose differs')
            backup = {}
            for name in self.staged:
                target = child(self.p['webroot'], name)
                backup[name] = target.read_bytes() if target.exists() else None
                if backup[name] is not None:
                    atomic_bytes(backup[name], child(root / 'before', name), 0o600)
            atomic_bytes(json.dumps(self.p, indent=2).encode(), root / 'plan.json', 0o600)
            changed = []
            try:
                self.activate(root / 'compose.production.json')
                self.verify(self.p['new_image'])
                for name in sorted(self.staged, key=lambda n: (self.p['static'][n]['kind'] != 'asset', n)):
                    changed.append(name)
                    atomic_bytes(self.staged[name], child(self.p['webroot'], name))
                for name, item in self.p['static'].items():
                    require(sha(child(self.p['webroot'], name)) == item['sha256'], 'Published static hash mismatch')
                self.verify(self.p['new_image'])
            except BaseException:
                failures = []
                for name in reversed(changed):
                    try:
                        target = child(self.p['webroot'], name)
                        if backup[name] is not None:
                            atomic_bytes(backup[name], target)
                        elif self.p['static'][name]['kind'] == 'page' and target.exists():
                            target.unlink()  # Only a page created by this transaction.
                        # Newly published assets are intentionally retained.
                        if backup[name] is not None:
                            require(target.read_bytes() == backup[name], 'Static rollback verification failed')
                        elif self.p['static'][name]['kind'] == 'page':
                            require(not target.exists(), 'New page rollback verification failed')
                    except Exception:
                        failures.append('static restore failed: ' + name)
                try:
                    self.activate(root / 'compose.rollback.json')
                    self.verify(self.p['old_image'])
                except Exception:
                    failures.append('API rollback verification failed')
                result = {'status': 'rollback_failed' if failures else 'rolled_back', 'failures': failures}
                atomic_bytes(json.dumps(result).encode(), root / 'receipt.json', 0o600)
                raise ReleaseError('Release failed; ' + result['status'] + '; inspect private receipt') from None
            result = {'status': 'deployed', 'source_commit': self.p['source_commit'], 'image': self.p['new_image']}
            atomic_bytes(json.dumps(result).encode(), root / 'receipt.json', 0o600)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Activate production (default: preflight only)')
    args = parser.parse_args()
    try:
        require(args.plan.stat().st_mode & 0o077 == 0, 'Plan must be private (0600)')
        print(json.dumps(Release(json.loads(args.plan.read_text())).execute(args.apply)))
    except (ReleaseError, OSError, ValueError, KeyError):
        parser.exit(1, 'Release rejected or failed; inspect private inputs/receipt. No secret output emitted.\n')


if __name__ == '__main__':
    main()
