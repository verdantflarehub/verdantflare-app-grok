import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

function page() {
  const context = vm.createContext({ window: {}, document: { querySelector: () => ({}) }, AbortController });
  vm.runInContext(readFileSync(new URL('../dashboard/dashboard.mjs', import.meta.url), 'utf8'), context);
  return expression => vm.runInContext(expression, context);
}

test('new account does not inherit screenshot values or another channel URL', () => {
  const run = page();
  assert.equal(run('blank().url'), '');
  assert.equal(run('blank().plusUsed'), null);
  assert.equal(run('blank().botUsed'), null);
  assert.equal(run('blank().autoRecharge'), 'unknown');
  const html = run("editor='new'; form()");
  assert.match(html, /SuperGrok Plus/);
  assert.match(html, /Grok Bot/);
  assert.doesNotMatch(html, /5 小时|7 天|Google|原型|示例/);
});

test('weekly breakdown, unknown, zero credits and gateway units remain distinct', () => {
  const run = page();
  run(`accounts=[accountView({account_id:'account',name:'Member',api_url:'https://gateway.example.invalid/v1',
    proxy:{mode:'direct'},status:'disabled',registration:{plus_used_percent:'8',build_used_percent:'6',chat_used_percent:'2',
    bot_used_percent:null,extra_credit_usd:'0',auto_recharge:'unknown'},
    gateway_observation:{remaining:'12',unit:'Credits'}})];`);
  const html = run('list()');
  assert.match(html, /8%/);
  assert.match(html, /每周 Grok Bot<\/small><strong class="num">未登记/);
  assert.match(html, /0 USD/);
  assert.match(html, /12 Credits/);
  assert.match(html, /已停用/);
  assert.doesNotMatch(html, /示例/);
});

test('budget and settlement use their own amounts and units', () => {
  const run = page();
  const html = run(`logs={accounts:[],summary:{task_count:1,budget:{amount:'5',unit:'USD',unknown_count:0},
    settlement:{amount:'6',unit:'USD',unknown_count:0}},items:[]}; logView()`);
  assert.match(html, /5 USD/);
  assert.match(html, /6 USD/);
  assert.match(html, /生成前估算/);
  assert.doesNotMatch(html, /详情/);
});

test('v0.2 full-width layout, top breadcrumb, official brand logo and zero return buttons', () => {
  const index = readFileSync(new URL('../dashboard/index.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../dashboard/dashboard.css', import.meta.url), 'utf8');
  const run = page();
  const formHtml = run("editor='new'; form()");
  assert.ok(index.indexOf('id="appBreadcrumb"') < index.indexOf('class="topline"'));
  assert.match(index, /viewBox="0 0 24 24"/);
  assert.doesNotMatch(index, /渠道管理<\/span>/);
  assert.match(css, /\.viewport\s*\{[^}]*max-width:\s*none/);
  assert.match(css, /\.viewport\s*\{[^}]*padding:\s*0/);
  assert.match(css, /\.g-form\s*\{[^}]*max-width:\s*none/);
  assert.doesNotMatch(formHtml, /data-cancel|>返回概览<|>取消</);
});

