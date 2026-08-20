import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { copyFileSync, existsSync } from 'node:fs'

/**
 * 迁移期间尚未搬到 React 的脚本仍是普通 <script>（非 module）。Vite 不打包
 * 它们，也不会复制 —— 不显式搬过去，dist/index.html 里的引用会全部 404，
 * 应用直接白屏（而且 CSP 下控制台只报 ERR_FILE_NOT_FOUND，看不出根因）。
 *
 * 每搬完一个模块就从这个清单里删掉一项；清单空了说明迁移完成。
 */
const LEGACY_SCRIPTS = [
  'locales.js',
  'i18n.js',
  'md.js',
  'charts.js',
  'kline.js',
  'annotations.js',
  'viewer.js',
  'tabs.js',
  // app.js 已拆：外壳归 React（main.tsx），命令式部分留在 shell.js
  'shell.js',
  'print.css'
]

function copyLegacyAssets (): Plugin {
  return {
    name: 'wyckoff-copy-legacy',
    // writeBundle 而不是 closeBundle：要在 Vite 写完产物之后、但仍在构建
    // 生命周期内，这样复制失败会让构建失败而不是静默产出坏包。
    writeBundle () {
      const from = resolve(__dirname, 'src/renderer')
      const to = resolve(__dirname, 'src/renderer/dist')
      for (const name of LEGACY_SCRIPTS) {
        const src = resolve(from, name)
        if (!existsSync(src)) {
          throw new Error(`legacy 资源不存在: ${name}（搬完了就从 LEGACY_SCRIPTS 里删掉）`)
        }
        copyFileSync(src, resolve(to, name))
      }
    }
  }
}

/**
 * 渲染层构建。产物进 src/renderer/dist/，main.js 从那里 loadFile。
 *
 * base: './' 是关键 —— Electron 用 file:// 加载页面，绝对路径的 /assets/xxx
 * 会解析到文件系统根目录，页面白屏且控制台只报 net::ERR_FILE_NOT_FOUND。
 *
 * 迁移期间 index.html 仍是老的 vanilla 入口；React 从 main.tsx 挂到
 * #react-root，逐屏替换。全部搬完后老入口才删。
 */
export default defineConfig({
  root: resolve(__dirname, 'src/renderer'),
  base: './',
  // root 和 publicDir 不能是同一个目录，而 vanilla 脚本就住在 root 里，
  // 所以用插件在打包末尾复制，见下面 copyLegacyAssets。
  publicDir: false,
  plugins: [react(), copyLegacyAssets()],
  build: {
    outDir: resolve(__dirname, 'src/renderer/dist'),
    emptyOutDir: true,
    // 桌面端不需要压缩到极致，可读的产物更好排查；也避免 terser 改动
    // canvas 绘图里对属性名的假设。
    minify: 'esbuild',
    sourcemap: true,
    rollupOptions: {
      input: resolve(__dirname, 'src/renderer/index.html')
    }
  },
  server: {
    port: 5273,
    strictPort: true
  }
})
