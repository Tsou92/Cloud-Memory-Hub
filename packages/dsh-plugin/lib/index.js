/**
 * Cloud Memory Hub capture for DeepSeek Harness.
 *
 * DSH has no shell hooks, so this plugin forwards the same two moments Claude
 * Code hooks see — prompt submitted, turn finished — to the installed
 * `cloud-memory-agent ... dsh-hook` command. All queueing, filtering, upload
 * and recall stay in that command; this file only translates events.
 *
 * Bindings come from ~/.cloud-memory-hub/dsh-capture.json, written by
 * `cloud-memory-setup automation --agent dsh`. A session is captured only when
 * its cwd equals a bound workspace exactly; nothing is guessed from paths.
 */
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'cloud-memory-hub-capture'
export const inject = ['agents']

const HOOK_TIMEOUT_MS = 12_000

export function bindingsFile() {
  return process.env.CLOUD_MEMORY_HUB_DSH_BINDINGS || join(homedir(), '.cloud-memory-hub', 'dsh-capture.json')
}

export function bindingFor(cwd, file = bindingsFile()) {
  if (!cwd) return undefined
  try {
    const document = JSON.parse(readFileSync(file, 'utf8'))
    if (document?.schema_version !== 1 || !Array.isArray(document.bindings)) return undefined
    return document.bindings.find(
      (item) => item?.workspace === cwd && Array.isArray(item.command) && item.command.length > 0,
    )
  } catch {
    return undefined
  }
}

function textOf(message) {
  return (message?.content || [])
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim()
}

/** Run the hook command; resolve its parsed stdout, or undefined on any failure. */
export function runHook(binding, payload) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(binding.command[0], binding.command.slice(1), {
        cwd: binding.workspace,
        stdio: ['pipe', 'pipe', 'ignore'],
        windowsHide: true,
      })
    } catch {
      return resolve(undefined)
    }
    let out = ''
    const timer = setTimeout(() => child.kill('SIGKILL'), HOOK_TIMEOUT_MS)
    child.stdout.on('data', (chunk) => {
      out += chunk
    })
    child.on('error', () => {
      clearTimeout(timer)
      resolve(undefined)
    })
    child.on('close', () => {
      clearTimeout(timer)
      try {
        resolve(JSON.parse(out))
      } catch {
        resolve(undefined)
      }
    })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

function sessionOf(agent) {
  const header = agent?.session?.header
  if (!header || header.origin === 'subagent') return undefined
  return { id: header.id || agent.id, cwd: header.cwd }
}

/** Last assistant text of the turn that is closing. */
export function finalAssistantText(events) {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]
    if (event?.type === 'turn/start') return ''
    if (event?.type === 'assistant/message') {
      const text = textOf(event.data?.message)
      if (text) return text
    }
  }
  return ''
}

export function createHooks({ resolveBinding = bindingFor, run = runHook } = {}) {
  return {
    async preStep({ agent, turn, signal }, next) {
      const decision = await next()
      if (decision?.kind !== 'enter' || signal?.aborted) return decision
      const session = sessionOf(agent)
      const binding = session && resolveBinding(session.cwd)
      if (!binding) return decision
      const injected = []
      for (const message of decision.messages || []) {
        if (message?.source?.kind !== 'user') continue
        const prompt = textOf(message)
        if (!prompt) continue
        const output = await run(binding, {
          hook_event_name: 'UserPromptSubmit',
          cwd: session.cwd,
          session_id: session.id,
          turn_id: message.id || `turn:${turn}:user`,
          prompt,
        })
        const context = output?.hookSpecificOutput?.additionalContext
        if (typeof context === 'string' && context.trim()) {
          injected.push({
            id: randomUUID(),
            role: 'user',
            content: [{ type: 'text', text: context }],
            source: { kind: `plugin:${name}`, form: 'recall' },
          })
        }
      }
      return injected.length ? { ...decision, messages: [...decision.messages, ...injected] } : decision
    },
    turnStopping({ agent, turn }) {
      const session = sessionOf(agent)
      const binding = session && resolveBinding(session.cwd)
      if (!binding) return
      const events = agent.session.snapshotEvents?.() ?? []
      const text = finalAssistantText(events)
      if (!text) return
      // Not awaited: the reply is already shown; capture must not hold the turn open.
      void run(binding, {
        hook_event_name: 'Stop',
        cwd: session.cwd,
        session_id: session.id,
        turn_id: `turn:${turn}:assistant`,
        last_assistant_message: text,
      })
    },
  }
}

export function apply(ctx) {
  const hooks = createHooks()
  ctx.on('agent/pre-step', hooks.preStep)
  ctx.on('agent/turn-stopping', hooks.turnStopping)
}
