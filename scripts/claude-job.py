#!/usr/bin/env python3
"""Dispatch one turn into a persistent, multi-repository session workspace."""
import argparse
import getpass
import hashlib
import json
import re
import sys
import uuid

from session_runtime import Error, acquire, bind_job, get, isolate, kubectl, template, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt')
    parser.add_argument('--session', type=uuid.UUID)
    parser.add_argument('--turn-id', help='deduplicates only while the Job exists')
    parser.add_argument('--started-by', default=getpass.getuser())
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    session = str(args.session or uuid.uuid4())
    name = 'claude-' + hashlib.sha256((args.turn_id or str(uuid.uuid4())).encode()).hexdigest()[:16]
    existing = get('job', name)
    if existing:
        actual = existing['metadata']['labels']['claude-session']
        if args.session and session != actual:
            raise Error('Turn ID already belongs to a different session')
        session = actual
    else:
        job, _ = template()
        job['metadata']['name'] = name
        started_by = re.sub(r'[^A-Za-z0-9._-]', '_', args.started_by)[:63].strip('._-') or 'unknown'
        for metadata in (job['metadata'], job['spec']['template']['metadata']):
            metadata['labels'].update({'claude-session': session, 'claude-started-by': started_by})
        spec = job['spec']['template']['spec']
        isolate(spec, session)
        for env in spec['containers'][0]['env']:
            if env['name'] in ('PROMPT', 'SESSION_ID'):
                env['value'] = args.prompt if env['name'] == 'PROMPT' else session
        owned = acquire(session, 'Job', name)
        try:
            created = write('create', job)
            bind_job(owned, created)
        except Error as exc:
            # A transport error can mean the server created the Job. Never release
            # a reservation when creation has an unknown outcome.
            raise Error(f'{exc}\nReservation retained for session {session}, Job {name}; see README recovery.') from exc
    print(json.dumps({'job': name, 'session_id': session, 'reused': bool(existing)}), flush=True)
    if args.wait:
        logs = kubectl('logs', '-f', f'job/{name}', '-c', 'claude', '--pod-running-timeout=10m').strip()
        try:
            result = json.loads(logs.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise Error('Job did not produce a JSON result; inspect its status and logs') from exc
        print(json.dumps(result, indent=2))
        if result.get('is_error'):
            raise Error('Claude reported a failed turn')


if __name__ == '__main__':
    try:
        main()
    except Error as exc:
        sys.exit(str(exc))
