import asyncio
import unittest

from research.images import ResearchImages


def candidate(url, role='content', width=800, height=600, alt=''):
    return dict(url=url, role=role, width=width, height=height, alt=alt, caption='', score=50)


class ResearchImagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_picks_relevant_images_skips_icons_and_reports_only_finished(self):
        import tempfile
        folder = tempfile.mkdtemp()
        fetched = []
        release_slow = asyncio.Event()
        async def fetch(url):
            fetched.append(url)
            if 'slow' in url:
                await release_slow.wait()
            path = f'{folder}/{url.rsplit("/", 1)[-1]}.jpg'
            with open(path, 'wb') as handle:
                handle.write(url.encode())
            return dict(path=path, width=800, height=600)
        images = ResearchImages('台南 黑琵季 活動', fetch, max_deliver=3, max_fetch=6)
        images.offer('s1', '台南活動', [
            candidate('https://a.example/logo.png'),
            candidate('https://a.example/thumb', role='thumbnail'),
            candidate('https://a.example/tiny', width=80, height=60),
            candidate('https://a.example/icon.svg'),
            candidate('https://a.example/blackfaced', alt='台南 黑琵季 活動海報'),
            candidate('https://a.example/other', alt='廣告'),
            candidate('https://a.example/third', alt='第三張'),
        ])
        images.offer('s2', '別的頁面', [candidate('https://b.example/slow', role='representative')])
        await images.settle(0.05)
        self.assertEqual(sorted(fetched), ['https://a.example/blackfaced', 'https://a.example/other',
                                           'https://b.example/slow'], 'per page cap 2; icons/thumbs/tiny skipped')
        text = images.section(delivered_sources={'s1'})
        self.assertIn(f'[[image: {folder}/blackfaced.jpg]] [s1] 台南 黑琵季 活動海報 (800x600)', text)
        self.assertNotIn('slow', text, 'downloads still running after the wait are not listed')
        self.assertLess(text.index('blackfaced'), text.index('other'), 'more relevant first')
        images.cancel()

    async def test_identical_images_from_different_urls_are_listed_once(self):
        import os, tempfile
        folder = tempfile.mkdtemp()
        async def fetch(url):
            path = os.path.join(folder, url.rsplit('/', 1)[-1] + '.jpg')
            with open(path, 'wb') as handle:
                handle.write(b'same-bytes' if 'dup' in url else url.encode())
            return dict(path=path, width=800, height=600)
        images = ResearchImages('q', fetch)
        images.offer('s1', 't', [candidate('https://a.example/dup1'), candidate('https://a.example/dup2')])
        images.offer('s2', 't', [candidate('https://b.example/unique')])
        await images.settle(1)
        text = images.section()
        self.assertEqual(text.count('- [[image:'), 2)
        self.assertIn('unique.jpg', text)

    async def test_failed_downloads_and_no_images_give_no_section(self):
        async def fetch(url):
            raise ValueError('not an image')
        images = ResearchImages('q', fetch)
        images.offer('s1', 't', [candidate('https://a.example/x')])
        await images.settle(0.5)
        self.assertEqual(images.section(), '')
        self.assertEqual(ResearchImages('q', fetch).section(), '')
