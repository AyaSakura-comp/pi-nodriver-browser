import unittest
from pathlib import Path

from tests.test_google_lens import LensJavaScriptTests


ROOT = Path(__file__).resolve().parents[1]


class UrlProvenanceJavaScriptTests(unittest.TestCase):
    run_node = LensJavaScriptTests.run_node

    def test_browser_open_requires_a_prior_search_result_in_the_same_session(self):
        text = (ROOT / 'index.ts').read_text()
        start = text.index('function extractSearchResultUrls')
        source = text[start:].replace('export default function', 'function register', 1)
        self.run_node(r'''
const Type = new Proxy({}, { get: () => (...args) => args[0] });
const DESCRIPTION = '', VISION_FALLBACK_GUIDANCE = '', SEARCH_FIRST_URL_RULE = '';
const DEFAULT_MAX_LINES = 2000, DEFAULT_MAX_BYTES = 50000;
const truncateHead = text => ({content:text, truncated:false});
class NodriverWorker { async request() { return {text:'ok'}; } async cleanupSession() {} disconnect() {} }
''' + source + r'''
const handlers = new Map();
const tools = new Map();
register({
  on(name, handler) { handlers.set(name, handler); },
  registerTool(tool) { tools.set(tool.name, tool); },
});
const ctx = {sessionManager:{getSessionId:()=> 'session-a'}};
await handlers.get('session_start')({}, ctx);

let blocked = await handlers.get('tool_call')({
  toolName:'browser', toolCallId:'1', input:{command:'open https://shop.example/item'}
}, ctx);
if (!blocked?.block || !blocked.reason.includes('URL_PROVENANCE_GUARD')) {
  throw new Error('unsearched browser open was not blocked');
}

await handlers.get('tool_result')({
  toolName:'web_search', toolCallId:'2', input:{query:'shop'}, isError:false,
  content:[{type:'text', text:'Result: https://shop.example/item'}], details:{}
}, ctx);
blocked = await handlers.get('tool_call')({
  toolName:'browser', toolCallId:'3', input:{command:'open https://shop.example/item'}
}, ctx);
if (blocked?.block) throw new Error('web_search result URL was blocked');

await handlers.get('tool_result')({
  toolName:'google_search', toolCallId:'4', input:{searches:[]}, isError:false,
  content:[{type:'text', text:'Result: https://other.example/path?q=1'}], details:{}
}, ctx);
blocked = await handlers.get('tool_call')({
  toolName:'browser', toolCallId:'5', input:{command:'open "https://other.example/path?q=1"'}
}, ctx);
if (blocked?.block) throw new Error('google_search result URL was blocked');

blocked = await handlers.get('tool_call')({
  toolName:'browser', toolCallId:'6', input:{command:'snapshot -i --full'}
}, ctx);
if (blocked?.block) throw new Error('non-open browser command was blocked');
''', suffix='.mts')

    def test_guidance_routes_unindexed_deep_links_through_search_result_parent(self):
        text = (ROOT / 'index.ts').read_text()
        self.assertIn('closest official parent URL', text)
        self.assertIn('navigate through visible links', text)
        self.assertIn('do not pass the unindexed deep URL', text)

    def test_browser_google_search_results_also_authorize_exact_open_urls(self):
        text = (ROOT / 'index.ts').read_text()
        start = text.index('function extractSearchResultUrls')
        source = text[start:].replace('export default function', 'function register', 1)
        self.run_node(r'''
const Type = new Proxy({}, { get: () => (...args) => args[0] });
const DESCRIPTION = '', VISION_FALLBACK_GUIDANCE = '', SEARCH_FIRST_URL_RULE = '';
const DEFAULT_MAX_LINES = 2000, DEFAULT_MAX_BYTES = 50000;
const truncateHead = text => ({content:text, truncated:false});
class NodriverWorker { async request() { return {text:'ok'}; } async cleanupSession() {} disconnect() {} }
''' + source + r'''
const handlers = new Map();
register({on(name, handler){handlers.set(name, handler);}, registerTool(){}});
const ctx = {sessionManager:{getSessionId:()=> 'session-a'}};
await handlers.get('session_start')({}, ctx);
await handlers.get('tool_result')({
  toolName:'browser', toolCallId:'1', input:{command:'google-search [{"query":"official"}]'},
  isError:false, content:[{type:'text', text:'Official\nhttps://official.example/home'}], details:{}
}, ctx);
const decision = await handlers.get('tool_call')({
  toolName:'browser', toolCallId:'2', input:{command:'open https://official.example/home'}
}, ctx);
if (decision?.block) throw new Error('browser google-search result URL was blocked');
''', suffix='.mts')

    def test_browser_open_allows_user_supplied_url_via_input_and_session_entries(self):
        text = (ROOT / 'index.ts').read_text()
        start = text.index('function extractSearchResultUrls')
        source = text[start:].replace('export default function', 'function register', 1)
        self.run_node(r'''
const Type = new Proxy({}, { get: () => (...args) => args[0] });
const DESCRIPTION = '', VISION_FALLBACK_GUIDANCE = '', SEARCH_FIRST_URL_RULE = '';
const DEFAULT_MAX_LINES = 2000, DEFAULT_MAX_BYTES = 50000;
const truncateHead = text => ({content:text, truncated:false});
class NodriverWorker { async request() { return {text:'ok'}; } async cleanupSession() {} disconnect() {} }
''' + source + r'''
const handlers = new Map();
register({on(name, handler){handlers.set(name, handler);}, registerTool(){}});
const entries = [
  {type: 'message', message: {role: 'user', content: 'Here is https://user.example/from-history'}}
];
const ctx = {
  sessionManager: {
    getSessionId: () => 'session-u',
    getEntries: () => entries,
  }
};
await handlers.get('session_start')({}, ctx);

// 1. URL from user input event is allowed
await handlers.get('input')({ text: 'Please check https://user.example/from-input' }, ctx);
let allowedInput = await handlers.get('tool_call')({
  toolName: 'browser', toolCallId: '1', input: { command: 'open https://user.example/from-input' }
}, ctx);
if (allowedInput?.block) throw new Error('user input URL was blocked: ' + allowedInput.reason);

// 2. URL from session history user message is allowed
let allowedHistory = await handlers.get('tool_call')({
  toolName: 'browser', toolCallId: '2', input: { command: 'open https://user.example/from-history' }
}, ctx);
if (allowedHistory?.block) throw new Error('user history URL was blocked: ' + allowedHistory.reason);

// 3. Trailing slash tolerance
let allowedSlash = await handlers.get('tool_call')({
  toolName: 'browser', toolCallId: '3', input: { command: 'open https://user.example/from-input/' }
}, ctx);
if (allowedSlash?.block) throw new Error('user input URL with trailing slash was blocked');

// 4. Guessed / hallucinated URL is blocked
let blocked = await handlers.get('tool_call')({
  toolName: 'browser', toolCallId: '4', input: { command: 'open https://guessed.example/path' }
}, ctx);
if (!blocked?.block || !blocked.reason.includes('URL_PROVENANCE_GUARD')) {
  throw new Error('guessed URL was not blocked');
}
''', suffix='.mts')


if __name__ == '__main__':
    unittest.main()

