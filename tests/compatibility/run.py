#!/usr/bin/env python3
"""Exercise the Bitnami container contract using Docker and native architecture."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', choices=['postgresql', 'mysql', 'redis'], required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'], required=True)
    parser.add_argument('--reference', help='Override the locked reference; requires repository@sha256:digest')
    parser.add_argument('--require-reference', action='store_true', help='Fail instead of contract-only mode if no exact reference is locked')
    parser.add_argument('--report', type=Path, default=Path('compatibility-report.json'))
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    service = args.service
    prefix = 'bitcompat-test-' + uuid.uuid4().hex[:12]
    password = secrets.token_hex(20)
    root_password = secrets.token_hex(20)
    containers, volumes = [], []
    report = dict(service=service, version=args.version, platform=args.platform,
                  candidate=args.candidate, observations=[], checks=[], status='failed')

    def redact(text):
        return text.replace(password, '[test password]').replace(root_password, '[test password]')

    def docker(*command, check=True, timeout=60):
        result = subprocess.run(['docker', *map(str, command)], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'Docker {command[0]} failed: {redact(result.stderr.strip())}')
        return result

    def checked(condition, message):
        if not condition:
            raise AssertionError(message)
        report['checks'].append(message)

    def inspect(name):
        return json.loads(docker('inspect', name).stdout)[0]

    def client(name, query, wrong=False):
        credential = 'wrong-password' if wrong else password
        if service == 'postgresql':
            command = ['-e', f'PGPASSWORD={credential}', name, '/opt/bitnami/postgresql/bin/psql',
                       '-h', '127.0.0.1', '-p', '55432', '-U', 'compat_user', '-d', 'compat',
                       '-t', '-A', '-v', 'ON_ERROR_STOP=1', '-c', query]
        elif service == 'mysql':
            command = ['-e', f'MYSQL_PWD={credential}', name, '/opt/bitnami/mysql/bin/mysql',
                       '--protocol=TCP', '--host=127.0.0.1', '--port=3307', '--user=compat_user',
                       '--database=compat', '--batch', '--skip-column-names', '--execute', query]
        else:
            command = ['-e', f'REDISCLI_AUTH={credential}', name, '/opt/bitnami/redis/bin/redis-cli',
                       '--raw', '-h', '127.0.0.1', '-p', '6380', *query]
        return docker('exec', *command, check=False, timeout=15)

    probe = ['PING'] if service == 'redis' else 'SELECT 1;'

    def ready(name):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            state = inspect(name)['State']
            if not state['Running']:
                raise RuntimeError('Service exited before becoming ready')
            command = docker('exec', name, 'cat', '/proc/1/comm', check=False).stdout.strip()
            if command != {'postgresql': 'postgres', 'mysql': 'mysqld', 'redis': 'redis-server'}[service]:
                time.sleep(1)
                continue
            result = client(name, probe)
            if result.returncode == 0 and result.stdout.strip() == ('PONG' if service == 'redis' else '1'):
                return
            time.sleep(1)
        raise TimeoutError('Service did not accept authenticated TCP connections')

    def query(name, command):
        result = client(name, command)
        if result.returncode != 0:
            raise RuntimeError('Database client failed: ' + redact(result.stderr.strip()))
        return result.stdout.strip()

    def stop(name):
        docker('stop', '--time', '20', name, timeout=30)
        state = inspect(name)['State']
        checked(state['ExitCode'] in [0, 143] and not state['OOMKilled'], 'SIGTERM stopped the server without SIGKILL/OOM')

    def new_volume():
        name = f'{prefix}-volume-{len(volumes)}'
        docker('volume', 'create', '--label', f'bitcompat.compatibility={prefix}', name)
        volumes.append(name)
        return name

    try:
        host_arch = docker('info', '--format', '{{.Architecture}}').stdout.strip()
        host_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(host_arch, host_arch)
        checked(host_arch == args.platform.split('/')[1], 'tests run on the requested native architecture')
        references = json.loads((Path(__file__).with_name('references.json')).read_text())
        locked = references.get(service, {}).get(args.version)
        reference = args.reference or (locked['image'] if locked else None)
        if reference:
            checked(re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', reference) is not None, 'reference is pinned by digest')
        if args.require_reference and not reference:
            raise ValueError('No exact-version Bitnami reference is locked')
        report['reference'] = reference
        report['mode'] = 'differential' if reference else 'contract-only'
        if not reference:
            print(f'::warning::No exact-version Bitnami reference locked for {service} {args.version}; contract tests only')

        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            secret_dir, init_dir = root / 'secrets', root / 'init'
            for directory in (secret_dir, init_dir):
                directory.mkdir(mode=0o755)
            for name, value in [('password', password), ('root_password', root_password)]:
                (secret_dir / name).write_text(value)
                (secret_dir / name).chmod(0o444)
            (init_dir / '01-contract.sql').write_text(
                "CREATE TABLE bitcompat_probe (id INTEGER PRIMARY KEY, value VARCHAR(128));\n"
                "INSERT INTO bitcompat_probe VALUES (1, 'initialized');\n")
            (init_dir / '01-contract.sql').chmod(0o444)
            env = {
                'postgresql': ['POSTGRESQL_USERNAME=compat_user', 'POSTGRESQL_DATABASE=compat',
                               'POSTGRESQL_PASSWORD_FILE=/run/compat-secrets/password', 'POSTGRESQL_PORT_NUMBER=55432'],
                'mysql': ['MYSQL_USER=compat_user', 'MYSQL_DATABASE=compat',
                          'MYSQL_PASSWORD_FILE=/run/compat-secrets/password',
                          'MYSQL_ROOT_PASSWORD_FILE=/run/compat-secrets/root_password', 'MYSQL_PORT_NUMBER=3307'],
                'redis': ['REDIS_PASSWORD_FILE=/run/compat-secrets/password', 'REDIS_PORT_NUMBER=6380'],
            }[service]

            def start(image, volume, empty=False, files=True):
                name = f'{prefix}-container-{len(containers)}'
                containers.append(name)
                command = ['run', '--detach', '--name', name, '--platform', args.platform,
                           '--label', f'bitcompat.compatibility={prefix}']
                if not empty:
                    command += ['--mount', f'type=volume,src={volume},dst=/bitnami/{service}',
                                '--mount', f'type=bind,src={secret_dir},dst=/run/compat-secrets,readonly']
                    if service != 'redis':
                        command += ['--mount', f'type=bind,src={init_dir},dst=/docker-entrypoint-initdb.d,readonly']
                    for variable in env:
                        if not files and "_FILE=" in variable:
                            key, source = variable.split("_FILE=", 1)
                            variable = key + "=" + (root_password if source.endswith("root_password") else password)
                        command += ['-e', variable]
                docker(*command, image)
                return name

            def exercise(image, volume=None, populated=False, files=True):
                metadata = docker('image', 'inspect', image, check=False)
                if metadata.returncode:
                    docker('pull', '--platform', args.platform, image, timeout=900)
                    metadata = docker('image', 'inspect', image)
                metadata = json.loads(metadata.stdout)[0]
                config = metadata['Config']
                checked(metadata['Architecture'] == host_arch, 'image architecture matches the runner')
                checked(config['User'].split(':')[0] == '1001', 'image defaults to UID 1001')
                checked(config['Entrypoint'] == [f'/opt/bitnami/scripts/{service}/entrypoint.sh'], 'standard Bitnami entrypoint')
                checked(config['Cmd'] == [f'/opt/bitnami/scripts/{service}/run.sh'], 'standard Bitnami command')
                default_port = {'postgresql': '5432', 'mysql': '3306', 'redis': '6379'}[service]
                checked(default_port + '/tcp' in config.get('ExposedPorts', {}), 'standard port is exposed')
                image_env = dict(value.split('=', 1) for value in config.get('Env', []) if '=' in value)
                report['observations'].append(dict(image=image, image_id=metadata['Id'],
                                                    distribution=image_env.get('OS_FLAVOUR'),
                                                    repo_digests=metadata.get('RepoDigests', []),
                                                    architecture=metadata['Architecture']))
                resolved = metadata['Id']
                bad = start(resolved, None, empty=True)
                deadline = time.monotonic() + 30
                while inspect(bad)['State']['Running'] and time.monotonic() < deadline:
                    time.sleep(0.5)
                state = inspect(bad)['State']
                checked(not state['Running'] and state['ExitCode'] != 0, 'startup rejects missing credentials')
                volume = volume or new_volume()
                name = start(resolved, volume, files=files)
                ready(name)
                checked(docker('exec', name, 'id', '-u').stdout.strip() == '1001', 'server container runs as UID 1001')
                ownership = docker('exec', name, 'stat', '-L', '-c', '%u:%g:%a', f'/bitnami/{service}/data').stdout.strip()
                report['observations'][-1]['data_permissions'] = ownership
                docker('exec', name, 'bash', '-c', f'touch /bitnami/{service}/data/.compat-write && rm /bitnami/{service}/data/.compat-write')
                report['checks'].append('persistent directory is writable by UID 1001')
                version = query(name, ['INFO', 'server'] if service == 'redis' else
                                ('SHOW server_version;' if service == 'postgresql' else 'SELECT VERSION();'))
                if service == 'redis':
                    version = next(line.split(':', 1)[1] for line in version.splitlines() if line.startswith('redis_version:'))
                checked(re.match(re.escape(args.version) + r'(?:[^\d.]|$)', version) is not None, 'running software matches the requested version')
                report['observations'][-1]['server_version'] = version
                wrong = client(name, probe, wrong=True)
                checked(wrong.returncode != 0 if service != 'redis' else
                        ('WRONGPASS' in wrong.stdout or 'NOAUTH' in wrong.stdout), 'TCP authentication rejects incorrect password')
                if service == 'redis':
                    if not populated:
                        checked(query(name, ['SET', 'bitcompat:probe', 'persisted']) == 'OK', 'write persisted fixture')
                    read = ['GET', 'bitcompat:probe']
                    expected = 'persisted'
                else:
                    checked(query(name, 'SELECT COUNT(*) FROM bitcompat_probe WHERE id=1;') == '1', 'init SQL ran exactly once')
                    if not populated:
                        query(name, "INSERT INTO bitcompat_probe VALUES (2, 'persisted');")
                    read = 'SELECT value FROM bitcompat_probe WHERE id=2;'
                    expected = 'persisted'
                checked(query(name, read) == expected, 'persistent fixture is readable')
                stop(name)
                docker('start', name)
                ready(name)
                checked(query(name, read) == expected, 'same-container restart retains data')
                stop(name)
                replacement = start(resolved, volume, files=files)
                ready(replacement)
                checked(query(replacement, read) == expected, 'replacement container accepts populated volume')
                if service != 'redis':
                    checked(query(replacement, 'SELECT COUNT(*) FROM bitcompat_probe;') == '2', 'replacement does not rerun init SQL')
                stop(replacement)
                return volume

            if reference:
                reference_volume = exercise(reference)
                exercise(args.candidate, reference_volume, populated=True)
                report['checks'].append('Bitnami populated volume accepted by candidate')
            exercise(args.candidate, files=False)
        if reference and len({item['image_id'] for item in report['observations']}) == 1:
            report['mode'] = 'reference-self-check'
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = redact(str(error))
        report['logs'] = {}
        for name in containers:
            result = docker('logs', '--tail', '80', name, check=False)
            report['logs'][name] = redact(result.stdout + result.stderr)
        raise
    finally:
        cleanup_errors = []
        for command in ([('rm', '--force', '--volumes', name) for name in reversed(containers)] +
                        [('volume', 'rm', name) for name in reversed(volumes)]):
            try:
                result = docker(*command, check=False)
                if result.returncode and 'No such container' not in result.stderr:
                    cleanup_errors.append(redact(result.stderr.strip()))
            except Exception as error:
                cleanup_errors.append(redact(str(error)))
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            report['status'] = 'failed'
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(f'{service} {args.version}: {report["status"]} ({report.get("mode", "setup")}); report: {args.report}')
        if cleanup_errors:
            raise RuntimeError('Test resource cleanup failed; see report')


if __name__ == '__main__':
    main()
