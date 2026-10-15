/**
 * DSH 桌宠开关 · Client 半
 * =========================
 * 在侧边栏底部（`sidebar.footer.action`，与「设置」同排）加一个按钮：
 * 点击打开/关闭桌面宠物，按钮颜色反映运行状态。
 *
 * 这是静态客户端模块：由 `window.__ModuleLoader__` 装载，用 CommonJS 风格的
 * `require` 取依赖，不能用 JSX。状态通过同源 fetch 走 Host 注册的两条路由。
 *
 * 为什么到处包 try/catch 并上报：插槽条目有独立的错误上报通道
 * （renderer 的 reportEntryError），组件渲染时抛错**不会**让应用崩溃，
 * 只会让那个条目静默消失 —— 表现就是「按钮没出现，控制台也看不出问题」。
 * 所以这里把每个阶段都报回 Host 的 /dsh-pet/client-log，出问题能直接看到。
 */
;(function () {
  /** 把诊断信息报回 Host。失败就算了，绝不能因为它影响按钮。 */
  function report(stage, detail) {
    try {
      fetch('/dsh-pet/client-log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage: stage, detail: detail === undefined ? null : String(detail) }),
      }).catch(function () {})
    } catch (e) { /* 忽略 */ }
  }

  // ── 阶段 1：模块被求值 ──
  report('module-eval', JSON.stringify({
    hasModuleLoader: typeof window.__ModuleLoader__ === 'object' && window.__ModuleLoader__ !== null,
    mode: window.__ModuleLoader__ && window.__ModuleLoader__.mode,
  }))

  if (typeof window.__ModuleLoader__ !== 'object' || window.__ModuleLoader__ === null) {
    report('fatal', 'window.__ModuleLoader__ 不存在')
    return
  }

  window.__ModuleLoader__.load({
    id: 'dsh-pet-toggle',
    factory: function (require) {
      var module = { exports: {} }
      var exports = module.exports

      report('factory-enter', null)

      var React
      try {
        React = require('react')
        report('require-react', typeof React + '/' + (React && typeof React.createElement))
      } catch (e) {
        report('fatal', 'require(react) 失败: ' + e)
        return module.exports
      }

      /** 爪印图标；颜色走 currentColor，跟随按钮状态。 */
      function paw() {
        return React.createElement(
          'svg',
          {
            width: 15, height: 15, viewBox: '0 0 16 16',
            fill: 'none', 'aria-hidden': 'true', style: { display: 'block' },
          },
          React.createElement('ellipse', { cx: 8, cy: 10.4, rx: 4.3, ry: 3.4, fill: 'currentColor' }),
          React.createElement('circle', { cx: 3.3, cy: 5.6, r: 1.9, fill: 'currentColor' }),
          React.createElement('circle', { cx: 12.7, cy: 5.6, r: 1.9, fill: 'currentColor' }),
          React.createElement('circle', { cx: 6.0, cy: 3.3, r: 1.7, fill: 'currentColor' }),
          React.createElement('circle', { cx: 10.0, cy: 3.3, r: 1.7, fill: 'currentColor' }),
        )
      }

      /**
       * 侧边栏底部的桌宠开关。
       * @param props - 插槽 owner props（`wide` 表示侧边栏是否为展开态）。
       */
      function PetToggle(props) {
        var runningState = React.useState(false)
        var running = runningState[0], setRunning = runningState[1]
        var busyState = React.useState(false)
        var busy = busyState[0], setBusy = busyState[1]
        var hoverState = React.useState(false)
        var hover = hoverState[0], setHover = hoverState[1]
        var errorState = React.useState('')
        var error = errorState[0], setError = errorState[1]

        // 用原生 setInterval 而不是 ctx.interval —— 少依赖一个服务，
        // 客户端插件的 ctx 上有没有 timer 不确定，没必要为它冒风险。
        React.useEffect(function () {
          var alive = true
          function tick() {
            fetch('/dsh-pet/status', { cache: 'no-store', credentials: 'include' })
              .then(function (r) { return r.ok ? r.json() : null })
              .then(function (d) {
                if (alive && d) {
                  setRunning(!!d.running)
                  setError(d.error || '')
                }
              })
              .catch(function () {})
          }
          tick()
          var id = setInterval(tick, 3000)
          return function () {
            alive = false
            clearInterval(id)
          }
        }, [])

        /**
         * 点击开关。
         *
         * 关键：**不能拿本组件里的 `running` 决定开还是关**。那个值来自 3 秒一次的
         * 轮询，可能是陈旧的 —— 一旦它还停在 true，第二次点击会再发一次 on=0，
         * 而 Host 的 stop() 在 handle 已为 null 时是空操作，表现就是「怎么点都没反应」。
         * 所以先取一次最新状态，再据此决定动作。
         */
        function toggle() {
          if (busy) return
          setBusy(true)
          report('click', JSON.stringify({ localRunning: running, busy: busy }))

          fetch('/dsh-pet/status', { cache: 'no-store', credentials: 'include' })
            .then(function (r) { return r.ok ? r.json() : null })
            .then(function (fresh) {
              // 取不到就用本地值兜底，总比不发好
              var nowRunning = fresh ? !!fresh.running : running
              var on = nowRunning ? '0' : '1'
              report('toggle-decided', JSON.stringify({
                fresh: fresh ? fresh.running : null, local: running, send: 'on=' + on,
              }))
              return fetch('/dsh-pet/toggle?on=' + on, {
                method: 'POST',
                credentials: 'include',
              })
            })
            .then(function (r) { return r.ok ? r.json() : null })
            .then(function (d) {
              report('toggle-response', JSON.stringify(d))
              if (d) {
                setRunning(!!d.running)
                setError(d.error || '')
              }
            })
            .catch(function (e) { setError(String(e)) })
            .then(function () { setBusy(false) })
        }

        var color = error
          ? 'var(--dsw-alias-state-error-primary)'
          : running
            ? 'var(--dsw-alias-brand-primary)'
            : 'var(--dsw-alias-label-secondary)'

        return React.createElement(
          'button',
          {
            type: 'button',
            onClick: toggle,
            onMouseEnter: function () { setHover(true) },
            onMouseLeave: function () { setHover(false) },
            disabled: busy,
            title: error ? '桌宠：' + error : running ? '关闭桌宠' : '打开桌宠',
            'aria-label': running ? '关闭桌宠' : '打开桌宠',
            'aria-pressed': running,
            'data-dsh-pet-toggle': '1',
            style: {
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
              height: '28px',
              width: props && props.wide ? 'auto' : '28px',
              padding: props && props.wide ? '0 8px' : '0',
              border: '1px solid transparent',
              borderRadius: '6px',
              background: hover ? 'var(--dsw-alias-bg-layer-2)' : 'transparent',
              color: color,
              cursor: busy ? 'default' : 'pointer',
              opacity: busy ? 0.55 : 1,
              font: 'inherit',
              fontSize: '12px',
              lineHeight: 1,
              transition: 'background .12s, color .12s',
            },
          },
          paw(),
          props && props.wide ? React.createElement('span', null, '桌宠') : null,
        )
      }

      /** 只依赖插槽注册表（不再依赖 timer）。 */
      var inject = ['slots']

      /**
       * 把开关注册进侧边栏底部的动作列表。
       * @param ctx - Client 根上下文。
       */
      function apply(ctx) {
        report('apply-enter', JSON.stringify({
          hasSlots: !!(ctx && ctx.slots),
          slotsType: ctx && ctx.slots ? typeof ctx.slots : 'none',
          hasInject: ctx && ctx.slots ? typeof ctx.slots.inject : 'none',
          hasRegister: ctx && ctx.slots ? typeof ctx.slots.register : 'none',
        }))

        try {
          ctx.slots.inject('sidebar.footer.action', function () {
            report('inject-callback-fired', null)
            var dispose = ctx.slots.register(
              { name: 'sidebar.footer.action', id: 'dsh-pet-toggle', order: 20, label: '桌宠' },
              PetToggle,
            )
            report('registered', typeof dispose)
            return dispose
          })
          report('inject-called', 'ok')
        } catch (e) {
          report('fatal', 'slots.inject 抛错: ' + (e && e.stack ? e.stack : e))
        }
      }

      exports.apply = apply
      exports.inject = inject
      report('factory-exit', 'ok')
      return module.exports
    },
  })

  report('load-called', 'ok')
})()
