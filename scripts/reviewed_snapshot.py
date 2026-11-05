"""Domain-neutral independently reviewed, append-only snapshot publication.

A proposal seals every output and evidence file. A distinct worker approves those
bytes. Immutable transactions and one atomic head provide recovery/read isolation.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def inside(root, path):
    root = Path(root).resolve()
    result = (root / path).resolve()
    result.relative_to(root)
    return result


def reference(root, path):
    path = inside(root, path)
    return {'path': str(path.relative_to(Path(root).resolve())), 'sha256': sha(path.read_bytes())}


def checked(root, ref):
    path = inside(root, ref['path'])
    raw = path.read_bytes()
    if sha(raw) != ref['sha256']:
        raise ValueError('Stale or changed evidence/output: ' + ref['path'])
    return raw


def atomic(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(store):
    store.mkdir(parents=True, exist_ok=True)
    with (store / '.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def immutable(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Immutable transaction collision: ' + str(path))
    else:
        atomic(path, raw)


def read_head(root, store):
    path = inside(root, store) / 'head.json'
    if not path.exists():
        return None
    head = json.loads(path.read_bytes())
    receipt = json.loads(checked(root, head['receipt']))
    if receipt['transaction'] != head['transaction']:
        raise ValueError('Head/receipt mismatch')
    proposal_raw = checked(root, receipt['proposal'])
    approval_raw = checked(root, receipt['approval'])
    proposal, approval = json.loads(proposal_raw), json.loads(approval_raw)
    if (sha(proposal_raw + b'\0' + approval_raw) != receipt['transaction']
            or approval.get('proposal_sha256') != sha(proposal_raw)
            or approval.get('approved') is not True
            or approval.get('reviewer') != receipt['reviewer']
            or proposal.get('proposer') != receipt['proposer']
            or receipt['reviewer'] == receipt['proposer']
            or proposal.get('parent') != receipt.get('parent')):
        raise ValueError('Receipt/review provenance mismatch')
    if set(receipt['outputs']) != set(proposal['outputs']):
        raise ValueError('Receipt/proposal outputs differ')
    for name, ref in receipt['outputs'].items():
        if ref['sha256'] != proposal['outputs'][name]['sha256']:
            raise ValueError('Receipt/proposal output hashes differ')
    expected_evidence = {(r['path'], r['sha256']) for r in [*proposal['evidence'], *approval.get('evidence', [])]}
    archived_evidence = {(r['original']['path'], r['original']['sha256']) for r in receipt['evidence']}
    if expected_evidence != archived_evidence:
        raise ValueError('Receipt/proposal evidence differs')
    for row in receipt['evidence']:
        if row['original']['sha256'] != row['archive']['sha256']:
            raise ValueError('Evidence archive hash differs')
        checked(root, row['archive'])
    for ref in receipt['outputs'].values():
        checked(root, ref)
    return receipt


def publish(root, store, proposal_path, approval_path, worker, fail_after=None):
    """Resume the identical proposal+approval after interruption, never reset head.

    fail_after is for fault-injection tests (prepared, outputs, head).
    """
    root = Path(root).resolve()
    store_path = inside(root, store)
    proposal_raw = inside(root, proposal_path).read_bytes()
    approval_raw = inside(root, approval_path).read_bytes()
    proposal = json.loads(proposal_raw)
    approval = json.loads(approval_raw)
    proposal_hash = sha(proposal_raw)
    if not worker or worker != approval.get('reviewer'):
        raise ValueError('Publishing worker must be the recorded independent reviewer')
    if not proposal.get('proposer') or worker == proposal['proposer']:
        raise ValueError('Independent reviewer required')
    if approval.get('approved') is not True or approval.get('proposal_sha256') != proposal_hash:
        raise ValueError('Approval does not seal this proposal')
    if not approval.get('rationale') or not approval.get('checks'):
        raise ValueError('Independent review rationale and checks required')
    if not proposal.get('evidence') or not proposal.get('outputs'):
        raise ValueError('Evidence and complete snapshot outputs required')
    # Evidence includes source captures, code/config and immutable previous inputs.
    for ref in [*proposal['evidence'], *approval.get('evidence', [])]:
        checked(root, ref)
    bodies = {name: checked(root, ref) for name, ref in proposal['outputs'].items()}
    if any('/' in name or name in ('.', '..', 'proposal.json', 'approval.json', 'receipt.json') or not name for name in bodies):
        raise ValueError('Output names must be plain filenames')
    transaction = sha(proposal_raw + b'\0' + approval_raw)
    txdir = store_path / 'transactions' / transaction
    with locked(store_path):
        # Revalidate under lock, including every sealed output, to prevent stale writers.
        for ref in [*proposal['evidence'], *approval.get('evidence', []), *proposal['outputs'].values()]:
            checked(root, ref)
        head = read_head(root, store)
        if head and head['transaction'] == transaction:
            return head  # exact retry after activation
        if (head or {}).get('transaction') != proposal.get('parent'):
            raise ValueError('Stale head; propose/review against current snapshot')
        immutable(txdir / 'proposal.json', proposal_raw)
        immutable(txdir / 'approval.json', approval_raw)
        if fail_after == 'prepared':
            raise RuntimeError('Simulated interruption after prepare')
        for name, body in bodies.items():
            immutable(txdir / name, body)
        archives = []
        refs = {(r['path'], r['sha256']): r for r in [*proposal['evidence'], *approval.get('evidence', [])]}
        for _, ref in sorted(refs.items()):
            archived = store_path / 'evidence' / ref['sha256']
            immutable(archived, checked(root, ref))
            archives.append({'original': ref, 'archive': reference(root, archived)})
        if fail_after == 'outputs':
            raise RuntimeError('Simulated interruption after outputs')
        receipt = {'transaction': transaction, 'parent': proposal.get('parent'),
                   'proposer': proposal['proposer'], 'reviewer': worker,
                   'proposal': reference(root, txdir / 'proposal.json'),
                   'approval': reference(root, txdir / 'approval.json'),
                   'evidence': archives,
                   'outputs': {name: reference(root, txdir / name) for name in sorted(bodies)}}
        immutable(txdir / 'receipt.json', encoded(receipt))
        atomic(store_path / 'head.json', encoded({'transaction': transaction,
                                               'receipt': reference(root, txdir / 'receipt.json')}))
        if fail_after == 'head':
            raise RuntimeError('Simulated interruption after head')
        return read_head(root, store)
