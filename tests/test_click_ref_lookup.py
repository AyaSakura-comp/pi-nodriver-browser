"""Execute click-target JS in a deterministic DOM fixture (no Chrome required)."""
import json
import subprocess
import unittest
from worker import CLICK_TARGET_JS


class ClickRefLookupTests(unittest.TestCase):
    def evaluate_fixture(self, extra=''):
        fixture = r'''
const measured = {target: 0, unrelated: 0};
const document = {defaultView: {
  innerWidth: 800, innerHeight: 600,
  getComputedStyle: el => ({display:'block', visibility:'visible', opacity:'1'}),
  crypto: {randomUUID: () => 'fixture-id'},
}};
function element(label, ref) {
  return {
    ownerDocument: document, tagName: 'BUTTON', id: '', type: '', value: '',
    innerText: label, textContent: label, shadowRoot: null, href: '',
    getAttribute: name => name === 'data-pi-ref' ? ref : null,
    hasAttribute: () => false, matches: () => false, closest: () => null,
    scrollIntoView: () => {},
    getBoundingClientRect: () => {
      measured[label] = (measured[label] || 0) + 1;
      return {left:10, top:20, right:110, bottom:60, width:100, height:40};
    },
  };
}
const unrelated = element('unrelated', null);
const target = element('target', 'e18');
document.querySelectorAll = selector => selector === '*' ? [unrelated,target] : [target];
EXTRA
const result = JSON.parse(eval(EXPRESSION));
console.log(JSON.stringify({result, measured}));
'''
        expression = CLICK_TARGET_JS.replace('__PI_CLICK_REQUEST__', json.dumps({'kind':'ref','value':'e18'}))
        code = fixture.replace('EXTRA', extra).replace('EXPRESSION', json.dumps(expression))
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_ref_lookup_does_not_measure_unrelated_dom_nodes(self):
        data = self.evaluate_fixture()
        self.assertTrue(data['result']['found'])
        self.assertEqual((data['result']['x'],data['result']['y']), (60,40))
        self.assertEqual(data['measured']['unrelated'], 0,
                         'A literal ref must not force layout reads for every DOM node')

    def test_ref_in_shadow_root(self):
        data = self.evaluate_fixture('''
const host = element('host', null);
host.shadowRoot = {querySelectorAll: () => [target]};
document.querySelectorAll = selector => selector === '*' ? [unrelated,host] : [];
''')
        self.assertTrue(data['result']['found'])
        self.assertEqual(data['measured']['unrelated'], 0)

    def test_ref_in_same_origin_frame_preserves_offsets(self):
        data = self.evaluate_fixture('''
const frame = element('frame', null);
frame.tagName = 'IFRAME';
frame.contentDocument = {defaultView: document.defaultView, querySelectorAll: () => [target]};
target.ownerDocument = frame.contentDocument;
document.querySelectorAll = selector => selector === '*' ? [unrelated,frame] : [];
''')
        self.assertTrue(data['result']['found'])
        self.assertEqual((data['result']['x'],data['result']['y']), (70,60))
        self.assertEqual(data['measured']['unrelated'], 0)

    def test_hidden_ref_is_not_admitted(self):
        data = self.evaluate_fixture("document.defaultView.getComputedStyle = () => ({display:'none'});")
        self.assertFalse(data['result']['found'])

    def test_disabled_ref_is_not_actionable(self):
        data = self.evaluate_fixture("target.matches = selector => selector === ':disabled';")
        self.assertTrue(data['result']['disabled'])


if __name__ == '__main__':
    unittest.main()
