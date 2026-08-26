'use strict'

const PRINT_WEB_PREFERENCES = Object.freeze({
  sandbox: true,
  nodeIntegration: false,
  contextIsolation: true,
  javascript: false
})

function blockPrintNetwork (session) {
  session.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: /^https?:/i.test(details.url) })
  })
}

module.exports = { PRINT_WEB_PREFERENCES, blockPrintNetwork }
