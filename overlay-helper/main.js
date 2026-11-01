// overlay-helper/main.js —— 元灵桌面悬浮窗壳（Electron 主进程，CommonJS）
// 透明无边框常驻窗：加载引擎内嵌 Overlay HTTP 服务（默认 127.0.0.1:8765/?embed=1）。
// 依赖可选：未安装 electron 时由插件侧降级为浏览器打开，本文件仅作壳。
'use strict'

const { app, BrowserWindow, Tray, Menu, screen } = require('electron')
const path = require('path')
const http = require('http')

// Overlay 后端地址（引擎 OverlayServer 提供 UI + API）
const OVERLAY_URL = process.env.OVERLAY_URL || 'http://127.0.0.1:8765/'
const EMBED_URL = OVERLAY_URL + (OVERLAY_URL.includes('?') ? '&' : '?') + 'embed=1'

let win = null
let tray = null
let watchdogTimer = null

function createWindow() {
  const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize
  const W = 380
  const H = 560
  win = new BrowserWindow({
    width: W,
    height: H,
    x: sw - W - 16,        // 右下角
    y: sh - H - 16,
    transparent: true,     // 透明
    frame: false,          // 无边框
    alwaysOnTop: true,     // 置顶
    skipTaskbar: true,     // 不显示在任务栏
    hasShadow: false,      // 无阴影
    focusable: true,
    resizable: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })
  win.loadURL(EMBED_URL)
  win.on('closed', () => { win = null })
  startWatchdog()
}

// 看门狗：每 5s 确保窗口置顶（部分平台 alt-tab 后会被夺顶）
function startWatchdog() {
  if (watchdogTimer) clearInterval(watchdogTimer)
  watchdogTimer = setInterval(() => {
    if (win && !win.isDestroyed()) {
      win.setAlwaysOnTop(true)
    }
  }, 5000)
}

function showWindow() {
  if (!win) { createWindow(); return }
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

function hideWindow() {
  if (win) win.hide()
}

function openInBrowser() {
  // 用 shell.openExternal 打开后端地址（无 electron 时本文件不会运行，故这是带窗内的兜底）
  try {
    const { shell } = require('electron')
    shell.openExternal(OVERLAY_URL)
  } catch (_) { /* ignore */ }
}

function buildTray() {
  // 托盘图标：无图标文件时用一个 1x1 透明 PNG 占位（避免 crash）
  const iconPath = path.join(__dirname, 'tray.png')
  const fs = require('fs')
  const icon = fs.existsSync(iconPath) ? iconPath : undefined
  try {
    tray = new Tray(icon || path.join(__dirname, 'fallback.ico'))
  } catch (_) {
    // 极端情况：无图标则跳过托盘
    return
  }
  const menu = Menu.buildFromTemplate([
    { label: '显示元灵悬浮窗', click: showWindow },
    { label: '隐藏', click: hideWindow },
    { label: '在浏览器中打开', click: openInBrowser },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ])
  tray.setToolTip('元灵 MetaSpirit')
  tray.setContextMenu(menu)
  tray.on('click', showWindow)
}

app.whenReady().then(() => {
  // 支持 --quit-all：先关闭后端再退出（后端由 Python 引擎托管，这里只是请求停止）
  if (process.argv.includes('--quit-all')) {
    const req = http.request({ hostname: '127.0.0.1', port: 8765, path: '/api/control', method: 'POST',
      headers: { 'Content-Type': 'application/json' } }, () => { app.quit() })
    req.on('error', () => app.quit())
    req.write(JSON.stringify({ action: 'stop' }))
    req.end()
    return
  }
  createWindow()
  buildTray()

  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow() })
})

app.on('window-all-closed', () => {
  // 悬浮窗允许"关窗不退出进程"：保持托盘常驻；真正退出走托盘菜单
})

app.on('before-quit', () => {
  if (watchdogTimer) clearInterval(watchdogTimer)
})
