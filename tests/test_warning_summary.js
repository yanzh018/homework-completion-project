const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const start = source.indexOf('function isLayoutIssue(');
const end = source.indexOf('\nfunction paintReport()', start);
assert(start >= 0 && end > start);
const context = {};
vm.runInNewContext(source.slice(start, end), context);
const notices = [
  '仅有一个字形版本，无法避免重复：A B',
  '使用 AI 推测补字，非真实提取，请检查字形：7 ≠',
  '仅有一个字形版本，无法避免重复：B C',
  '使用 AI 推测补字，非真实提取，请检查字形：. 7',
  '其他需要保留的提示'
];
const result = Array.from(context.displayWarnings(notices));
assert.deepStrictEqual(result, [
  '使用 AI 推测补字，非真实提取，请检查字形：7 ≠ .',
  '仅有一个字形版本，无法避免重复：A B C',
  '其他需要保留的提示'
]);
assert.strictEqual(notices.length, 5, 'the original audit remains unchanged');
assert(context.isLayoutIssue('第 1 页检测到字形边界碰撞'));
assert(context.isLayoutIssue('第 1 页有笔迹压到原题'));
assert(!context.isLayoutIssue('此区域答案尚未勾选审阅确认'));
console.log('PASS warnings are grouped without dropping labels or changing the audit');
console.log('PASS collision and original-page overlap enter automatic layout adjustment');
