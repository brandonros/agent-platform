#!/usr/bin/env python3
"""Open or close a session IDE. Closing the port-forward does not release ownership."""
import argparse
import subprocess
import sys
import uuid

from session_runtime import Error, acquire, get, ide_pod, kubectl, lease_name, release, template, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('open', 'close'))
    parser.add_argument('session', type=uuid.UUID)
    args = parser.parse_args()
    session = str(args.session)
    name = f'claude-ide-{session}'
    pod = get('pod', name)
    owned = get('lease', lease_name(session))
    if args.action == 'close':
        if not owned or owned['metadata'].get('annotations', {}).get('session-resource-name') != name:
            raise Error('Session is not reserved by this IDE')
        if owned['metadata']['annotations'].get('session-closing') == 'true':
            raise Error('IDE close already in progress; see README recovery if it failed')
        owned['metadata']['annotations']['session-closing'] = 'true'
        owned = write('replace', owned)  # CAS: only one closer may delete the pod.
        kubectl('delete', 'pod', name, '--ignore-not-found', '--wait=true', '--timeout=120s')
        release(owned)
        print('IDE closed; workspace and history retained. Automated turns may resume.')
        return
    if pod:
        if owned and owned['metadata'].get('annotations', {}).get('session-closing') == 'true':
            raise Error('IDE is closing')
        if not owned or owned['metadata'].get('annotations', {}).get('session-resource-name') != name:
            raise Error('IDE exists without its reservation; refusing to open it')
    else:
        job, image = template()
        proxy = get('deployment', 'oauth2-proxy')
        if get('secret', 'oauth2-proxy') and not proxy:
            raise Error('Entra is configured but its deployment is missing; run just apply')
        pod = ide_pod(job, image, session, proxy)
        acquire(session, 'Pod', name)
        try:
            pod = write('create', pod)
        except Error as exc:
            raise Error(f'{exc}\nReservation retained; see README recovery.') from exc
    kubectl('wait', '--for=condition=Ready', f'pod/{name}', '--timeout=180s')
    port = pod['metadata']['annotations']['session-port']
    print(f'Open http://localhost:8080. Select the conversation in Claude Session Manager.\n'
          f'When finished, run: just session-close {session}', flush=True)
    result = subprocess.run(['kubectl', '-n', 'agent-platform', 'port-forward', '--address=127.0.0.1',
                             f'pod/{name}', f'8080:{port}'])
    if result.returncode:
        raise Error('Port-forward stopped; IDE still owns the session. Reopen or explicitly close it.')


if __name__ == '__main__':
    try:
        main()
    except Error as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        print('\nPort-forward stopped. Run session-close to release the IDE reservation.')
