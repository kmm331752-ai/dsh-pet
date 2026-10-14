/**
 * DSH 桌宠开关 · Client 半
 * =========================
 * 在侧边栏底部（`sidebar.footer.action`，与「设置」同排）加一个按钮：
 * 点击打开/关闭桌面宠物，按钮颜色反映运行状态。
 *
 * 这是静态客户端模块：由 `window.__ModuleLoader__` 装载，用 CommonJS 风格的
 * `require` 取依赖，不能用 JSX。状态通过同源 fetch 走 Host 注册的两条路由。
 */
window.__ModuleLoader__.load({
  id: 'dsh-pet-toggle',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    const React = require('react')

    /** 爪印图标；颜色走 currentColor，跟随按钮状态。 */
    const paw = () => React.createElement(
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

    /**
     * 侧边栏底部的桌宠开关。
     * @param props - 插槽 owner props（`wide` 表示侧边栏是否为展开态）。
     */
    function PetToggle(props) {
      const [running, setRunning] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const [hover, setHover] = React.useState(false)
      const [error, setError] = React.useState('')

      // 轮询 Host 状态。用 ctx.interval 而不是原生 setInterval ——
      // 它的 disposer 会被 useEffect 在卸载时调用，插件停用后不留定时器。
      React.useEffect(() => {
        let alive = true
        const tick = () => {
          fetch('/dsh-pet/status', { cache: 'no-store', credentials: 'include' })
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
              if (alive && d) {
                setRunning(!!d.running)
                setError(d.error || '')
              }
            })
            .catch(() => {})
        }
        tick()
        return ctx.interval(tick, 3000)
      }, [])

      const toggle = () => {
        if (busy) return
        setBusy(true)
        fetch('/dsh-pet/toggle?on=' + (running ? '0' : '1'), {
          method: 'POST',
          credentials: 'include',
        })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (d) {
              setRunning(!!d.running)
              setError(d.error || '')
            }
          })
          .catch((e) => setError(String(e)))
          .then(() => setBusy(false))
      }

      const color = error
        ? 'var(--dsw-alias-state-error-primary)'
        : running
          ? 'var(--dsw-alias-brand-primary)'
          : 'var(--dsw-alias-label-secondary)'

      return React.createElement(
        'button',
        {
          type: 'button',
          onClick: toggle,
          onMouseEnter: () => setHover(true),
          onMouseLeave: () => setHover(false),
          disabled: busy,
          title: error ? '桌宠：' + error : running ? '关闭桌宠' : '打开桌宠',
          'aria-label': running ? '关闭桌宠' : '打开桌宠',
          'aria-pressed': running,
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '6px',
            height: '28px',
            width: props.wide ? 'auto' : '28px',
            padding: props.wide ? '0 8px' : '0',
            border: '1px solid transparent',
            borderRadius: '6px',
            background: hover ? 'var(--dsw-alias-bg-layer-2)' : 'transparent',
            color,
            cursor: busy ? 'default' : 'pointer',
            opacity: busy ? 0.55 : 1,
            font: 'inherit',
            fontSize: '12px',
            lineHeight: 1,
            transition: 'background .12s, color .12s',
          },
        },
        paw(),
        props.wide ? React.createElement('span', null, '桌宠') : null,
      )
    }

    /** 仅依赖插槽注册表。 */
    const inject = ['slots', 'timer']

    /**
     * 把开关注册进侧边栏底部的动作列表。
     *
     * 用生成器返回注册项 —— 这是官方客户端插件（如 brand-official）的写法，
     * 注册的归属与释放由插槽系统接管。
     * @param ctx - Client 根上下文。
     */
    function apply(ctx) {
      ctx.slots.inject('sidebar.footer.action', function* () {
        yield ctx.slots.register(
          { name: 'sidebar.footer.action', id: 'dsh-pet-toggle', order: 20, label: '桌宠' },
          PetToggle,
        )
      })
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
