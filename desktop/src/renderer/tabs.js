'use strict'

/**
 * Artifact tab strip. Tabs sit on the grey bar; the selected tab bleeds into
 * the content area below it. Every tab uses the app theme — there is no
 * per-tab dark mode, so charts and reports look like one application.
 */

;(function () {
const t = (key, params) => window.WyckoffI18n.t(key, params)
const MAX_TABS = 6

const el = (tag, cls, text) => {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text !== undefined) node.textContent = text
  return node
}

class TabPane {
  constructor (stripId, bodyId, opts) {
    this.strip = document.getElementById(stripId)
    this.body = document.getElementById(bodyId)
    this.tabs = []
    this.activeId = null
    this.seq = 1
    this.renderToken = 0
    this.onCountChange = (opts && opts.onCountChange) || null
  }

  /** Let the host show/hide the pane based on whether any tab remains. */
  notifyCount () {
    if (this.onCountChange) this.onCountChange(this.tabs.length)
  }

  /** Notify the currently active tab that it is being replaced or closed. */
  runHide (nextKey) {
    if (!this.activeId || this.activeId === nextKey) return
    const current = this.tabs.find((item) => item.key === this.activeId)
    if (current && current.spec.onHide) current.spec.onHide()
  }

  /**
   * @param {object} spec
   *  - title: label text
   *  - glyph: leading icon
   *  - build: () => Node | Promise<Node>
   *  - key:   optional identity; same key replaces instead of duplicating
   */
  open (spec) {
    const key = spec.key || `t${this.seq++}`
    const existing = this.tabs.find((tab) => tab.key === key)
    if (existing) {
      existing.spec = spec
      this.select(key)
      return key
    }

    this.tabs.push({ key, spec })
    // Drop the oldest non-pinned tab rather than letting the strip overflow.
    if (this.tabs.length > MAX_TABS) {
      const victim = this.tabs.findIndex((tab) => !tab.spec.pinned && tab.key !== key)
      if (victim !== -1) this.tabs.splice(victim, 1)
    }
    this.notifyCount()
    this.select(key)
    return key
  }

  close (key) {
    const index = this.tabs.findIndex((tab) => tab.key === key)
    if (index === -1) return
    // Read onHide before splicing: afterwards the spec is gone from the list.
    const closing = this.tabs[index].spec
    if (closing.onHide) closing.onHide()
    this.tabs.splice(index, 1)
    if (this.activeId === key) {
      const next = this.tabs[index] || this.tabs[index - 1]
      this.activeId = null
      if (next) {
        this.select(next.key)
      } else {
        this.renderToken++
        this.body.replaceChildren()
      }
    }
    this.renderStrip()
    this.notifyCount()
  }

  /** Close every tab; the host then hides the pane. */
  closeAll () {
    // Detach any native view first, or it keeps floating over the closed pane.
    for (const tab of this.tabs) {
      if (tab.spec.onHide) tab.spec.onHide()
    }
    this.tabs = []
    this.activeId = null
    // Bump the token so an in-flight build cannot paint into the empty pane.
    this.renderToken++
    this.body.replaceChildren()
    this.renderStrip()
    this.notifyCount()
  }

  async select (key) {
    const tab = this.tabs.find((item) => item.key === key)
    if (!tab) return
    // Native views (the browser) float above the DOM, so leaving one attached
    // would cover whatever tab is selected next. Let the outgoing tab detach.
    this.runHide(key)
    this.activeId = key
    this.renderStrip()

    // Comparing activeId alone is not enough: two selects of the SAME key can
    // both pass that check and each append, rendering the body twice. A render
    // token means only the newest select is allowed to write.
    const token = ++this.renderToken

    this.body.replaceChildren()

    const placeholder = this.body.appendChild(el('p', 'empty', t('tab.loading')))
    try {
      const node = await tab.spec.build()
      if (token !== this.renderToken) return
      placeholder.remove()
      this.body.replaceChildren(node)
    } catch (err) {
      if (token !== this.renderToken) return
      placeholder.remove()
      this.body.replaceChildren(el('p', 'sys err', t('tab.loadFailed', { error: err && err.message ? err.message : err })))
    }
  }

  renderStrip () {
    this.strip.replaceChildren()
    for (const tab of this.tabs) {
      const isActive = tab.key === this.activeId
      const node = el('div', `tab${isActive ? ' on' : ''}`)
      node.appendChild(el('span', 'g', tab.spec.glyph || '▤'))
      node.appendChild(el('span', 'nm', tab.spec.title))
      if (!tab.spec.pinned) {
        const close = el('span', 'x', '✕')
        close.onclick = (event) => {
          event.stopPropagation()
          this.close(tab.key)
        }
        node.appendChild(close)
      }
      node.onclick = () => this.select(tab.key)
      this.strip.appendChild(node)
    }
  }

  has (key) {
    return this.tabs.some((tab) => tab.key === key)
  }
}

window.WyckoffTabs = { TabPane }
})()
