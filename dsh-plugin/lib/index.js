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

import { createRequire } from 'node:module'

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

  // 诊断：客户端按钮不显示时，第一个要排除的是「包没被扫进启动图」。
  //
  // 前端的插件启动**不用 Host 的条目树**，而是遍历 __DSH_BOOT__.plugins：
  //     const s = this.manifest.plugins.map(a => a.id)
  //     await Promise.all(s.map(async a => i.create({ name: a })))
  // 所以 manifest 里没有的包根本不会被创建成客户端条目 —— 按钮自然不出现。
  //
  // manifest 由 dsh-client-modules 的 ClientModuleRegistry 组合而来，而它
  // 在 resolveMeta() 定位不到包时会**静默返回 null**（还把这个失败结果缓存起来），
  // 宿主日志里什么都看不到。这条路由就是把这层黑盒打开：
  //   GET /dsh-pet/debug-boot          只诊断
  //   GET /dsh-pet/debug-boot?fix=1    诊断 + 清掉负缓存并重新组合
  const findClientModules = () => {
    // 服务名是 clientModules（dsh-client-modules 里 super(ctx, "clientModules")）。
    // 它可能挂在 root 上而不是本插件的作用域里，所以几条路都试一遍。
    const tries = [
      () => ctx.get('clientModules'),
      () => ctx.root?.get?.('clientModules'),
      () => ctx.reflect?.get?.('clientModules'),
      () => ctx.root?.reflect?.get?.('clientModules'),
    ]
    for (const t of tries) {
      try {
        const v = t()
        if (v !== undefined && v !== null) return v
      } catch { /* 换下一条路 */ }
    }
    return undefined
  }

  const safe = (fn) => {
    try { return { ok: true, value: fn() } } catch (err) { return { ok: false, error: String((err && err.message) || err) } }
  }

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/dsh-pet/debug-boot',
    handler: (req, res) => {
      const out = {}
      const cm = findClientModules()
      out.hasService = cm !== undefined
      if (cm === undefined) {
        out.hint = '拿不到 clientModules 服务，下面列出本插件能看到的服务名'
        out.visibleServices = safe(() => Object.keys(ctx.root ?? {}).filter((k) => !k.startsWith('_')).slice(0, 120)).value ?? []
        return sendJson(res, out)
      }

      // ── 1. 找我的条目，以及它所属树的 baseUrl ──
      const entries = safe(() => [...ctx.loader.entries()]).value ?? []
      out.entryCount = entries.length
      const mine = entries.filter((e) => /pet/i.test(e.options?.name ?? ''))
      out.mine = mine.map((e) => ({
        name: e.options?.name,
        id: e.options?.id,
        disabled: e.disabled === true,
        fiberState: e.fiber?.state ?? null,
        baseUrl: e.parent?.tree?.ctx?.baseUrl ?? null,
      }))

      const e0 = mine[0]
      if (e0 !== undefined) {
        const loaderName = e0.options.name
        const baseUrl = e0.parent?.tree?.ctx?.baseUrl
        out.loaderName = loaderName
        out.baseUrl = baseUrl

        // ── 2. 逐步复现 ClientModuleRegistry 的定位过程 ──
        out.locatePkgJson = safe(() => cm.locatePkgJson(loaderName, baseUrl))
        out.sourceKey = safe(() => cm.sourceKey(loaderName, baseUrl)).value ?? null
        out.pkgMetaCached = safe(() => {
          const m = cm.pkgMeta
          if (m === undefined) return 'no pkgMeta property'
          const hit = m.get(out.sourceKey)
          return hit === undefined ? 'not cached' : hit
        }).value
        out.resolveSource = safe(() => cm.resolveSource(e0))
        out.resolveMeta = safe(() => cm.resolveMeta(loaderName, baseUrl))

        // ── 3. loader 的 ESM 解析接口（locatePkgJson 走的就是这条） ──
        const internal = ctx.loader.internal
        out.loaderInternal = {
          present: internal !== undefined,
          version: internal?.version ?? null,
          hasResolveSync: typeof internal?.resolveSync === 'function',
        }
        if (typeof internal?.resolveSync === 'function' && baseUrl !== undefined) {
          out.manualResolve = safe(() => (internal.version === 'v2'
            ? internal.resolveSync(baseUrl, { specifier: loaderName, attributes: {} })
            : internal.resolveSync(loaderName, baseUrl, {})))
        }
        // 退路：Node 自己的解析（internal 不可用时 locatePkgJson 用的就是它）
        out.requireResolve = safe(() => createRequire(baseUrl).resolve(`${loaderName}/package.json`))
      }

      // ── 4. 组合出来的图（__DSH_BOOT__ 的来源） ──
      out.graph = safe(() => {
        const g = cm.graph()
        return { keys: Object.keys(g ?? {}), plugins: (g?.plugins ?? []).map((p) => p.id ?? p) }
      })

      // ── 5. fix=1：清掉负缓存并强制重新组合 ──
      if (/[?&]fix=1/.test(req.url || '')) {
        const before = JSON.stringify(out.graph?.value?.plugins ?? [])
        out.fix = safe(() => {
          const cleared = cm.pkgMeta?.clear !== undefined
          if (cleared) cm.pkgMeta.clear()
          const errs = []
          cm.flush((err) => errs.push(String((err && err.message) || err)))
          const after = (cm.graph()?.plugins ?? []).map((p) => p.id ?? p)
          return { clearedCache: cleared, flushErrors: errs, after, changed: JSON.stringify(after) !== before }
        })
      }

      sendJson(res, out)
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
