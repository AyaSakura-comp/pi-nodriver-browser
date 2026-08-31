import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_installs_extension_and_disables_conflicting_package(self):
        with tempfile.TemporaryDirectory() as temp:
            agent_dir = Path(temp) / 'agent'
            agent_dir.mkdir()
            settings = agent_dir / 'settings.json'
            settings.write_text(json.dumps({
                'packages': ['npm:pi-agent-browser', 'npm:pi-until-done'],
            }))
            env = {
                **os.environ,
                'PI_AGENT_DIR': str(agent_dir),
                'SKIP_SYSTEM_CHECKS': '1',
                'SKIP_PIP_INSTALL': '1',
            }

            subprocess.run([str(ROOT / 'install.sh')], cwd=ROOT, env=env, check=True)

            installer_source = (ROOT / 'install.sh').read_text()
            self.assertIn('PI_NODRIVER_SOCKET', installer_source)
            self.assertIn("'command': 'shutdown'", installer_source)
            extension = agent_dir / 'extensions/nodriver-browser'
            self.assertTrue((extension / 'index.ts').is_file())
            self.assertTrue((extension / 'worker.py').is_file())
            self.assertTrue((extension / 'browser_logic.py').is_file())
            extension_source = (extension / 'index.ts').read_text()
            self.assertIn('createConnection', extension_source)
            self.assertIn('--server', extension_source)
            self.assertIn('worker.disconnect()', extension_source)
            self.assertIn('ctx.sessionManager.getSessionId()', extension_source)
            self.assertIn('sessionId', extension_source)
            self.assertIn('PI_NODRIVER_SCREEN', extension_source)
            self.assertIn('500x1000x24', extension_source)
            self.assertIn('if (response.screenshotPath)', extension_source)
            self.assertIn('name: "fetch_image"', extension_source)
            self.assertIn('fetch-image ${JSON.stringify(params.url)}', extension_source)
            self.assertIn('response.imagePath', extension_source)
            self.assertIn('[[image: ${response.imagePath}]]', extension_source)
            self.assertIn('vision-mark <x> <y>', extension_source)
            self.assertIn('vision-click <preview-token>', extension_source)
            self.assertIn('vision-mark-drag <start_x>', extension_source)
            self.assertIn('vision-drag <preview-token>', extension_source)
            self.assertIn('close', extension_source)
            self.assertIn('shutdown', extension_source)
            worker_source = (extension / 'worker.py').read_text()
            self.assertIn('PI_NODRIVER_WINDOW_SIZE', worker_source)
            self.assertIn('500,1000', worker_source)
            self.assertIn('--start-maximized', worker_source)
            self.assertIn('--window-position=0,0', worker_source)
            self.assertIn('--disable-features=Translate', worker_source)
            self.assertIn('--disable-session-crashed-bubble', worker_source)
            self.assertIn('--hide-crash-restore-bubble', worker_source)
            native_click = worker_source.split('    async def native_click', 1)[1].split('    async def execute', 1)[0]
            self.assertIn('minimum_settle_seconds = 0.1', native_click)
            self.assertIn('maximum_settle_seconds = 0.5', native_click)
            self.assertIn('new_tab_timeout_seconds = 2.0', native_click)
            self.assertNotIn('page.sleep(1)', native_click)
            updated = json.loads(settings.read_text())
            self.assertEqual(updated['packages'], ['npm:pi-until-done'])
            self.assertTrue((agent_dir / 'settings.json.pi-nodriver-browser.bak').is_file())

    def test_ref_guidance_uses_literal_examples_and_never_teaches_angle_wrapped_refs(self):
        extension_source = (ROOT / 'index.ts').read_text()
        worker_source = (ROOT / 'worker.py').read_text()
        readme_source = (ROOT / 'README.md').read_text()

        self.assertIn('REF SYNTAX IS LITERAL', extension_source)
        self.assertIn("click @e16", extension_source)
        self.assertIn("fill @e6", extension_source)
        self.assertIn('never wrap refs in', extension_source)
        self.assertNotIn('<@ref>', extension_source)
        self.assertNotIn('<@ref>', readme_source)
        self.assertNotIn('usage: click <@ref>', worker_source)
        self.assertIn('usage: click @e1', worker_source)
        self.assertIn('do not include < or >', worker_source)
        self.assertIn('To enter text, use fill or type with a literal ref', extension_source)
        self.assertNotIn('Press Enter, Tab, Space, Backspace, or text', extension_source)

    def test_form_safety_and_navigation_guidance_matches_runtime_guards(self):
        extension_source = (ROOT / 'index.ts').read_text()
        readme_source = (ROOT / 'README.md').read_text()
        worker_source = (ROOT / 'worker.py').read_text()

        self.assertIn('Never fill or type into a <label> ref', extension_source)
        self.assertIn('checked="true|false"', extension_source)
        self.assertIn('same origin', extension_source)
        self.assertIn('different-origin', extension_source)
        self.assertIn('authoritative and may be used immediately', worker_source)
        self.assertIn('same origin', readme_source)
        self.assertNotIn('run snapshot -i once to unlock ref commands', extension_source)

    def test_installs_parallel_image_delivery_and_incidental_crawl_image_guidance(self):
        extension_source = (ROOT / 'index.ts').read_text()
        worker_source = (ROOT / 'worker.py').read_text()

        self.assertIn('name: "fetch_images"', extension_source)
        self.assertIn('Promise.allSettled', extension_source)
        self.assertGreaterEqual(extension_source.count('throw new Error("fetch_images cancelled")'), 2)
        self.assertIn('Type.Array(Type.String', extension_source)
        self.assertIn('MAX_BATCH_IMAGE_BYTES', extension_source)
        self.assertIn('attachmentBytes', extension_source)
        batch_tool = extension_source.split('name: "fetch_images"', 1)[1].split('name: "crawl"', 1)[0]
        self.assertNotIn('type: "image"', batch_tool)
        self.assertIn('without injecting image bytes into the next model turn', batch_tool)
        self.assertIn('concrete products, people, places, animals, or events', extension_source)
        self.assertIn('even when the user did not explicitly ask for images', extension_source)
        self.assertIn('Do not finalize a concrete-subject answer', extension_source)
        self.assertIn('clean page text plus ranked image candidates', extension_source)
        self.assertIn('get text|images|url|title', extension_source)
        self.assertIn("extract_image_candidate_result(tab)", worker_source)
        self.assertIn("'imageCandidates': image_candidates", worker_source)
        self.assertIn("'imageCount':", worker_source)

    def test_registers_directional_google_search_with_deduplication_guidance(self):
        extension_source = (ROOT / 'index.ts').read_text()
        worker_source = (ROOT / 'worker.py').read_text()

        self.assertIn('name: "google_search"', extension_source)
        self.assertIn('minItems: 1', extension_source)
        self.assertIn('maxItems: 4', extension_source)
        self.assertIn('official or primary sources', extension_source)
        self.assertIn('independent reviews or community experience', extension_source)
        self.assertIn('alternatives, risks, or counter-evidence', extension_source)
        self.assertIn('google-search ${JSON.stringify(searches)}', extension_source)
        self.assertIn('TIME_SENSITIVE_SEARCH_PATTERN', extension_source)
        self.assertIn('currentTime: Type.Optional', extension_source)
        self.assertIn('requires a fresh gettime(action: "now") result', extension_source)
        self.assertIn('[Current time confirmed:', extension_source)
        self.assertIn("if action == 'google-search':", worker_source)
        self.assertIn('select_diverse_search_results', worker_source)
        google_worker = worker_source.split("if action == 'google-search':", 1)[1].split("if action == 'crawl':", 1)[0]
        self.assertIn('width=1920', google_worker)
        self.assertIn('mobile=False', google_worker)

    def test_google_search_extracts_the_description_sibling_outside_the_heading_block(self):
        worker_source = (ROOT / 'worker.py').read_text()
        google_js = worker_source.split("GOOGLE_RESULTS_JS =", 1)[1].split("IMAGE_NAT64_WELL_KNOWN", 1)[0]

        self.assertIn("anchor.closest('[data-snhf]')?.parentElement", google_js)
        self.assertIn("[data-sncf=\"1\"], .VwiC3b, [data-snf=\"nke7rc\"]", google_js)
        self.assertIn("snippetElement?.innerText", google_js)

    def test_fetch_image_metadata_does_not_force_implicit_visual_intent(self):
        extension_source = (ROOT / 'index.ts').read_text()
        self.assertIn(
            'Use fetch_image after web_search, crawl, or browser when the user asks to see',
            extension_source,
        )
        self.assertNotIn('Treat visual-appearance questions', extension_source)
        self.assertNotIn('「X 長怎樣？」', extension_source)
        self.assertNotIn('what does X look like?', extension_source)
        self.assertNotIn('visually depictable real-world subject', extension_source)
        self.assertNotIn('explicit or implicit visual-delivery requests', extension_source)


if __name__ == '__main__':
    unittest.main()
