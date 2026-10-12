import json
import subprocess
import unittest
from worker import SNAPSHOT_JS, SELECT_OPTIONS_JS, CLICK_TARGET_JS, REF_ACTION_JS
from browser_logic import format_snapshot


class SnapshotLayoutBatchingTests(unittest.TestCase):
    def evaluate_fixture(self, extra='', expression=SNAPSHOT_JS):
        fixture = r'''
let dirtyLayout = false, forcedLayouts = 0;
const document = {defaultView: {
  innerWidth:800, innerHeight:600,
  getComputedStyle: () => ({display:'block',visibility:'visible',opacity:'1'}),
}};
const getComputedStyle = document.defaultView.getComputedStyle;
const elements = Array.from({length:3}, (_,i) => {
  const attributes = {};
  return {
    ownerDocument:document,tagName:'BUTTON',shadowRoot:null,
    innerText:'Control '+i,textContent:'Control '+i,value:'',tabIndex:-1,
    getAttribute: name => attributes[name] || null,
    hasAttribute: name => name in attributes,
    setAttribute: (name,value) => {attributes[name]=value;dirtyLayout=true;},
    removeAttribute: name => {delete attributes[name];dirtyLayout=true;},
    matches: selector => selector !== ':disabled',
    scrollIntoView: () => {},
    getBoundingClientRect: () => {
      if(dirtyLayout){forcedLayouts++;dirtyLayout=false;}
      return {left:10,top:20,right:110,bottom:60,width:100,height:40};
    },
  };
});
document.querySelectorAll = selector => selector === '*' ? elements : [];
EXTRA
const rows = JSON.parse(eval(EXPRESSION));
console.log(JSON.stringify({rows,forcedLayouts,refs:elements.map(e=>e.getAttribute('data-pi-ref')),rootQueries:document.rootQueries || 0}));
'''
        script = fixture.replace('EXTRA',extra).replace('EXPRESSION', json.dumps(expression))
        run = subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_layout_reads_precede_ref_attribute_writes(self):
        result = self.evaluate_fixture()
        self.assertEqual(len(result['rows']),3)
        self.assertEqual(result['refs'],['e1','e2','e3'])
        self.assertEqual(result['forcedLayouts'],0,
                         'Ref writes must not interleave with per-element layout reads')

    def test_cyclic_frame_document_is_not_recursively_revisited(self):
        result = self.evaluate_fixture('''
elements[2].tagName = 'IFRAME';
elements[2].contentDocument = document;
document.rootQueries = 0;
document.querySelectorAll = selector => {
  if(selector !== '*')return [];
  if(++document.rootQueries > 4)throw Error('fixture traversal runaway');
  return elements;
};
''')
        self.assertEqual(result['rootQueries'],1,
                         'Repeated frame documents must be stopped before recursion')
        self.assertEqual(len(result['rows']),3)

    def test_other_dom_walkers_do_not_revisit_cyclic_roots(self):
        cyclic = '''
elements[2].tagName = 'IFRAME';
elements[2].contentDocument = document;
document.body = elements[0];
document.rootQueries = 0;
document.querySelectorAll = selector => {
  if(selector !== '*')return [];
  if(++document.rootQueries > 4)throw Error('fixture traversal runaway');
  return elements;
};
'''
        scripts = {
            'select-options': SELECT_OPTIONS_JS,
            'click-css': CLICK_TARGET_JS.replace('__PI_CLICK_REQUEST__',json.dumps({'kind':'css','value':':disabled'})),
            'ref-action': REF_ACTION_JS.replace('__PI_REF_ACTION_REQUEST__',json.dumps({'ref':'e999','action':'fill','value':'test'})),
        }
        for name, expression in scripts.items():
            with self.subTest(name=name):
                result = self.evaluate_fixture(cyclic, expression)
                self.assertEqual(result['rootQueries'],1)

    def test_old_ref_in_skipped_shadow_root_cannot_alias_new_ref(self):
        setup = r'''
const a = elements[0], b = elements[1];
b.getBoundingClientRect = () => ({left:210,top:20,right:310,bottom:60,width:100,height:40});
function root(children) {
  return {querySelectorAll: selector => selector === '*' ? children :
    children.filter(el => el.getAttribute('data-pi-ref'))};
}
function host(child) {
  return {...elements[2],tagName:'DIV',matches:()=>false,shadowRoot:root([child])};
}
const hostA = host(a), hostB = host(b);
document.querySelectorAll = selector => selector === '*' ? [hostA,hostB] : [];
FIRST_SNAPSHOT
const originalRect = hostA.getBoundingClientRect;
hostA.getBoundingClientRect = () => ({width:0,height:0});
SECOND_SNAPSHOT
hostA.getBoundingClientRect = originalRect;
'''.replace('FIRST_SNAPSHOT', SNAPSHOT_JS + ';').replace('SECOND_SNAPSHOT', SNAPSHOT_JS + ';')
        click = CLICK_TARGET_JS.replace('__PI_CLICK_REQUEST__', json.dumps({'kind':'ref','value':'e1'}))
        result = self.evaluate_fixture(setup, click)
        self.assertEqual(result['rows']['x'], 260, 'e1 must resolve to B, not skipped A')

    def test_snapshot_root_budget_matches_ref_action_budget(self):
        setup = r'''
const button = elements[0];
const hosts = Array.from({length:256}, () => {
  const attrs = {};
  const child = {...button,
    getAttribute:name=>attrs[name] || null,
    setAttribute:(name,value)=>{attrs[name]=value;},
    removeAttribute:name=>{delete attrs[name];}};
  return {...button, tagName:'DIV', matches:()=>false,
    shadowRoot:{querySelectorAll:selector=>selector === '*' ? [child] :
      (child.getAttribute('data-pi-ref') ? [child] : [])}};
});
document.querySelectorAll = selector => selector === '*' ? hosts : [];
'''
        result = self.evaluate_fixture(setup)
        self.assertEqual(len(result['rows']), 255)
        self.assertTrue(all(row['snapshotTruncated'] for row in result['rows']))

    def test_truncated_snapshot_is_explicitly_marked_in_text(self):
        text = format_snapshot([{'ref':'e1','tag':'button','text':'Example','snapshotTruncated':True}])
        self.assertIn('truncated',text.lower())


if __name__ == '__main__':
    unittest.main()
