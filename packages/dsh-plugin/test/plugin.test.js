import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, chmodSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { bindingFor, createHooks, finalAssistantText, runHook } from '../lib/index.js'

const workspace = '/synthetic/project'
const binding = { workspace, command: ['/bin/true'] }

function agentWith(events, header = { id: 'session-1', cwd: workspace }) {
  return { id: header.id, session: { header, snapshotEvents: () => events } }
}

const user = (text, id = 'm1') => ({ id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] })

test('pre-step forwards the user prompt and appends returned recall', async () => {
  const calls = []
  const hooks = createHooks({
    resolveBinding: (cwd) => (cwd === workspace ? binding : undefined),
    run: async (_binding, payload) => {
      calls.push(payload)
      return { hookSpecificOutput: { additionalContext: 'synthetic recall' } }
    },
  })
  const decision = { kind: 'enter', messages: [user('hello')] }
  const result = await hooks.preStep({ agent: agentWith([]), turn: 1 }, async () => decision)
  assert.deepEqual(calls, [
    { hook_event_name: 'UserPromptSubmit', cwd: workspace, session_id: 'session-1', turn_id: 'm1', prompt: 'hello' },
  ])
  assert.equal(result.messages.length, 2)
  assert.equal(result.messages[1].content[0].text, 'synthetic recall')
  assert.equal(result.messages[1].source.kind, 'plugin:cloud-memory-hub-capture')
})

test('unbound workspaces, subagents and non-user messages are never forwarded', async () => {
  const calls = []
  const hooks = createHooks({ resolveBinding: (cwd) => (cwd === workspace ? binding : undefined), run: async (_b, p) => calls.push(p) })
  const plugin = { id: 'x', role: 'user', source: { kind: 'plugin:other' }, content: [{ type: 'text', text: 'injected' }] }
  await hooks.preStep({ agent: agentWith([], { id: 's', cwd: '/elsewhere' }), turn: 1 }, async () => ({ kind: 'enter', messages: [user('a')] }))
  await hooks.preStep({ agent: agentWith([], { id: 's', cwd: workspace, origin: 'subagent' }), turn: 1 }, async () => ({ kind: 'enter', messages: [user('a')] }))
  await hooks.preStep({ agent: agentWith([]), turn: 1 }, async () => ({ kind: 'enter', messages: [plugin] }))
  hooks.turnStopping({ agent: agentWith([], { id: 's', cwd: '/elsewhere' }), turn: 1 })
  assert.deepEqual(calls, [])
})

test('turn-stopping sends only the final assistant text of the current turn', async () => {
  const events = [
    { type: 'turn/start' },
    { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'old turn' }] } } },
    { type: 'turn/start' },
    { type: 'assistant/message', data: { message: { content: [{ type: 'reasoning', text: 'hidden' }, { type: 'tool-call' }] } } },
    { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'final answer' }] } } },
  ]
  assert.equal(finalAssistantText(events), 'final answer')
  assert.equal(finalAssistantText(events.slice(0, 4)), '')
  const calls = []
  const hooks = createHooks({ resolveBinding: () => binding, run: async (_b, p) => calls.push(p) })
  hooks.turnStopping({ agent: agentWith(events), turn: 2 })
  assert.deepEqual(calls, [
    { hook_event_name: 'Stop', cwd: workspace, session_id: 'session-1', turn_id: 'turn:2:assistant', last_assistant_message: 'final answer' },
  ])
})

test('binding lookup requires an exact workspace and a valid file', () => {
  const dir = mkdtempSync(join(tmpdir(), 'cmh-dsh-'))
  const file = join(dir, 'bindings.json')
  writeFileSync(file, JSON.stringify({ schema_version: 1, bindings: [binding] }))
  assert.deepEqual(bindingFor(workspace, file), binding)
  assert.equal(bindingFor(workspace + '/sub', file), undefined)
  writeFileSync(file, 'not json')
  assert.equal(bindingFor(workspace, file), undefined)
})

test('runHook passes stdin JSON and survives a failing command', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'cmh-dsh-'))
  const script = join(dir, 'hook.sh')
  writeFileSync(script, '#!/bin/sh\ncat >/dev/null\nprintf \'{"continue":true}\'\n')
  chmodSync(script, 0o700)
  assert.deepEqual(await runHook({ workspace: dir, command: [script] }, { a: 1 }), { continue: true })
  assert.equal(await runHook({ workspace: dir, command: [join(dir, 'missing')] }, {}), undefined)
})
