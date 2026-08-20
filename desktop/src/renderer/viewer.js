'use strict'

/**
 * 产物容器：渲染 AI 生成的报告，内容类型无所谓。
 *
 * 安全边界是这个文件存在的理由。报告是模型生成的，可能引用了任意网页文本，
 * 因此：
 *  - markdown / text 走 createElement + textContent，从不 innerHTML
 *  - HTML 只在 sandbox iframe 里渲染，且 sandbox 不含 allow-scripts，
 *    所以 <script> 与 onerror 都不会执行；也不含 allow-same-origin，
 *    拿不到主文档
 *  - PDF 交给 Chromium 内置阅读器，同样在受限 frame 内
 * 任何「先拼字符串再塞进 DOM」的改法都会破掉这层，别这么做。
 */

;(function () {
  const t = (key, params) => window.WyckoffI18n.t(key, params)
  // Resolved per call, not cached, so a language switch mid-session is picked up.
  const KIND_KEYS = {
    markdown: 'viewer.kindMarkdown',
    html: 'viewer.kindHtml',
    pdf: 'viewer.kindPdf',
    text: 'viewer.kindText',
    unsupported: 'viewer.kindFile'
  }
  const kindLabel = (kind) => t(KIND_KEYS[kind] || 'viewer.kindFile')

  const el = (tag, cls, text) => {
    const node = document.createElement(tag)
    if (cls) node.className = cls
    if (text != null) node.textContent = text
    return node
  }

  /** blob URL 让 iframe 天然是 opaque origin，比 srcdoc 更难拿到宿主。 */
  const blobUrl = (data, mime) => URL.createObjectURL(new Blob([data], { type: mime }))

  const b64ToBytes = (b64) => {
    const bin = atob(b64)
    const out = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
    return out
  }

  /** 记录本次渲染创建的 blob URL，切换产物时回收，避免内存泄漏。 */
  let liveUrls = []
  const releaseUrls = () => {
    for (const url of liveUrls) URL.revokeObjectURL(url)
    liveUrls = []
  }

  function renderHtml (content) {
    const frame = document.createElement('iframe')
    frame.className = 'vframe'
    // 不给 allow-scripts / allow-same-origin：报告只需要显示，不需要执行。
    frame.setAttribute('sandbox', '')
    frame.setAttribute('referrerpolicy', 'no-referrer')
    const url = blobUrl(content, 'text/html')
    liveUrls.push(url)
    frame.src = url
    return frame
  }

  function renderPdf (b64) {
    const url = blobUrl(b64ToBytes(b64), 'application/pdf')
    liveUrls.push(url)
    const frame = document.createElement('iframe')
    frame.className = 'vframe'
    frame.src = url
    return frame
  }

  function renderText (content) {
    const pre = el('pre', 'vtext')
    // textContent：文本产物里的 < > 必须显示为字符，不是标签。
    pre.textContent = content
    return pre
  }

  /**
   * Hand report content to main for PDF export.
   * markdown: re-render the source to the .doc DOM and send its outerHTML; main
   *   wraps it in print.css (read from disk there — the renderer's CSP forbids
   *   fetching it, and main already reads files).
   * html: the model's document carries its own styles, send it whole.
   */
  async function exportReport (btn, payload, onError) {
    const original = btn.textContent
    btn.disabled = true
    btn.textContent = t('viewer.exporting')
    try {
      const isMd = payload.kind === 'markdown' && window.WyckoffMd
      const body = isMd
        ? window.WyckoffMd.renderMarkdown(payload.content).outerHTML
        : payload.content
      const name = String(payload.name || 'report').replace(/\.[^.]+$/, '')
      const res = await window.wyckoff.exportPdf({ body, wrap: isMd, name })
      if (!res.ok && !res.canceled) onError(t('viewer.exportFailed', { error: res.error || '' }))
    } finally {
      btn.disabled = false
      btn.textContent = original
    }
  }

  function renderBody (payload) {
    if (payload.kind === 'markdown') {
      // md.js 同样是纯 createElement 实现，可以安全复用。
      return window.WyckoffMd
        ? window.WyckoffMd.renderMarkdown(payload.content)
        : renderText(payload.content)
    }
    if (payload.kind === 'html') return renderHtml(payload.content)
    if (payload.kind === 'pdf') return renderPdf(payload.content)
    return renderText(payload.content)
  }

  /**
   * @param {object} deps
   *  - call: (method, params) => Promise<result|null>
   *  - onError: (message) => void
   */
  window.createArtifactViewer = function createArtifactViewer (deps) {
    const root = el('div', 'vwrap')
    const listCol = el('div', 'vlist')
    const bodyCol = el('div', 'vbody')
    const fileInput = document.createElement('input')
    fileInput.type = 'file'
    fileInput.multiple = true
    fileInput.accept = '.md,.markdown,.html,.htm,.pdf,.txt,.log,.json,.csv'
    fileInput.hidden = true
    root.append(listCol, bodyCol, fileInput)

    let items = []
    let activeRel = null

    const showBody = async (rel) => {
      activeRel = rel
      bodyCol.replaceChildren(el('p', 'empty', t('viewer.loading')))
      const payload = await deps.call('artifact_read', { path: rel })
      // 期间用户可能已经点了别的产物，丢弃过期结果。
      if (activeRel !== rel) return
      releaseUrls()
      if (!payload) {
        bodyCol.replaceChildren(el('p', 'empty', t('viewer.readFailed')))
        return
      }
      const head = el('div', 'vhead')
      head.appendChild(el('span', 'vname', payload.name))
      head.appendChild(el('span', 'vkind', kindLabel(payload.kind)))
      // PDF export for the two kinds whose content we can serialize: markdown
      // (our .doc DOM) and html (the model's own document). text/pdf get none.
      if (window.wyckoff.exportPdf && (payload.kind === 'markdown' || payload.kind === 'html')) {
        const btn = el('button', 'vexport', t('viewer.exportPdf'))
        btn.onclick = () => exportReport(btn, payload, deps.onError)
        head.appendChild(btn)
      }
      bodyCol.replaceChildren(head, renderBody(payload))
    }

    const renderList = () => {
      listCol.replaceChildren()
      if (!items.length) return
      for (const item of items) {
        const row = el('button', item.rel_path === activeRel ? 'vitem on' : 'vitem')
        row.appendChild(el('span', 'vi-name', item.name))
        const meta = item.kind === 'unsupported'
          ? t('viewer.unsupported')
          : `${kindLabel(item.kind)} · ${Math.max(1, Math.round(item.size / 1024))}KB`
        row.appendChild(el('span', 'vi-meta', meta))
        row.disabled = item.kind === 'unsupported'
        row.onclick = async () => {
          await showBody(item.rel_path)
          renderList()
        }
        listCol.appendChild(row)
      }
    }

    const renderEmpty = () => {
      root.classList.add('is-empty')
      const empty = el('div', 'vempty')
      const icon = el('i', 'vempty-icon')
      icon.dataset.lucide = 'folder-open'
      icon.setAttribute('aria-hidden', 'true')
      const title = el('h2', null, t('viewer.emptyTitle'))
      const hint = el('p', null, t('viewer.emptyHint'))
      const button = el('button', 'vimport', t('viewer.import'))
      button.type = 'button'
      button.onclick = () => fileInput.click()
      empty.append(icon, title, hint, button)
      bodyCol.replaceChildren(empty)
      if (window.WyckoffRefreshIcons) window.WyckoffRefreshIcons()
    }

    const importFiles = async (files) => {
      if (!files.length) return
      let last = null
      for (const file of files) {
        const source = window.wyckoff.filePath ? window.wyckoff.filePath(file) : file.path
        if (!source) {
          deps.onError(t('viewer.dropPathFailed'))
          continue
        }
        const res = await deps.call('artifact_import', { source })
        if (res) last = res.rel_path
        else deps.onError(t('viewer.importFailed', { name: file.name }))
      }
      fileInput.value = ''
      await refresh(last)
    }

    const refresh = async (selectRel) => {
      const res = await deps.call('artifact_list')
      items = (res && res.items) || []
      root.classList.toggle('is-empty', !items.length)
      if (!items.length) {
        renderList()
        renderEmpty()
        return
      }
      const firstPreview = items.find((item) => item.kind !== 'unsupported')
      const target = selectRel || activeRel || (firstPreview && firstPreview.rel_path)
      const exists = items.some((i) => i.rel_path === target)
      renderList()
      if (exists) await showBody(target)
      else bodyCol.replaceChildren(el('p', 'empty', t('viewer.pickHint')))
      renderList()
    }

    // 拖放导入：文件先复制进报告目录，容器只信任那一个位置。
    root.addEventListener('dragover', (event) => {
      event.preventDefault()
      root.classList.add('drop')
    })
    root.addEventListener('dragleave', () => root.classList.remove('drop'))
    root.addEventListener('drop', async (event) => {
      event.preventDefault()
      root.classList.remove('drop')
      await importFiles([...(event.dataTransfer?.files || [])])
    })
    fileInput.addEventListener('change', () => importFiles([...(fileInput.files || [])]))

    return {
      node: root,
      refresh,
      dispose: releaseUrls
    }
  }
})()
