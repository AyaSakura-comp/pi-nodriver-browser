"""Direct HTTP downloads for images and PDFs found on pages.

Only public addresses are contacted: the resolver drops loopback, private,
link-local and other non-global IPs, so a page cannot point a download at a
service on this machine or the LAN.
"""
import asyncio
import hashlib
import io
import ipaddress
import re
import socket
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp.abc import AbstractResolver
from PIL import Image

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/147.0.0.0 Safari/537.36')
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_MAX_PIXELS = 40_000_000
PDF_MAX_BYTES = 40 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS


class FetchError(Exception):
    pass


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        results = []
        for family_, _type, proto, _canon, address in infos:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global:
                continue
            results.append({'hostname': host, 'host': address[0], 'port': address[1],
                            'family': family_, 'proto': proto, 'flags': socket.AI_NUMERICHOST})
        if not results:
            raise OSError(f'{host} has no public address')
        return results

    async def close(self):
        pass


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise FetchError(f'not a public http(s) URL: {url[:200]}')
    try:
        if not ipaddress.ip_address(parts.hostname).is_global:
            raise FetchError('private address')
    except ValueError:
        pass
    return url


async def download(url, *, max_bytes, timeout, accept):
    """GET url (redirects followed, each hop re-resolved publicly). Returns (bytes, final_url)."""
    check_url(url)
    headers = {'User-Agent': USER_AGENT, 'Accept': accept}
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=4)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.get(url, headers=headers, max_redirects=5) as response:
                if response.status >= 400:
                    raise FetchError(f'HTTP {response.status}')
                check_url(str(response.url))
                declared = response.content_length
                if declared is not None and declared > max_bytes:
                    raise FetchError(f'response is {declared} bytes (limit {max_bytes})')
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f'response exceeds {max_bytes} bytes')
                    chunks.append(chunk)
                return b''.join(chunks), str(response.url)
    except FetchError:
        raise
    except asyncio.TimeoutError:
        raise FetchError(f'download timed out after {timeout:g}s') from None
    except (aiohttp.ClientError, OSError) as error:
        raise FetchError(f'{type(error).__name__}: {error}') from None


def normalize_image(data):
    """Validate image bytes; keep JPEG/PNG as they are, re-encode anything else
    (WebP, GIF, AVIF, MPO, BMP...) to PNG/JPEG that every consumer can decode."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if width * height > IMAGE_MAX_PIXELS:
                raise FetchError('image exceeds 40 megapixels')
            fmt = (image.format or '').upper()
            if fmt in ('JPEG', 'PNG'):
                return data, f'image/{fmt.lower()}', '.jpg' if fmt == 'JPEG' else '.png', width, height
            frame = image.convert('RGBA' if 'A' in image.getbands() or image.mode == 'P' else 'RGB')
            out = io.BytesIO()
            if frame.mode == 'RGBA':
                frame.save(out, 'PNG', optimize=True)
                return out.getvalue(), 'image/png', '.png', width, height
            frame.save(out, 'JPEG', quality=92)
            return out.getvalue(), 'image/jpeg', '.jpg', width, height
    except FetchError:
        raise
    except Exception as error:
        raise FetchError(f'not a decodable image: {error}') from None


def safe_name(text, limit=48):
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', text).strip('-.')[:limit]
    return stem or 'file'


async def fetch_image(url, directory):
    data, final_url = await download(url, max_bytes=IMAGE_MAX_BYTES, timeout=15.0,
                                     accept='image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5')
    data, mime, suffix, width, height = await asyncio.to_thread(normalize_image, data)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.urlsplit(final_url).path).stem
    digest = hashlib.sha1(data).hexdigest()[:8]
    path = directory / f'{digest}-{safe_name(name)}{suffix}'
    path.write_bytes(data)
    return path, mime, width, height, final_url


async def pdf_text(url, timeout=20.0):
    data, _final_url = await download(url, max_bytes=PDF_MAX_BYTES, timeout=timeout,
                                      accept='application/pdf,*/*;q=0.5')
    if not data.startswith(b'%PDF'):
        raise FetchError('not a PDF')
    process = await asyncio.create_subprocess_exec(
        'pdftotext', '-layout', '-enc', 'UTF-8', '-', '-',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(process.communicate(data), timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise FetchError('pdftotext timed out') from None
    text = out.decode('utf-8', 'replace')
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()
