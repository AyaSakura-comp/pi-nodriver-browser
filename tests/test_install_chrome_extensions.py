import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class FakeCdpClient:
    def __init__(self, extensions):
        self.extensions = extensions
        self.requests = []

    def request(self, method, params=None):
        self.requests.append((method, params or {}))
        if method == 'Extensions.loadUnpacked':
            return {'id': f"id-{len(self.requests)}"}
        if method == 'Extensions.getExtensions':
            return {'extensions': self.extensions}
        raise AssertionError(f'unexpected method: {method}')


class ChromeExtensionPipeInstallerTests(unittest.TestCase):
    def test_reads_multiple_null_delimited_cdp_messages_without_losing_data(self):
        from install_chrome_extensions import read_cdp_message

        read_fd, write_fd = os.pipe()
        buffer = bytearray()
        try:
            os.write(
                write_fd,
                json.dumps({'id': 1, 'result': {'first': True}}).encode()
                + b'\0'
                + json.dumps({'id': 2, 'result': {'second': True}}).encode()
                + b'\0',
            )

            first = read_cdp_message(read_fd, buffer)
            second = read_cdp_message(read_fd, buffer)
        finally:
            os.close(read_fd)
            os.close(write_fd)

        self.assertEqual(first['id'], 1)
        self.assertEqual(second['id'], 2)

    def test_builds_pipe_command_with_required_extension_debugging_flags(self):
        from install_chrome_extensions import chrome_pipe_command

        self.assertEqual(
            chrome_pipe_command('/usr/bin/google-chrome', Path('/tmp/profile')),
            [
                '/usr/bin/google-chrome',
                '--user-data-dir=/tmp/profile',
                '--remote-debugging-pipe',
                '--enable-unsafe-extension-debugging',
                '--headless=new',
                '--no-first-run',
                '--no-default-browser-check',
                'about:blank',
            ],
        )

    def test_loads_and_verifies_every_requested_extension(self):
        from install_chrome_extensions import load_and_verify_extensions

        paths = [Path('/extensions/stealth'), Path('/extensions/buster')]
        client = FakeCdpClient([
            {
                'id': 'stealth-id',
                'name': 'Stealth',
                'version': '1.0.0',
                'path': '/extensions/stealth',
                'enabled': True,
            },
            {
                'id': 'buster-id',
                'name': 'Buster',
                'version': '3.4.0',
                'path': '/extensions/buster',
                'enabled': True,
            },
        ])

        extensions = load_and_verify_extensions(client, paths)

        self.assertEqual([extension['id'] for extension in extensions], ['stealth-id', 'buster-id'])
        self.assertEqual(
            client.requests,
            [
                ('Extensions.loadUnpacked', {'path': '/extensions/stealth'}),
                ('Extensions.loadUnpacked', {'path': '/extensions/buster'}),
                ('Extensions.getExtensions', {}),
            ],
        )

    def test_closes_pipe_fds_when_chrome_fails_to_start(self):
        from install_chrome_extensions import install_extensions

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            extension = temp / 'extension'
            extension.mkdir()
            (extension / 'manifest.json').write_text('{}')
            before = {int(fd) for fd in os.listdir('/proc/self/fd')}
            leaked = set()
            try:
                with mock.patch(
                    'install_chrome_extensions.subprocess.Popen',
                    side_effect=RuntimeError('Chrome failed to start'),
                ):
                    with self.assertRaisesRegex(RuntimeError, 'Chrome failed to start'):
                        install_extensions('/usr/bin/google-chrome', temp / 'profile', [extension])
                after = {int(fd) for fd in os.listdir('/proc/self/fd')}
                leaked = after - before
                self.assertEqual(leaked, set())
            finally:
                for fd in leaked:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_rejects_an_extension_that_chrome_did_not_enable(self):
        from install_chrome_extensions import load_and_verify_extensions

        client = FakeCdpClient([
            {
                'id': 'buster-id',
                'name': 'Buster',
                'version': '3.4.0',
                'path': '/extensions/buster',
                'enabled': False,
            },
        ])

        with self.assertRaisesRegex(RuntimeError, 'not enabled'):
            load_and_verify_extensions(client, [Path('/extensions/buster')])


if __name__ == '__main__':
    unittest.main()
