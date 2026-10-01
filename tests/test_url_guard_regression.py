import unittest
from pathlib import Path
from tests.test_google_lens import LensJavaScriptTests

ROOT = Path(__file__).resolve().parents[1]

class ExactUrlGuardTests(unittest.TestCase):
    run_node = LensJavaScriptTests.run_node

    def test_both_extension_guards(self):
        paths = [(ROOT / 'index.ts', 'browser', 'extractSearchResultUrls'),
                 (ROOT / 'index.ts', 'browser_intent', 'extractSearchResultUrls')]
        for path, tool, helper in paths:
            with self.subTest(tool=tool):
                text = path.read_text()
                helpers = text[text.index('function '+helper):text.index('export default function')]
                if helper == 'extractHttpUrls':
                    helpers = helpers[:helpers.index('const DESCRIPTION')]
                hooks = text[text.index('  const searchedUrls ='):text.index('  // Close this session') if helper == 'extractHttpUrls' else text.index('  pi.on("before_agent_start"')]
                # browser declares queue before searchedUrls, which the hooks do not use.
                script = helpers + '\nfunction register(pi) {\n' + hooks + '\n}\n'
                script += '''
const handlers = new Map();
register({on(n,f){handlers.set(n,f);}});
let entries = [];
const ctx = {sessionManager:{getSessionId:()=> 's',getEntries:()=>entries}};
await handlers.get('session_start')({},ctx);
const tool = TOOL;
const check = async url => handlers.get('tool_call')({toolName:tool,input:tool==='browser'?{command:'open '+url}:{action:'open',url}},ctx);
entries = [{type:'message',message:{role:'assistant',content:[{type:'text',text:'https://guessed.test/item'}]}}];
if (!(await check('https://guessed.test/item'))?.block) throw Error('assistant URL authorized');
await handlers.get('input')({text:'Open https://user.test/path/'},ctx);
if ((await check('https://user.test/path/'))?.block) throw Error('user URL blocked');
if (!(await check('https://user.test/path'))?.block) throw Error('modified slash authorized');
await handlers.get('tool_result')({toolName:'web_search',isError:false,content:[{type:'text',text:'https://search.test/item'}]},ctx);
if ((await check('https://search.test/item'))?.block) throw Error('search URL blocked');
await handlers.get('tool_result')({toolName:'web_search',isError:true,content:[{type:'text',text:'https://failed.test/item'}]},ctx);
if (!(await check('https://failed.test/item'))?.block) throw Error('failed search authorized');
'''.replace('TOOL', repr(tool))
                self.run_node(script, suffix='.mts')
