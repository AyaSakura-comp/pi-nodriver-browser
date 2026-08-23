#!/usr/bin/env python3
import argparse
import json
import os
import select
import subprocess
import tempfile
import time
from pathlib import Path


def read_cdp_message(read_fd, buffer, timeout=20):
    deadline = time.monotonic() + timeout
    while b'\0' not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Chrome CDP pipe response timed out')
        ready, _, _ = select.select([read_fd], [], [], remaining)
        if not ready:
            raise TimeoutError('Chrome CDP pipe response timed out')
        chunk = os.read(read_fd, 65536)
        if not chunk:
            raise RuntimeError('Chrome closed the CDP pipe')
        buffer.extend(chunk)
    raw, remainder = buffer.split(b'\0', 1)
    buffer[:] = remainder
    return json.loads(raw)


def chrome_pipe_command(chrome_executable, profile):
    return [
        str(chrome_executable),
        f'--user-data-dir={profile}',
        '--remote-debugging-pipe',
        '--enable-unsafe-extension-debugging',
        '--headless=new',
        '--no-first-run',
        '--no-default-browser-check',
        'about:blank',
    ]


class CdpPipeClient:
    def __init__(self, write_fd, read_fd):
        self.write_fd = write_fd
        self.read_fd = read_fd
        self.buffer = bytearray()
        self.next_id = 1

    def request(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        payload = {
            'id': request_id,
            'method': method,
            'params': params or {},
        }
        os.write(self.write_fd, json.dumps(payload).encode() + b'\0')
        while True:
            message = read_cdp_message(self.read_fd, self.buffer)
            if message.get('id') != request_id:
                continue
            if 'error' in message:
                error = message['error']
                raise RuntimeError(
                    f"Chrome CDP {method} failed: {error.get('message', error)}"
                )
            return message.get('result', {})


def load_and_verify_extensions(client, paths):
    resolved_paths = [Path(path).resolve() for path in paths]
    for path in resolved_paths:
        client.request('Extensions.loadUnpacked', {'path': str(path)})

    installed = client.request('Extensions.getExtensions').get('extensions', [])
    installed_by_path = {
        Path(extension['path']).resolve(): extension
        for extension in installed
        if extension.get('path')
    }
    verified = []
    for path in resolved_paths:
        extension = installed_by_path.get(path)
        if extension is None:
            raise RuntimeError(f'Chrome did not install extension: {path}')
        if not extension.get('enabled'):
            raise RuntimeError(f'Chrome extension is not enabled: {path}')
        verified.append(extension)
    return verified


def ensure_pipe_destinations_open():
    owned_fds = []
    for target_fd in (3, 4):
        try:
            os.fstat(target_fd)
        except OSError:
            while True:
                fd = os.open(os.devnull, os.O_RDONLY)
                owned_fds.append(fd)
                if fd >= target_fd:
                    break
    return owned_fds


def install_extensions(chrome_executable, profile, paths):
    profile = Path(profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    paths = [Path(path).resolve() for path in paths]
    for path in paths:
        if not (path / 'manifest.json').is_file():
            raise RuntimeError(f'Chrome extension manifest not found: {path}')

    process = None
    open_fds = set()
    with tempfile.TemporaryFile() as stderr:
        try:
            owned_destination_fds = ensure_pipe_destinations_open()
            open_fds.update(owned_destination_fds)
            command_read, command_write = os.pipe()
            response_read, response_write = os.pipe()
            open_fds.update((command_read, command_write, response_read, response_write))

            def prepare_child_pipe_fds():
                os.dup2(command_read, 3)
                os.dup2(response_write, 4)

            process = subprocess.Popen(
                chrome_pipe_command(chrome_executable, profile),
                pass_fds=(3, 4, command_read, response_write),
                preexec_fn=prepare_child_pipe_fds,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
            for fd in owned_destination_fds + [command_read, response_write]:
                os.close(fd)
                open_fds.discard(fd)

            client = CdpPipeClient(command_write, response_read)
            extensions = load_and_verify_extensions(client, paths)
            client.request('Browser.close')
            process.wait(timeout=20)
            if process.returncode != 0:
                stderr.seek(0)
                raise RuntimeError(
                    f'Chrome extension installer exited {process.returncode}: '
                    f'{stderr.read().decode(errors="replace")[-2000:]}'
                )
            return extensions
        finally:
            for fd in open_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('extension', nargs='+')
    args = parser.parse_args()
    extensions = install_extensions(args.chrome, args.profile, args.extension)
    print(json.dumps({
        'installed': [
            {
                'id': extension['id'],
                'name': extension['name'],
                'version': extension['version'],
                'path': extension['path'],
            }
            for extension in extensions
        ]
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
