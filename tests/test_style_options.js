const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const start = source.indexOf('function paintStyleOptions()');
const end = source.indexOf('\nfunction invalidateReview()', start);
assert(start >= 0 && end > start);
const elements = {
  fontSize: {value: '16'}, autoFitSize: {checked: true}, autoFitHelp: {},
  allowInferred: {checked: true}, missingPolicy: {value: 'strict'}, missingPolicyHelp: {}
};
const context = {$: id => elements[id], assignment: {answers: [{text: 'answer', source: 'ai'}]}};
vm.runInNewContext(source.slice(start, end), context);
context.paintStyleOptions();
assert(!elements.autoFitSize.disabled && elements.autoFitHelp.textContent.includes('13 pt'));
assert(elements.missingPolicyHelp.textContent.includes('AI 推测字形'));
assert(elements.missingPolicyHelp.textContent.includes('阻止导出'));
elements.fontSize.value = '12';
context.paintStyleOptions();
assert(elements.autoFitSize.disabled && elements.autoFitSize.checked);
assert(elements.autoFitHelp.textContent.includes('没有可用的缩小空间'));
elements.fontSize.value = '10';
context.paintStyleOptions();
assert(elements.autoFitSize.disabled && elements.autoFitSize.checked);
elements.fontSize.value = '12.5';
context.assignment.answers[0].user_edited = true;
elements.allowInferred.checked = false;
elements.missingPolicy.value = 'reference';
context.paintStyleOptions();
assert(!elements.autoFitSize.disabled && elements.autoFitHelp.textContent.includes('12 pt'));
assert(elements.autoFitHelp.textContent.includes('不会自动缩小'));
assert(elements.missingPolicyHelp.textContent.includes('灰色印刷参考字'));
assert(elements.missingPolicyHelp.textContent.includes('AI 推测字形已关闭'));
assert(elements.missingPolicyHelp.textContent.includes('显示为问号'));
console.log('PASS style guidance reflects missing-glyph choices and size eligibility without changing saved selections');
