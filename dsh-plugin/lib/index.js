/**
 * DSH 桌宠开关 · Host 半
 * ======================
 * 职责：
 *   1. 用 `ctx.subprocess` 启动/终止桌宠进程，并跟踪它的存活
 *   2. 用 `ctx.webServer` 暴露两条同源路由，供浏览器按钮读写状态
 *
 * 为什么走 HTTP 而不是 typert Remote：Remote 需要生成式 schema 与代码生成，
 * 而 webServer 路由是 DSH 自带的、插件可直接注册的载体，浏览器侧同源 fetch
 * 即可访问（kun-like 等插件也是这样给页面供素材的）。
 *
 * 配置（cordis.patch.yml 的 config 段）：
 *   petDir      桌宠程序目录
 *   pythonPath  pythonw.exe 的绝对路径
 *   scriptPath  可选，默认 <petDir>/dsh_pet.py
 */

/** Cordis 插件名。 */
export const name = 'dsh-pet-toggle'

/** 硬依赖：进程管理与 HTTP 载体。 */
export const inject = ['subprocess', 'webServer']

/**
 * 注册桌宠进程管理与控制路由。
 * @param ctx - Host 上下文。
 * @param config - 补丁行传入的配置。
 */
export function apply(ctx, config) {
  const cfg = config ?? {}
  const petDir = cfg.petDir ?? ''
  const pythonPath = cfg.pythonPath ?? 'pythonw.exe'
  const scriptPath = cfg.scriptPath ?? (petDir ? `${petDir}\\dsh_pet.py` : 'dsh_pet.py')

  /** 当前桌宠进程句柄；null 表示未运行。 */
  let handle = null
  /** 最近一次失败原因，供按钮提示。 */
  let lastError = null

  const status = () => ({ running: handle !== null, error: lastError })

  /** 启动桌宠；已在运行则原样返回。 */
  const start = () => {
    if (handle !== null) return status()
    try {
      handle = ctx.subprocess.spawn({
        argv: [pythonPath, scriptPath],
        cwd: petDir,
        // 桌宠自带 GUI，不需要任何标准流；inherit 会让它跟着宿主控制台
        stdio: { stdin: 'ignore', stdout: 'ignore', stderr: 'ignore' },
        graceMs: 4000,
      })
      const spawned = handle
      const forget = () => {
        if (handle === spawned) handle = null
      }
      // 用户从桌宠右键退出时，这里也要跟着复位
      spawned.done.then(forget, forget)
      lastError = null
      console.log('[dsh-pet] 已启动:', scriptPath)
    } catch (err) {
      lastError = String((err && err.message) || err)
      handle = null
      console.error('[dsh-pet] 启动失败:', lastError)
    }
    return status()
  }

  /** 终止桌宠。 */
  const stop = () => {
    if (handle === null) return status()
    const running = handle
    handle = null
    try {
      running.terminate()
      console.log('[dsh-pet] 已停止')
    } catch (err) {
      lastError = String((err && err.message) || err)
      console.error('[dsh-pet] 停止失败:', lastError)
    }
    return status()
  }

  const sendJson = (res, payload) => {
    const body = JSON.stringify(payload)
    res.writeHead(200, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': String(Buffer.byteLength(body)),
      'Cache-Control': 'no-store',
    })
    res.end(body)
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/dsh-pet/status',
    handler: (_req, res) => sendJson(res, status()),
  }))

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/dsh-pet/toggle',
    handler: (req, res) => {
      const on = /[?&]on=1/.test(req.url || '')
      sendJson(res, on ? start() : stop())
    },
  }))

  // 诊断：把客户端模块图（__DSH_BOOT__ 的来源）读出来，确认本包有没有进图。
  // 客户端按钮不显示时，第一个要排除的就是「包没被扫进启动图」。
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/dsh-pet/debug-boot',
    handler: (_req, res) => {
      const wp = ctx.get('webPlugins')
      const info = { hasService: wp !== undefined, hasGraph: false, keys: [], ids: [], petIds: [] }
      if (wp && typeof wp.graph === 'function') {
        try {
          const g = wp.graph()
          info.hasGraph = true
          info.keys = Object.keys(g || {})
          const ids = []
          const walk = (v, depth) => {
            if (depth > 5 || v === null || typeof v !== 'object') return
            if (Array.isArray(v)) {
              for (const x of v) walk(x, depth + 1)
              return
            }
            if (typeof v.id === 'string') ids.push(v.id)
            if (typeof v.name === 'string' && typeof v.kind === 'string') ids.push(v.name)
            for (const k of Object.keys(v)) walk(v[k], depth + 1)
          }
          walk(g, 0)
          info.ids = [...new Set(ids)]
          info.petIds = info.ids.filter((x) => /pet/i.test(x))
        } catch (err) {
          info.error = String((err && err.message) || err)
        }
      }
      sendJson(res, info)
    },
  }))

  // 插件被停用或更新时，把桌宠一并关掉 —— 不让它变成孤儿进程
  ctx.effect(() => () => {
    stop()
  })

  // 随 DSH 启动：这个插件是 profile 补丁里的一行，DSH 一起来 apply 就跑。
  // 桌宠自带单实例锁，所以重复启动不会叠出第二只。
  if (cfg.autostart !== false) start()

  console.log('[dsh-pet] 路由已注册: /dsh-pet/status, /dsh-pet/toggle')
}
