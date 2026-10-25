"""Shared Kubernetes/session operations. Locks fail closed after uncertain creates."""
import copy
import json
import pathlib
import re
import subprocess
import uuid

USERS = pathlib.Path(__file__).resolve().parent.parent / 'users.yaml'


class Error(RuntimeError):
    pass


def kubectl(*args, input=None):
    result = subprocess.run(
        ['kubectl', '-n', 'agent-platform', *args], input=input,
        capture_output=True, text=True)
    if result.returncode:
        raise Error(f"kubectl {' '.join(args[:3])}: {result.stderr.strip()}")
    return result.stdout


def get(kind, name):
    raw = kubectl('get', kind, name, '--ignore-not-found', '-o', 'json')
    return json.loads(raw) if raw.strip() else None


def write(verb, obj):
    return json.loads(kubectl(verb, '-f', '-', '-o', 'json', input=json.dumps(obj)))


def done(obj):
    if obj['kind'] == 'Pod':
        return obj.get('status', {}).get('phase') in ('Succeeded', 'Failed')
    return any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
               for c in obj.get('status', {}).get('conditions', []))


def lease_name(session):
    return f'claude-session-{session}'


def acquire(session, kind, name):
    """CAS acquisition; never expire a reservation whose creator may still run.

    A completed holder can be replaced. An absent holder cannot: a delayed or
    disconnected dispatcher could still create it. This intentionally requires
    operator recovery after a crash instead of pretending a timeout is fencing.
    """
    current = get('lease', lease_name(session))
    if current and current.get('spec', {}).get('holderIdentity'):
        annotations = current['metadata'].get('annotations', {})
        resource = get(annotations.get('session-resource-kind', 'Job'),
                       annotations.get('session-resource-name', current['spec']['holderIdentity']))
        if resource is None or not done(resource):
            raise Error(f'Session {session} is reserved; close its IDE or wait for its Job. '
                        'An absent holder requires operator recovery; see README.')
    # Also catch old dispatchers that created Jobs without this Lease protocol.
    jobs = json.loads(kubectl('get', 'jobs', '-l', f'claude-session={session}', '-o', 'json'))
    if any(not done(job) for job in jobs['items']):
        raise Error(f'Session {session} has an active Job')
    lease = {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
             'metadata': {'name': lease_name(session), 'labels': {'claude-session': session},
                          'annotations': {'session-resource-kind': kind, 'session-resource-name': name}},
             'spec': {'holderIdentity': str(uuid.uuid4())}}
    if current:
        lease['metadata']['resourceVersion'] = current['metadata']['resourceVersion']
    return write('replace' if current else 'create', lease)


def release(owned):
    """Clear only our reservation, atomically; never delete a successor's Lease."""
    current = get('lease', owned['metadata']['name'])
    if current is None or current['spec'].get('holderIdentity') != owned['spec']['holderIdentity']:
        raise Error('Session ownership changed; refusing to release another owner')
    current['spec']['holderIdentity'] = ''
    write('replace', current)  # resourceVersion protects the read/modify/write.


def template():
    config = get('configmap', 'claude-job-template')
    if not config:
        raise Error('claude-job-template not found; run just apply')
    return json.loads(config['data']['job.json']), config['data']['ide-image']


def users():
    """users.yaml: {id: {name, email, github}}; flat enough to parse without PyYAML."""
    found, current = {}, None
    for line in USERS.read_text().splitlines():
        if m := re.match(r'^([A-Za-z0-9-]+):\s*$', line):
            current = found[m[1]] = {}
        elif (m := re.match(r'^\s+(name|email|github):\s*(.+?)\s*$', line)) and current is not None:
            current[m[1]] = m[2].strip('"\'')
    return found


def default_user():
    ids = list(users())
    return ids[0] if len(ids) == 1 else None


def for_user(spec, user):
    """Run on the user's PVC and node, committing as the user."""
    profile = users().get(user)
    if not profile:
        raise Error(f'Unknown user {user!r}; add them to users.yaml')
    term = spec['affinity']['podAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]
    term['labelSelector']['matchLabels']['code-server-user'] = user
    for volume in spec['volumes']:
        if volume['name'] == 'workspace':
            volume['persistentVolumeClaim']['claimName'] = f'code-server-{user}'
    spec['containers'][0]['env'] += [
        {'name': f'GIT_{role}_{field.upper()}', 'value': profile[field]}
        for role in ('AUTHOR', 'COMMITTER') for field in ('name', 'email')]


def isolate(spec, session):
    """Mount only this session into the main container; setup sees the PVC root."""
    container = spec['containers'][0]
    for mount in container['volumeMounts']:
        if mount['name'] == 'workspace':
            leaf = 'claude' if mount['mountPath'].endswith('/.claude') else 'workspace'
            mount['subPath'] = f'sessions/{session}/{leaf}'
    spec['initContainers'] = [{
        'name': 'session-directories', 'image': container['image'],
        'imagePullPolicy': container['imagePullPolicy'],
        'command': ['sh', '-ec',
                    'mkdir -p "/storage/sessions/$SESSION_ID/workspace" "/storage/sessions/$SESSION_ID/claude"'],
        'env': [{'name': 'SESSION_ID', 'value': session}],
        'volumeMounts': [{'name': 'workspace', 'mountPath': '/storage'}]
    }]


def bind_job(owned, job):
    """GC the reservation with its completed Job; CAS cannot alter a successor."""
    owned['metadata']['ownerReferences'] = [{
        'apiVersion': 'batch/v1', 'kind': 'Job', 'name': job['metadata']['name'],
        'uid': job['metadata']['uid']}]
    write('replace', owned)


def ide_pod(job, image, session, proxy=None):
    spec = copy.deepcopy(job['spec']['template']['spec'])
    isolate(spec, session)
    container = spec['containers'][0]
    container.update(name='code-server', image=image,
                     command=['dumb-init', 'code-server'],
                     args=['--bind-addr', '0.0.0.0:8080', '--auth', 'none', '/home/coder/workspace'],
                     resources={'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'memory': '2Gi'}})
    container['env'] = [e for e in container['env']
                        if e['name'] == 'CLAUDE_CODE_OAUTH_TOKEN' or e['name'].startswith('GIT_')]
    container['ports'] = [{'containerPort': 8080}]
    if proxy:
        sidecar = copy.deepcopy(proxy['spec']['template']['spec']['containers'][0])
        sidecar['name'] = 'oauth2-proxy'
        for env in sidecar['env']:
            if env['name'] == 'OAUTH2_PROXY_UPSTREAMS':
                env['value'] = 'http://127.0.0.1:8080'
        spec['containers'].append(sidecar)
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': f'claude-ide-{session}',
                         'labels': {'app': 'claude-session-ide', 'claude-session': session},
                         'annotations': {'session-port': '4180' if proxy else '8080'}}, 'spec': spec}
