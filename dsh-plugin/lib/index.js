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
  /**
   * 正在退出中的上一只进程。
   *
   * 桌宠用「绑端口 51937」做单实例锁，而进程收到终止信号后端口大约还要
   * **2 秒**才真正释放。这期间 spawn 起来的新实例会 bind 失败并立刻退出 ——
   * 表现就是连点两下之后「怎么点都没反应」。所以 stop 记下 done，
   * start 必须先等它落地。
   */
  let stopping = null

  const status = () => ({ running: handle !== null, error: lastError })

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

  /** 起一只，不做等待。返回是否成功 spawn（不代表它活下来了）。 */
  const spawnOnce = () => {
    try {
      const spawned = ctx.subprocess.spawn({
        argv: [pythonPath, scriptPath],
        cwd: petDir,
        // 桌宠自带 GUI，不需要任何标准流；inherit 会让它跟着宿主控制台
        stdio: { stdin: 'ignore', stdout: 'ignore', stderr: 'ignore' },
        graceMs: 4000,
      })
      handle = spawned
      const startedAt = Date.now()
      const forget = () => {
        if (handle !== spawned) return
        handle = null
        // 起来不到 3 秒就退出，基本就是撞上单实例锁了（桌宠会非零退出）
        if (Date.now() - startedAt < 3000 && lastError === null) {
          lastError = '桌宠启动后立即退出：单实例锁端口仍被上一只占用'
        }
      }
      // 用户从桌宠右键退出时，这里也要跟着复位
      spawned.done.then(forget, forget)
      return true
    } catch (err) {
      lastError = String((err && err.message) || err)
      handle = null
      console.error('[dsh-pet] spawn 失败:', lastError)
      return false
    }
  }

  /**
   * 启动桌宠；已在运行则原样返回。
   *
   * 会做两件事，都是为了绕开「单实例锁端口要 2 秒才释放」这个坑：
   *   1. 先等上一只真正退出（stop 时记下的 done）
   *   2. 起来之后确认它还活着；被锁挡掉就等一会儿重试
   *
   * 第 2 条是必须的：DSH 重启时，上一个 DSH 启动的桌宠可能还在退出中，
   * 新进程 bind 失败会立刻退出 —— 那时候没有任何"上一只"可以等。
   * 实测就是这么翻车的：重启 DSH 后桌宠没了，按钮变红。
   */
  const start = async (retries = 3) => {
    if (handle !== null) return status()
    if (stopping !== null) {
      try {
        await stopping
      } catch { /* 上一只怎么退的不重要，只要它退干净了 */ }
      stopping = null
      // done 落地就代表进程没了，端口理论上已释放；留一点余量更稳
      await sleep(300)
    }
    if (handle !== null) return status()          // 等待期间可能已经被别人拉起

    for (let attempt = 0; attempt <= retries; attempt += 1) {
      lastError = null
      if (!spawnOnce()) break                     // spawn 本身就抛错了，重试没意义
      await sleep(1200)
      if (handle !== null) {                      // 活过 1.2 秒 —— 起来了
        console.log('[dsh-pet] 已启动:', scriptPath, attempt ? `(第 ${attempt + 1} 次尝试)` : '')
        return status()
      }
      if (attempt < retries) {
        console.log(`[dsh-pet] 第 ${attempt + 1} 次启动被单实例锁挡掉，等 2 秒重试`)
        await sleep(2000)
      }
    }
    return status()
  }

  /** 终止桌宠。立刻返回，但把退出 promise 留给下一次 start 等。 */
  const stop = () => {
    if (handle === null) return status()
    const running = handle
    handle = null
    try {
      running.terminate()
      stopping = running.done.catch(() => {})
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
      // start() 是异步的（要先等上一只退出），stop() 是同步的
      const result = on ? start() : stop()
      if (result !== null && typeof result.then === 'function') {
        result.then(
          (snap) => sendJson(res, snap),
          (err) => sendJson(res, { running: false, error: String((err && err.message) || err) }),
        )
        return
      }
      sendJson(res, result)
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

  // 客户端半的启动上报。浏览器里出问题（组件抛错、服务缺失）不会让应用崩溃，
  // 插槽条目只是静默消失 —— 光看界面和宿主日志都发现不了。
  // 所以 client.js 把每个阶段 POST 到这里，GET 回读。
  const clientLog = []

  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/dsh-pet/client-log',
    handler: (req, res) => {
      if (req.method === 'POST') {
        const chunks = []
        req.on('data', (c) => chunks.push(c))
        req.on('end', () => {
          try {
            const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
            clientLog.push({ t: new Date().toISOString().slice(11, 19), ...body })
            if (clientLog.length > 60) clientLog.shift()
          } catch { /* 报文坏了就丢 */ }
          sendJson(res, { ok: true, count: clientLog.length })
        })
        return
      }
      sendJson(res, { count: clientLog.length, entries: clientLog })
    },
  }))

  // 只读探测：宿主到底暴露了哪些服务？
  //
  // 起因是想让桌宠从「显示器」变成「遥控器」—— 现在它只能看 DSH 的状态，
  // 唯一能做的动作是复制会话 ID / 打开日志目录 / 把窗口切前台。
  //
  // 而这个插件的 inject 只有 ['subprocess', 'webServer']，碰不到会话管理。
  // 所以第一步得先知道宿主**有没有**会话相关的服务可注入 —— 有，双向才谈得上。
  //
  // 拿不到服务清单（Cordis 没有公开的枚举接口），所以换个路子：
  // 从 loader 的条目树里挖。条目是插件包，包会声明自己 inject 什么 ——
  // 把整棵树里出现过的服务名收集起来，就知道这个宿主大概有哪些能力。
  //
  // 纯只读，不改任何状态。查完可以删掉这条路由。
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: '/dsh-pet/probe-services',
    handler: (_req, res) => {
      const safe = (fn, fallback) => { try { return fn() } catch { return fallback } }
      const out = { inject: inject, sources: {}, names: [], byPackage: {}, error: null }

      // 1) 自己注入的
      out.sources.own = [...inject]

      // 2) loader 条目树里，每个包声明的 inject
      const entries = safe(() => [...ctx.loader.entries()], []) || []
      out.entryCount = entries.length
      const names = new Set()
      for (const e of entries) {
        const pkg = safe(() => e && e.options && e.options.name, null) || safe(() => e && e.name, null)
        const inj = safe(() => e && e.options && e.options.inject, null)
        if (Array.isArray(inj) && inj.length) {
          const list = inj.filter((x) => typeof x === 'string')
          for (const n of list) names.add(n)
          if (pkg) out.byPackage[pkg] = list
        }
      }

      // 3) 兜底：直接从 ctx 上能看到哪些键（Cordis 的服务挂在 ctx 上）
      const ctxKeys = safe(() => {
        const ks = []
        for (const k in ctx) {
          if (typeof k === 'string' && !k.startsWith('_')) ks.push(k)
        }
        return ks
      }, [])
      out.ctxKeys = ctxKeys.slice(0, 200)
      for (const k of ctxKeys) names.add(k)

      // 4) 关键：Cordis 的服务注册表。
      //    第一版只看了 loader 条目和 ctx 的**可枚举键**，结果只有 8 个内部
      //    名字（fiber / reflect / registry …）—— 因为服务不是可枚举属性，
      //    是注册在 registry 里的。这里把注册表挖开。
      const reg = safe(() => ctx.registry, null)
      out.registryServices = null
      if (reg) {
        out.registryKeys = safe(() => Object.keys(reg).slice(0, 60), [])
        const dump = safe(() => {
          if (typeof reg.list === 'function') return reg.list()
          if (reg instanceof Map) return [...reg.keys()]
          if (reg.store instanceof Map) return [...reg.store.keys()]
          if (reg.services instanceof Map) return [...reg.services.keys()]
          if (reg._services instanceof Map) return [...reg._services.keys()]
          if (reg.entries instanceof Map) return [...reg.entries.keys()]
          return null
        }, null)
        if (Array.isArray(dump)) {
          out.registryServices = dump
            .map((x) => (typeof x === 'string' ? x : (x && (x.name || x.id)) || ''))
            .filter((x) => typeof x === 'string' && x)
            .slice(0, 400)
          for (const n of out.registryServices) names.add(n)
        } else {
          // 形状不认识就把属性名报回来，下次照着改
          out.registryShape = safe(
            () => Object.getOwnPropertyNames(reg).slice(0, 40), [])
        }
      }

      out.names = [...names].sort()
      // 5) 挑出和会话/任务相关的，这才是双向的关键
      out.sessionLike = out.names.filter((n) => /session|task|turn|message|agent|conversation|history/i.test(n))
      sendJson(res, out)
    },
  }))

  // 插件被停用或更新时，把桌宠一并关掉 —— 不让它变成孤儿进程
  ctx.effect(() => () => {
    stop()
  })

  // 随 DSH 启动：这个插件是 profile 补丁里的一行，DSH 一起来 apply 就跑。
  // 桌宠自带单实例锁，所以重复启动不会叠出第二只。
  if (cfg.autostart !== false) {
    start().catch((err) => console.error('[dsh-pet] 自动启动失败:', err))
  }

  console.log('[dsh-pet] 路由已注册: /dsh-pet/status, /dsh-pet/toggle, '
    + '/dsh-pet/debug-boot, /dsh-pet/client-log, /dsh-pet/probe-services')
}
