'use strict'

// 组件写的类名必须在样式表里真的存在。
//
// 我在那一版的审批页写了 className="evidence"，而 app.css 定义的是 .approval-evidence
// （两列网格 + 上分隔线）。没有任何东西报错，只是那块元信息退化成一行一行
// 裸堆的标签/值 —— 用户的原话是「好丑」。账号菜单里的 .menu-email 同理，
// 样式表里叫 .menu-em。
//
// 这类 bug 我的探针抓不到：文字颜色正常、没有重叠、没有坏文本，元素也都在。
// 只有对着样式表核对类名才能发现。所以做成测试，不靠肉眼。
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync, readdirSync } = require('node:fs')
const { join } = require('node:path')

const R = join(__dirname, '..', 'src', 'renderer')

/** 样式表里出现过的类名（含 .a.b、.a .b 这些组合里的每一段）。 */
function definedClasses () {
  const css = readFileSync(join(R, 'app.css'), 'utf8')
  const out = new Set()
  for (const m of css.matchAll(/\.([a-zA-Z][\w-]*)/g)) out.add(m[1])
  return out
}

/** 组件里静态写死的 className（模板里插值的跳过 —— 那些要运行时才知道）。 */
function usedClasses () {
  const dir = join(R, 'components')
  const out = new Map()
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.tsx'))) {
    const src = readFileSync(join(dir, file), 'utf8')
    for (const m of src.matchAll(/className=["`]([^"`{]+)["`]/g)) {
      for (const name of m[1].trim().split(/\s+/)) {
        if (name && !out.has(name)) out.set(name, file)
      }
    }
  }
  return out
}

test('每个静态 className 在 app.css 里都有对应规则', () => {
  const defined = definedClasses()
  const missing = [...usedClasses()].filter(([name]) => !defined.has(name))
  assert.deepEqual(
    missing.map(([n, f]) => `${n} (${f})`),
    [],
    '这些类名样式表里没有 —— 元素会退化成无样式的裸堆'
  )
})

test('确认记录页的元信息用的是那个两列网格', () => {
  const src = readFileSync(join(R, 'components', 'RecordsPage.tsx'), 'utf8')
  const css = readFileSync(join(R, 'app.css'), 'utf8')
  assert.match(src, /className="approval-evidence"/, '容器类名写错就没有网格')
  // 顺带确认那条规则本身还在（有人重命名 CSS 时这两条会一起红）
  assert.match(css, /\.approval-evidence \{[^}]*display: grid/s)
  assert.match(css, /\.approval-evidence \{[^}]*grid-template-columns: repeat\(2/s)
})
