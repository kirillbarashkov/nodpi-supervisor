/**
 * NoDPI supervisor — the "Network" page of the Hermes desktop app.
 *
 * The page should answer three questions at a glance: is the stack alive, which
 * mode is active right now, and what do I press to change it.
 *
 * Styling deliberately avoids theme variables such as --accent / --border: they
 * may be undefined in a given theme, and an undefined variable invalidates the
 * whole CSS declaration — cards and buttons then render as invisible text
 * (that is exactly what happened on the first attempt). Surfaces and lines here
 * are translucent neutrals that read on both dark and light themes, and semantic
 * colours always carry an explicit fallback.
 *
 * Backend: integrations/hermes/backend/plugin_api.py (ctx.rest('/overview')).
 * Plain ESM, no build step: the UI is built with jsx() calls, not JSX syntax.
 */

import { PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, host } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'nodpi-supervisor'
const PATH = '/nodpi'

const C = {
  fg: 'var(--foreground, #e6e6e6)',
  muted: 'var(--muted-foreground, rgba(220,220,220,0.70))',
  line: 'rgba(127,127,127,0.38)',
  lineSoft: 'rgba(127,127,127,0.22)',
  lineStrong: 'rgba(127,127,127,0.60)',
  surface: 'rgba(127,127,127,0.10)',
  surface2: 'rgba(127,127,127,0.16)',
  accent: 'var(--ui-accent, #4f9cf9)',
  accentSoft: 'rgba(79,156,249,0.16)',
  ok: '#22c55e',
  okSoft: 'rgba(34,197,94,0.14)',
  warn: '#f59e0b',
  warnSoft: 'rgba(245,158,11,0.15)',
  bad: '#ef4444',
  badSoft: 'rgba(239,68,68,0.14)',
  mono: 'ui-monospace, SFMono-Regular, Consolas, monospace'
}

const HOVER_CSS = `
.hn-btn { transition: filter .12s ease, background .12s ease, border-color .12s ease; }
.hn-btn:hover:not(:disabled) { filter: brightness(1.18); }
.hn-btn:active:not(:disabled) { transform: translateY(1px); }
.hn-mode:hover:not(:disabled) { border-color: #4f9cf9 !important; }
.hn-mode:focus-visible, .hn-btn:focus-visible { outline: 2px solid #4f9cf9; outline-offset: 2px; }
`

function StyleTag() {
  return jsx('style', { dangerouslySetInnerHTML: { __html: HOVER_CSS } })
}

const TITLES = {
  switch: 'logs/switch.log — connections, mode changes, supervision events',
  access: 'logs/nodpi-access.log — one line per connection: time · method · host · traffic',
  error: 'logs/nodpi-error.log — NoDPI failures and tracebacks'
}

const META = {
  running: { label: 'running', color: C.ok, bg: C.okSoft },
  external: { label: 'external', color: C.warn, bg: C.warnSoft },
  starting: { label: 'starting', color: C.warn, bg: C.warnSoft },
  restarting: { label: 'restarting', color: C.warn, bg: C.warnSoft },
  down: { label: 'down', color: C.bad, bg: C.badSoft },
  stopped: { label: 'stopped', color: C.muted, bg: C.surface },
  failed: { label: 'failed', color: C.bad, bg: C.badSoft },
  unknown: { label: 'unknown', color: C.muted, bg: C.surface }
}

function meta(state) {
  return META[state] || META.unknown
}

function fmtUptime(s) {
  if (!s || s < 0) return '—'
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm'
  return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm'
}

/* ---------------------------------------------------------------- primitives */

function Btn({ children, onClick, disabled, kind, title }) {
  const base = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    borderRadius: 8,
    padding: '8px 15px',
    fontSize: 13,
    fontWeight: 600,
    font: 'inherit',
    fontFamily: 'inherit',
    cursor: disabled ? 'default' : 'pointer',
    opacity: disabled ? 0.42 : 1,
    whiteSpace: 'nowrap',
    lineHeight: 1.2
  }

  const skins = {
    primary: {
      background: C.accent,
      border: '1px solid ' + C.accent,
      color: '#fff'
    },
    danger: {
      background: C.badSoft,
      border: '1px solid ' + C.bad,
      color: '#ff8080'
    },
    ghost: {
      background: 'transparent',
      border: '1px solid ' + C.line,
      color: C.fg
    },
    subtle: {
      background: C.surface,
      border: '1px solid ' + C.lineSoft,
      color: C.fg
    },
    chip: {
      background: C.surface2,
      border: '1px solid ' + C.line,
      color: C.fg,
      padding: '5px 11px',
      fontSize: 12,
      fontWeight: 600
    }
  }

  const skin = skins[kind || 'ghost']

  return jsx('button', {
    className: 'hn-btn',
    onClick,
    disabled,
    title,
    style: { ...base, ...skin },
    children
  })
}

function Chip({ children, color, bg, border }) {
  return jsx('span', {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 5,
      padding: '2px 9px',
      borderRadius: 999,
      fontSize: 11.5,
      fontWeight: 700,
      letterSpacing: '0.02em',
      color: color || C.muted,
      background: bg || 'transparent',
      border: '1px solid ' + (border || color || C.line),
      whiteSpace: 'nowrap'
    },
    children
  })
}

function Status({ state }) {
  const m = meta(state)
  return jsxs(Chip, {
    color: m.color,
    bg: m.bg,
    border: m.color,
    children: [
      jsx('span', {
        style: {
          width: 7, height: 7, borderRadius: '50%',
          background: m.color, display: 'inline-block'
        }
      }),
      m.label
    ]
  })
}

function Section({ label, hint, right, children, style }) {
  return jsxs('div', {
    style: {
      border: '1px solid ' + C.line,
      borderRadius: 12,
      background: C.surface,
      padding: '13px 15px 15px',
      ...style
    },
    children: [
      jsxs('div', {
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginBottom: 12,
          flexWrap: 'wrap'
        },
        children: [
          jsx('span', {
            style: {
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: '0.08em',
              textTransform: 'uppercase',
              color: C.muted
            },
            children: label
          }),
          hint
            ? jsx('span', { style: { fontSize: 11.5, color: C.muted }, children: hint })
            : null,
          jsx('span', { style: { flex: 1 } }),
          right || null
        ]
      }),
      children
    ]
  })
}

function ModeCard({ title, desc, active, disabled, onClick }) {
  return jsxs('button', {
    className: 'hn-btn hn-mode',
    onClick,
    disabled,
    'aria-pressed': active,
    style: {
      flex: '1 1 230px',
      minWidth: 0,
      textAlign: 'left',
      border: active ? '2px solid ' + C.accent : '1px solid ' + C.line,
      background: active ? C.accentSoft : 'rgba(127,127,127,0.04)',
      borderRadius: 10,
      padding: active ? '12px 14px' : '13px 15px',
      cursor: disabled ? 'default' : 'pointer',
      opacity: disabled ? 0.5 : 1,
      font: 'inherit',
      fontFamily: 'inherit',
      color: C.fg
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 9, marginBottom: 5 },
        children: [
          jsx('span', {
            style: {
              width: 17, height: 17, borderRadius: '50%',
              border: active ? '5px solid ' + C.accent : '2px solid ' + C.lineStrong,
              display: 'inline-block',
              flex: '0 0 auto',
              boxSizing: 'border-box',
              background: 'transparent'
            }
          }),
          jsx('span', { style: { fontWeight: 700, fontSize: 14 }, children: title }),
          active
            ? jsx(Chip, {
                color: C.accent,
                bg: C.accentSoft,
                border: C.accent,
                children: 'SELECTED'
              })
            : null
        ]
      }),
      jsx('div', {
        style: { color: C.muted, fontSize: 12, lineHeight: 1.45, paddingLeft: 26 },
        children: desc
      })
    ]
  })
}

/* ---------------------------------------------------------------- page */

export function NetworkPage({ ctx }) {
  const [ov, setOv] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)
  const [probe, setProbe] = useState(null)
  const [logTab, setLogTab] = useState('access')
  const [seen, setSeen] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await ctx.rest('/overview')
      setOv(res)
      setSeen(new Date().toLocaleTimeString('ru-RU'))
      setError(res && res.error ? res.error : null)
    } catch (e) {
      // An empty state must never be painted as "stack stopped": if the first
      // load fails there is no answer yet, and the page says so instead.
      setError(String(e))
    }
  }, [ctx])

  useEffect(() => {
    load()
    const timer = setInterval(load, 3000)
    return () => clearInterval(timer)
  }, [load])

  // First paint often races the backend right after a plugin reload; retry fast.
  useEffect(() => {
    if (ov || !error) return undefined
    const retry = setTimeout(load, 1200)
    return () => clearTimeout(retry)
  }, [ov, error, load])

  const act = useCallback(async (key, path, method) => {
    setBusy(key)
    try {
      const res = await ctx.rest(path, { method: method || 'POST' })
      if (res && res.overview) setOv(res.overview)
      setError(res && res.ok === false ? res.error || 'action failed' : null)
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy('')
    }
  }, [ctx, load])

  async function runProbe() {
    setBusy('test')
    setProbe(null)
    try {
      setProbe(await ctx.rest('/test?host=opencode.ai'))
    } catch (e) {
      setProbe({ ok: false, error: String(e) })
    } finally {
      setBusy('')
    }
  }

  const up = !!(ov && ov.switch_up)
  const mode = (ov && ov.mode) || '—'
  const nodpi = mode === 'nodpi'
  const comps = (ov && ov.components) || []
  const logs = (ov && ov.logs) || {}
  const byMode = (ov && ov.by_mode) || {}
  const noop = !!busy

  /* --- no answer yet: never invent a state --- */
  if (!ov) {
    return jsxs('div', {
      style: { padding: '18px 22px 26px', maxWidth: 900, color: C.fg },
      children: [
        jsx(StyleTag, {}),
        jsx('h2', { style: { margin: '0 0 12px', fontSize: 19 }, children: 'Network' }),
        jsxs('div', {
          style: {
            border: '1px solid ' + C.line,
            borderRadius: 12,
            background: C.surface,
            padding: '16px 18px'
          },
          children: [
            jsx('div', {
              style: { fontWeight: 600, fontSize: 13.5, marginBottom: 6 },
              children: error
                ? 'Could not read the stack state'
                : 'Loading state…'
            }),
            jsx('div', {
              style: { color: C.muted, fontSize: 12.5, whiteSpace: 'pre-wrap' },
              children: error || 'request to the plugin backend'
            }),
            error
              ? jsx('div', {
                  style: { marginTop: 12 },
                  children: jsx(Btn, { kind: 'primary', onClick: load, children: 'Retry' })
                })
              : null
          ]
        })
      ]
    })
  }

  /* --- header: what is happening right now --- */
  const header = jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
    children: [
      jsx('h2', { style: { margin: 0, fontSize: 19 }, children: 'Network' }),
      up
        ? jsx(Status, { state: 'running' })
        : jsx(Status, { state: 'down' }),
      jsxs('span', {
        style: { fontSize: 12, color: C.muted },
        children: ['connections ', String((ov && ov.connections) || 0)]
      }),
      jsx('span', { style: { flex: 1 } }),
      jsx('span', {
        style: { fontSize: 11.5, color: C.muted, fontFamily: C.mono },
        children: seen ? 'updated ' + seen : ''
      })
    ]
  })

  /* --- the main answer: where traffic goes --- */
  const nowLine = jsxs('div', {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      marginTop: 12,
      padding: '10px 13px',
      borderRadius: 10,
      border: '1px solid ' + (up ? C.accent : C.line),
      background: up ? C.accentSoft : C.surface,
      fontSize: 13.5
    },
    children: [
      jsx('span', { style: { color: C.muted }, children: 'Traffic is currently going' }),
      jsx('span', {
        style: { fontWeight: 700, color: up ? C.accent : C.muted },
        children: !up ? 'nowhere — the stack is stopped' : nodpi ? 'through NoDPI' : 'directly'
      }),
      jsxs('span', {
        style: { color: C.muted, fontFamily: C.mono, fontSize: 11.5, marginLeft: 'auto' },
        children: [((ov && ov.proxy) || '127.0.0.1:8881')]
      })
    ]
  })

  /* --- mode --- */
  const modeSection = jsx(Section, {
    label: 'mode',
    hint: 'click a card to switch',
    style: { marginTop: 12 },
    children: jsxs('div', {
      style: { display: 'flex', gap: 10, flexWrap: 'wrap' },
      children: [
        jsx(ModeCard, {
          title: 'Direct',
          desc: 'No bypass: fast, but the ISP may reset TLS 1.3.',
          active: mode === 'direct',
          disabled: !up || noop,
          onClick: () => act('mode', '/mode/direct')
        }),
        jsx(ModeCard, {
          title: 'Through NoDPI',
          desc: 'Client Hello fragmentation — gets past DPI on PQ key shares.',
          active: nodpi,
          disabled: !up || noop,
          onClick: () => act('mode', '/mode/nodpi')
        })
      ]
    })
  })

  /* --- actions --- */
  const actions = jsxs('div', {
    style: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginTop: 10 },
    children: [
      jsx(Btn, {
        kind: up ? 'ghost' : 'primary',
        onClick: () => act('start', '/stack/start'),
        disabled: noop,
        children: busy === 'start' ? 'Starting…' : 'Start stack'
      }),
      jsx(Btn, {
        kind: 'ghost',
        onClick: () => act('restart', '/stack/restart'),
        disabled: noop,
        children: busy === 'restart' ? 'Restarting…' : 'Restart'
      }),
      jsx(Btn, {
        kind: 'danger',
        onClick: () => act('stop', '/stack/stop'),
        disabled: noop || !up,
        title: 'Stops NoDPI and the switch — the client will be left without network',
        children: busy === 'stop' ? 'Stopping…' : 'Stop'
      }),
      jsx('span', { style: { flex: 1 } }),
      jsx(Btn, {
        kind: 'subtle',
        onClick: runProbe,
        disabled: noop || !up,
        children: busy === 'test' ? 'Testing…' : 'Test connection'
      })
    ]
  })

  const probeLine = probe
    ? jsx('div', {
        style: {
          marginTop: 8,
          fontSize: 12.5,
          color: probe.ok ? C.ok : C.bad,
          fontFamily: C.mono
        },
        children: probe.ok
          ? 'connection ok: opencode.ai, ' + probe.ms + ' ms, mode ' +
            (probe.mode === 'nodpi' ? 'through NoDPI' : 'directly')
          : 'connection failed: ' + (probe.error || 'error')
      })
    : null

  const errorBox = error
    ? jsxs('div', {
        style: {
          marginTop: 10,
          padding: '10px 12px',
          borderRadius: 9,
          border: '1px solid ' + C.bad,
          background: C.badSoft,
          color: '#ff9a9a',
          fontSize: 12.5,
          whiteSpace: 'pre-wrap'
        },
        children: [
          jsx('div', { children: error }),
          jsx('div', {
            style: { marginTop: 5, color: C.muted },
            children: up
              ? 'details in the logs below'
              : 'press "Start stack": the backend will launch the switch and NoDPI'
          })
        ]
      })
    : null

  /* --- components: rows wrap so they stay readable in a narrow pane --- */
  const metaItem = (label, value, mono) => jsxs('span', {
    style: {
      fontSize: 12,
      color: C.muted,
      fontFamily: mono ? C.mono : 'inherit',
      whiteSpace: 'nowrap'
    },
    children: [
      jsx('span', { style: { opacity: 0.7 }, children: label + ' ' }),
      jsx('span', { style: { color: C.fg }, children: value })
    ]
  })

  const compRows = comps.length === 0
    ? [jsx('div', { style: { color: C.muted, fontSize: 12.5 }, children: 'no data' }, 'empty')]
    : comps.map((c) => jsxs('div', {
        style: {
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: '8px 14px',
          padding: '11px 0',
          borderTop: '1px solid ' + C.lineSoft
        },
        children: [
          jsx('span', {
            style: { fontWeight: 700, fontSize: 13, minWidth: 92 },
            children: c.name
          }),
          jsx(Status, { state: c.state }),
          metaItem('port', String(c.port), true),
          metaItem('pid', c.pid ? String(c.pid) : '—', true),
          metaItem('uptime', fmtUptime(c.uptime_s)),
          c.restarts > 0
            ? jsx(Chip, {
                color: C.warn,
                bg: C.warnSoft,
                border: C.warn,
                children: 'auto ×' + c.restarts
              })
            : null,
          jsx('span', { style: { flex: 1, minWidth: 8 } }),
          jsx(Btn, {
            kind: 'chip',
            disabled: noop || c.state === 'unknown',
            onClick: () => c.name === 'NoDPI'
              ? act('restart:' + c.name, '/nodpi/restart')
              : act('restart:' + c.name, '/stack/restart'),
            children: busy === 'restart:' + c.name ? '…' : 'restart'
          })
        ]
      }, c.name))

  const compSection = jsx(Section, {
    label: 'components',
    hint: 'the switch holds the client port and keeps NoDPI alive',
    style: { marginTop: 12 },
    children: jsxs('div', {
      children: [
        compRows,
        comps.filter((c) => c.last_error).map((c) => jsx('div', {
          style: {
            marginTop: 6,
            padding: '7px 10px',
            borderRadius: 8,
            background: C.badSoft,
            border: '1px solid ' + C.bad,
            color: '#ff9a9a',
            fontSize: 12,
            whiteSpace: 'pre-wrap'
          },
          children: c.name + ': ' + c.last_error
        }, c.name + '-err'))
      ]
    })
  })

  /* --- logs --- */
  const logLines = logs[logTab] || []

  const tab = (key, label) => jsx(Btn, {
    kind: logTab === key ? 'primary' : 'chip',
    onClick: () => setLogTab(key),
    children: label
  })

  const logSection = jsx(Section, {
    label: 'logs',
    style: { marginTop: 12 },
    right: jsx(Btn, { kind: 'chip', onClick: load, children: 'refresh' }),
    children: jsxs('div', {
      children: [
        jsxs('div', {
          style: { display: 'flex', gap: 6, marginBottom: 9, flexWrap: 'wrap' },
          children: [
            tab('switch', 'Switch'),
            tab('access', 'NoDPI: access'),
            tab('error', 'NoDPI: errors')
          ]
        }),
        jsx('div', {
          style: {
            fontSize: 11.5,
            color: C.muted,
            marginBottom: 5,
            fontFamily: C.mono
          },
          children: TITLES[logTab] || TITLES.switch
        }),
        jsx('pre', {
          style: {
            margin: 0,
            maxHeight: 220,
            overflow: 'auto',
            background: 'rgba(0,0,0,0.30)',
            border: '1px solid ' + C.lineSoft,
            borderRadius: 8,
            padding: '9px 11px 14px',
            marginBottom: 2,
            fontFamily: C.mono,
            fontSize: 11.5,
            lineHeight: 1.55,
            whiteSpace: 'pre-wrap',
            color: 'rgba(220,220,220,0.9)'
          },
          children: (logLines.length
            ? logLines.join('\n')
            : logTab === 'error'
              ? 'no errors'
              : 'empty')
        })
      ]
    })
  })

  return jsxs('div', {
    style: {
      padding: '18px 22px 26px',
      maxWidth: 900,
      color: C.fg,
      fontFamily: 'inherit'
    },
    children: [jsx(StyleTag, {}), header, nowLine, modeSection, actions, probeLine, errorBox, compSection, logSection]
  })
}

export default {
  id: ID,
  name: 'Network switch',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PATH },
        render: () => jsx(NetworkPage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PATH, label: 'Network', codicon: 'globe' }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'network-switch.open',
          label: 'Open Network Switch',
          keywords: ['network', 'proxy', 'nodpi', 'dpi', 'switch'],
          run: () => host.navigate(PATH)
        }
      }
    ])
  }
}
