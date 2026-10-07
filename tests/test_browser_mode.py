import unittest
from pathlib import Path
from tests.test_google_lens import LensJavaScriptTests
ROOT = Path(__file__).resolve().parents[1]
class BrowserModeTests(unittest.TestCase):
    run_node = LensJavaScriptTests.run_node
    def test_config_and_exclusive_registration(self):
        self.assertTrue((ROOT / 'browser-config.ts').exists())
        source = (ROOT / 'browser-config.ts').read_text()
        self.run_node(source + '''
if (parseBrowserConfig({browserMode:'intent'}).browserMode !== 'intent') throw Error('intent');
if (parseBrowserConfig({browserMode:'direct'}).browserMode !== 'direct') throw Error('direct');
let failed=false; try {parseBrowserConfig({browserMode:'both'});} catch {failed=true;}
if (!failed) throw Error('invalid mode accepted');
if (parseBrowserConfig({}).crawlGuards !== true) throw Error('crawlGuards default');
if (parseBrowserConfig({crawlGuards:false}).crawlGuards !== false) throw Error('crawlGuards false');
failed=false; try {parseBrowserConfig({crawlGuards:'off'});} catch {failed=true;}
if (!failed) throw Error('non-boolean crawlGuards accepted');
''', suffix='.mts')
        source = (ROOT / 'index.ts').read_text()
        self.assertIn('if (browserMode === "direct") pi.registerTool', source)
        self.assertIn('if (browserMode === "intent") registerIntent(pi,', source)
        self.assertTrue((ROOT / 'intent/intent_service.py').exists())
