# OpenCode wake and completion probes, 2026-11-05

Host: OpenCode 1.19.5, installed with `npm install -g opencode-ai@1.19.5`. Model: `opencode/deepseek-v4.1-flash-free`, a free Zen model that needs no key (free models may collect prompts; the probes sent only fixed probe text). The OpenCode window was the interactive TUI, run in a detached tmux pane with a fixed server port:

```
opencode /path/to/workflow-core/.probe/p5 --port 4096 -m opencode/deepseek-v4.1-flash-free --prompt '<text>'
```

A project plugin, `.probe/p5/.opencode/plugins/probe.js`, logged every event except message parts to `.probe/p5/events.log`. Global configuration was not changed. On first start, OpenCode spent about 60 s installing the plugin's dependencies into `.opencode/node_modules` before the port opened.

## Probe 5: `prompt_async` into a live, idle OpenCode window

```
curl -X POST -H 'Content-Type: application/json' \
  -d '{"parts":[{"type":"text","text":"<text>"}]}' \
  http://127.0.0.1:4096/session/<session-id>/prompt_async     # → 204
```

| Time | Event |
| --- | --- |
| 01:43:13 | First turn done; `session.idle` |
| 01:43:42 | `prompt_async` sent (HTTP 204); `session.status: busy` **in the same second** |
| 01:44:17 | Reply `PROBE-5 woke` shown in the TUI; `session.idle` |

**Result: passed.** The idle window took the external message as a new turn at once. The 35 s gap was model generation on the free model, not wake latency. Session ids come from `GET /session`.

## Probe 3, OpenCode part: does `session.idle` mean done?

| Case | How the turn ended | Events |
| --- | --- | --- |
| A, done | Answered `PROBE-5A done` | `session.idle` |
| B, plain-text question | Asked red or blue and ended the turn | `session.idle`, 01:44:34, the same as A |
| C, question tool | Opened a red/blue choice in the TUI | **`question.asked`**, 01:44:37, with `id`, `sessionID` and the structured `questions` (`question`, `header`, `options`). The session stayed busy; no `session.idle` within 60 s |

## Findings

1. **One entry point, HTTP:** any process can wake an OpenCode session with `prompt_async`, provided the TUI or `opencode serve` was started with a known `--port`. With the default `--port 0` the port is random.
2. **`session.idle` has the same limit as `Stop` on the other hosts:** a plain-text question looks the same as "done".
3. **OpenCode has the cleanest "needs a person" signal of the three hosts.** Its question tool emits a dedicated `question.asked` event carrying the question and options, and it keeps the turn open. On Claude Code, `AskUserQuestion` only produces a generic `Notification` (`permission_prompt`). On Codex, no structured equivalent has been found yet.
4. The plugin `event` hook receives all of these events in-process, so an OpenCode worker can notify the controller directly from its plugin: `session.idle` plus a report-file check means done, and `question.asked` means it needs a person.

## Clean-up state

The tmux pane was closed and port 4096 is free. The probe session remains in OpenCode's local storage. `.probe/` is ignored by git and can be deleted.
