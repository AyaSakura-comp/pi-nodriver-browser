"""Offline source-pin contracts; private submodules need not be initialized."""
import configparser
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'omniparser': ('OmniParser', 'master'),
    'laya': ('laya', 'laya'),
    'xvfb-streaming': ('xvfb-streaming', 'master'),
}


class DependencySubmoduleTests(unittest.TestCase):
    def test_expected_remotes_paths_and_branches(self):
        cfg = configparser.ConfigParser()
        cfg.read(ROOT / '.gitmodules')
        self.assertEqual(len(cfg.sections()), len(EXPECTED))
        for name, (repo, branch) in EXPECTED.items():
            section = cfg[f'submodule "dependencies/{name}"']
            self.assertEqual(section['path'], f'dependencies/{name}')
            self.assertEqual(section['url'], f'https://github.com/AyaSakura-comp/{repo}.git')
            self.assertEqual(section['branch'], branch)

    def test_index_contains_pinned_gitlinks(self):
        listing = subprocess.check_output(
            ['git', 'ls-files', '--stage', '--', 'dependencies/'], cwd=ROOT, text=True)
        rows = {line.split('\t')[1]: line.split('\t')[0].split() for line in listing.splitlines()}
        self.assertEqual(set(rows), {f'dependencies/{name}' for name in EXPECTED})
        for mode, sha, stage in rows.values():
            self.assertEqual(mode, '160000')
            self.assertRegex(sha, r'^[a-f0-9]{40}$')
            self.assertEqual(stage, '0')


if __name__ == '__main__':
    unittest.main()
