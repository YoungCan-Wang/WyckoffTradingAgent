'use strict'

/**
 * 极简 i18n。桌面端原本全硬编码中文，这里把界面文案抽成 key，
 * 支持中英切换，并跟随系统语言。
 *
 * 设计取舍：
 *  - 不引第三方库。文案量固定、无复数变形需求，一个查表函数足够。
 *  - key 用点分命名（区域.用途），value 里的 {name} 是占位符，
 *    运行时用 t(key, { name }) 替换。
 *  - 缺 key 时回退到中文，再回退到 key 本身——宁可显示英文缺项，
 *    也不让界面出现空白。
 */

;(function () {
  const STORE_KEY = 'wyckoff.lang'
  const FALLBACK = 'zh'

  // locale 表由 locales.js 挂到 window.WyckoffLocales。
  const tables = () => window.WyckoffLocales || {}

  let current = FALLBACK

  /** 支持的语言码，供设置页构建下拉。 */
  function available () {
    return Object.keys(tables())
  }

  /** 系统语言映射到我们支持的语言码；不认识就回退。 */
  function systemLang () {
    const nav = (navigator.language || '').toLowerCase()
    if (nav.startsWith('zh')) return 'zh'
    if (nav.startsWith('en')) return 'en'
    return FALLBACK
  }

  /**
   * 决定初始语言：用户显式选过 > 系统语言 > 回退。
   * 存的值若已不在支持列表里（比如删了某语言），也回退。
   */
  function resolveInitial () {
    let saved = null
    try {
      saved = localStorage.getItem(STORE_KEY)
    } catch {
      saved = null
    }
    if (saved && available().includes(saved)) return saved
    const sys = systemLang()
    return available().includes(sys) ? sys : FALLBACK
  }

  function setLang (lang, { persist = true } = {}) {
    if (!available().includes(lang)) return
    current = lang
    if (persist) {
      try {
        localStorage.setItem(STORE_KEY, lang)
      } catch {
        /* private mode: 本次会话内仍生效，只是不持久化 */
      }
    }
    document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-CN' : lang)
  }

  function getLang () {
    return current
  }

  /**
   * 取译文。key 缺失时按 中文 -> key 本身 逐级回退。
   * params 里的键用 {key} 形式在文案中替换。
   */
  function t (key, params) {
    const all = tables()
    const table = all[current] || {}
    let value = table[key]
    if (value == null) value = (all[FALLBACK] || {})[key]
    if (value == null) return key
    if (!params) return value
    return value.replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
    )
  }

  /**
   * 把 data-i18n* 标注的静态节点刷成当前语言。
   *  - data-i18n           -> textContent
   *  - data-i18n-title     -> title 属性
   *  - data-i18n-placeholder -> placeholder 属性
   *  - data-i18n-aria-label  -> aria-label 属性
   * 动态生成的界面不走这里，靠重渲染时直接调用 t()。
   */
  function applyDom (root = document) {
    for (const node of root.querySelectorAll('[data-i18n]')) {
      node.textContent = t(node.getAttribute('data-i18n'))
    }
    const attrs = [
      ['data-i18n-title', 'title'],
      ['data-i18n-placeholder', 'placeholder'],
      ['data-i18n-aria-label', 'aria-label']
    ]
    for (const [dataAttr, domAttr] of attrs) {
      for (const node of root.querySelectorAll(`[${dataAttr}]`)) {
        node.setAttribute(domAttr, t(node.getAttribute(dataAttr)))
      }
    }
  }

  // 语言切换后要重刷整个界面：静态节点用 applyDom，动态视图交给订阅者。
  const listeners = new Set()
  function onChange (fn) {
    listeners.add(fn)
    return () => listeners.delete(fn)
  }
  function notify () {
    applyDom()
    for (const fn of listeners) {
      try {
        fn(current)
      } catch {
        /* 单个订阅者出错不该拖垮整轮刷新 */
      }
    }
  }

  window.WyckoffI18n = {
    t,
    setLang: (lang, opts) => {
      setLang(lang, opts)
      notify()
    },
    getLang,
    available,
    systemLang,
    resolveInitial,
    applyDom,
    onChange,
    STORE_KEY
  }
})()
