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
  // 页面每次渲染 index 都会走这个钩子。桌面版如果由 Electron 外壳自己组页面
  // （__DSH_TRANSPORT__ 是 shell 提供的载体），这里可能一次都不触发 ——
  // 那就说明客户端模块的交付方式和我们想的不一样。
  let indexInjectHits = 0
  const indexInjectUrls = []
  ctx.on('webserver/index-inject', (table) => {
    indexInjectHits += 1
    try {
      const rows = Array.isArray(table) ? table : []
      for (const row of rows) if (row && typeof row.src === 'string') indexInjectUrls.push(row.src)
    } catch { /* 只是诊断，别影响渲染 */ }
  })

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
      // 结构是 { rev, entries, batches } —— 没有 plugins 字段。
      // 之前误以为有，取了个空数组，白绕了一圈。
      out.graph = safe(() => {
        const g = cm.graph() ?? {}
        const entries = g.entries ?? []
        const batches = g.batches ?? []
        return {
          rev: g.rev ?? null,
          entryCount: entries.length,
          entryIds: entries.map((e) => e.id),
          hasMine: entries.some((e) => /pet/i.test(e.id ?? '')),
          batches: batches.map((b) => ({ phase: b.phase, url: b.url })),
        }
      })

      // ── 4b. 内部表：table 是 Map<包名, 记录>，sources 是 Map<sourceKey, 源> ──
      out.table = safe(() => {
        const t = cm.table
        if (t === undefined) return 'no table property'
        const keys = [...t.keys()]
        return { size: keys.length, keys, hasMine: keys.some((k) => /pet/i.test(k)) }
      })
      out.sources = safe(() => {
        const s = cm.sources
        if (s === undefined) return 'no sources property'
        const all = [...s.values()].map((v) => ({ pkg: v.packageName, loaderName: v.loaderName }))
        return { size: all.length, mine: all.filter((v) => /pet/i.test(v.pkg)) }
      })
      out.clientPath = safe(() => cm.clientPath('dsh-pet-toggle'))

      // ── 4d. 端到端：图里如果有我，就按它的 url 把 bundle 真取一遍 ──
      // 这一步证明「组合 -> 供给」整条链路通不通，而不只是元数据解析成功。
      out.bundleProbe = safe(() => {
        const g = cm.graph() ?? {}
        const mineEntry = (g.entries ?? []).find((e) => /pet/i.test(e.id ?? ''))
        if (mineEntry === undefined) return '图里没有我的条目'
        const res = cm.fetchBundle({ method: 'GET', url: mineEntry.url })
        return {
          entryId: mineEntry.id,
          rev: mineEntry.rev ?? null,
          url: mineEntry.url,
          status: res.status,
          contentType: res.headers?.get?.('content-type') ?? null,
          bytes: res.body === null ? 0 : undefined,
        }
      })

      // ── 4c. 桌面版到底走不走 index-inject？ ──
      // 如果 Electron 外壳自己组页面（__DSH_TRANSPORT__ 是 shell 提供的），
      // 这个钩子可能根本不触发 —— 那问题就不在组合，而在交付。
      out.indexInject = { fired: indexInjectHits, urls: indexInjectUrls.slice(0, 5) }

      // ── 5. fix=1：清掉负缓存并强制重新组合 ──
      if (/[?&]fix=1/.test(req.url || '')) {
        out.fix = safe(() => {
          const before = (cm.graph()?.entries ?? []).map((e) => e.id)
          const cleared = cm.pkgMeta?.clear !== undefined
          if (cleared) cm.pkgMeta.clear()
          const errs = []
          cm.dirty?.add?.(out.loaderName)             // 先标脏，flush 才会重走本条目
          cm.flush((err) => errs.push(String((err && err.message) || err)))
          const after = (cm.graph()?.entries ?? []).map((e) => e.id)
          return {
            clearedCache: cleared,
            flushErrors: errs,
            beforeCount: before.length,
            afterCount: after.length,
            added: after.filter((x) => !before.includes(x)),
            hasMine: after.some((e) => /pet/i.test(e)),
          }
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
