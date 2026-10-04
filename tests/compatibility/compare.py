#!/usr/bin/env python3
"""Compare two immutable Docker image snapshots and their basic lifecycle."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import uuid


def docker(*args, timeout=120):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        # Do not include container logs or commands containing fixture credentials.
        raise RuntimeError(f'Docker {args[0]} failed (exit {result.returncode})')
    return result.stdout


def inspect(name):
    return json.loads(docker('inspect', name))[0]


def differences(reference, candidate):
    return {key: {'reference': reference.get(key), 'candidate': candidate.get(key)}
            for key in sorted(reference.keys() | candidate.keys())
            if reference.get(key) != candidate.get(key)}


def filesystem(archive):
    # Compare the tree, not binary hashes: compiled binaries differ across builds.
    tree = {}
    with tarfile.open(archive, mode='r|*') as stream:
        for entry in stream:
            path = '/' + entry.name.removeprefix('./').lstrip('/').rstrip('/')
            tree[path] = {'type': entry.type.decode('ascii'),
                          'mode': oct(entry.mode), 'uid': entry.uid, 'gid': entry.gid,
                          'link': entry.linkname if entry.issym() or entry.islnk() else None}
    return tree


def snapshot(image, args):
    metadata = inspect(image)
    if metadata['Os'] + '/' + metadata['Architecture'] != args.platform:
        raise ValueError('Image architecture differs from the requested native platform')
    config = metadata['Config']
    result = {'id': metadata['Id'], 'digests': metadata.get('RepoDigests', []),
              'platform': args.platform, 'size_bytes': metadata['Size'],
              'config': {key: config.get(key) for key in
                         ['User', 'Entrypoint', 'Cmd', 'WorkingDir', 'Volumes',
                          'ExposedPorts', 'StopSignal']},
              'environment': dict(item.split('=', 1) for item in config.get('Env') or [])}
    name = 'bitcompat-compare-' + uuid.uuid4().hex[:12]
    created = False
    try:
        options = ['create', '--name', name, '--platform', args.platform,
                   '--label', 'bitcompat.compatibility=general']
        if args.env_file:
            options += ['--env-file', args.env_file]
        # Start with the image's actual entrypoint and CMD; no host ports or mounts.
        docker(*options, metadata['Id'])
        created = True
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'filesystem.tar'
            docker('export', '--output', archive, name, timeout=300)
            result['filesystem'] = filesystem(archive)
        # Export precedes startup, excluding initialization and runtime changes.
        docker('start', name)
        deadline = time.monotonic() + args.start_timeout
        ready = False
        while time.monotonic() < deadline:
            state = inspect(name)['State']
            if not state['Running']:
                break
            if args.ready_command:
                probe = subprocess.run(['docker', 'exec', name, '/bin/sh', '-c', args.ready_command],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10)
                if probe.returncode == 0:
                    ready = True
                    break
            time.sleep(1)
        state = inspect(name)['State']
        result['startup'] = {'running': state['Running'],
                             'ready': ready if args.ready_command else None,
                             'probe': 'explicit' if args.ready_command else 'process-alive-only'}
        if state['Running'] and args.configure_command:
            result['configure'] = docker('exec', name, '/bin/sh', '-c', args.configure_command)
        else:
            result['configure'] = None
        if state['Running']:
            # Explicit SIGTERM rather than an image-specific StopSignal override.
            docker('stop', '--signal', 'SIGTERM', '--time', args.stop_timeout, name,
                   timeout=args.stop_timeout + 30)
        state = inspect(name)['State']
        result['stop'] = {'exit_code': state['ExitCode'], 'oom_killed': state['OOMKilled']}
        result['lifecycle_passed'] = (result['startup']['running']
                                     and (ready or not args.ready_command)
                                     and state['ExitCode'] in (0, 143)
                                     and not state['OOMKilled'])
        return result
    finally:
        if created:
            docker('rm', '--force', '--volumes', name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True, help='repository@sha256:digest')
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--platform', required=True, choices=['linux/amd64', 'linux/arm64'])
    parser.add_argument('--env-file', type=Path, help='Same startup fixture for both containers; never copied into reports')
    parser.add_argument('--ready-command', help='Optional identical shell readiness probe inside each container')
    parser.add_argument('--configure-command', help='Optional identical shell command exposing configure flags')
    parser.add_argument('--start-timeout', type=int, default=10)
    parser.add_argument('--stop-timeout', type=int, default=20)
    parser.add_argument('--output', type=Path, default=Path('image-comparison'))
    args = parser.parse_args()
    if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', args.reference):
        parser.error('Reference must be pinned to a registry digest')
    if args.start_timeout <= 0 or args.stop_timeout <= 0:
        parser.error('Timeouts must be positive')
    host = json.loads(docker('info', '--format', '{{json .}}'))['Architecture']
    host = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(host, host)
    if args.platform != 'linux/' + host:
        parser.error('Run on the requested native architecture')
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'failed', 'reference': args.reference, 'candidate': args.candidate,
              'platform': args.platform, 'filesystem_scope': 'pre-start docker export; volume contents excluded',
              'size_measure': 'Docker inspect Size: local uncompressed image, not registry transfer size'}
    try:
        reference = snapshot(args.reference, args)
        (args.output / 'reference.json').write_text(json.dumps(reference, indent=2) + '\n')
        candidate = snapshot(args.candidate, args)
        (args.output / 'candidate.json').write_text(json.dumps(candidate, indent=2) + '\n')
        report['mode'] = 'self-check' if reference['id'] == candidate['id'] else 'comparison'
        for field in ['filesystem', 'environment', 'config']:
            report[field + '_differences'] = differences(reference[field], candidate[field])
        report['configure'] = {'requested': args.configure_command is not None,
                               'available': bool(reference['configure'] and candidate['configure']),
                               'equal': reference['configure'] == candidate['configure'],
                               'reference': reference['configure'], 'candidate': candidate['configure']}
        delta = candidate['size_bytes'] - reference['size_bytes']
        report['size'] = {'reference_bytes': reference['size_bytes'],
                          'candidate_bytes': candidate['size_bytes'], 'delta_bytes': delta,
                          'delta_percent': 100 * delta / reference['size_bytes'] if reference['size_bytes'] else None}
        report['lifecycle'] = {key: {'startup': value['startup'], 'stop': value['stop'],
                                   'passed': value['lifecycle_passed']}
                               for key, value in [('reference', reference), ('candidate', candidate)]}
        report['status'] = 'review-required' if all(value['lifecycle_passed'] for value in [reference, candidate]) else 'failed'
    except Exception as error:
        report['error'] = str(error)
    finally:
        (args.output / 'comparison.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"{report['status']}: {args.output / 'comparison.json'}")
    return 1 if report['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
