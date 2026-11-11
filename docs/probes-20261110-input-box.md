# Probes 2026-11-10: where each host draws its input box (#18)

Each host ran in its own tmux session on a private socket (`tmux -L wfc18probe`, 100×30), started in
this worktree; the server was killed afterwards (`tmux -L wfc18probe kill-server`). Text was typed with
`tmux send-keys -l`, as `type_into` does. Trailing blank lines are left out below.

## Codex 0.167.0 (`codex -s read-only -a never`)

Empty box after start: the placeholder, a blank line, a two-line footer.

```
› Ask Codex to do anything

  GPT-6.1-Sol medium · /path/to/workflow-core/.worktrees/18-impl
  ← for agents · ? for shortcuts
```

With the answer of message 175 typed (not submitted): the box grows, then a blank line and a one-line footer.

```
› [router] Answer to question 174: 批准page-scrape，通过现有统一UpstreamFetcher只读抓取官方基金页。
  条件：必须核实页面明确属于请求的基金代码，并定位明确“相关ETF”语义的链接关系，不是从任意六位href
  ...
  仅语法六位就证明是ETF。

  GPT-6.1-Sol medium · /path/to/workflow-core/.worktrees/18-impl
```

With much more text the box scrolls: its first visible line ends with `↑` at the right edge and the
`[router]` line is no longer on screen.

```
› 条件：必须核实页面明确属于请求的基金代码，并定位明确“相关ETF”语义的链接关系，不是从任意六位href  ↑
  猜；接受目标官方同域https基金详情URL且代码不同于联接基金，仅允许固定主机/路径拼接，不跟随任意外部
```

After one short submit (`Reply with just the word ok. Do not run any tools.`), the scrollback shows
the submitted message after the same `›`, with the empty box below it:

```
› Reply with just the word ok. Do not run any tools.
• ok
  Worked for 5s • 8:14 PM
› Ask Codex to do anything
  GPT-6.1-Sol medium · /path/to/workflow-core/.worktrees/18-impl · Reply with ok
  ← for agents · ? for shortcuts
```

(blank lines left out). An Enter into that empty box changed nothing on screen and submitted nothing.

The screen of message 176 (`ledger/prompts/issue-18-screen-enter-dropped.txt`) has no blank line
between the box and the footer, so the box's paragraph runs into the footer line; `codex_input_box`
takes trailing ` · ` lines of such a paragraph for the footer.

## Claude Code 2.1.322

The box sits between two `─` rules (the upper one may carry the session title); the prompt is `❯`.

```
──────────────────────────────────────────────────────────── <session title> ─
❯ [router] Answer to question 174: 批准page-scrape，通过现有统一UpstreamFetcher只读抓取官方基金页
  。条件：必须核实页面明确属于请求的基金代码，并定位明确“相关ETF”语义的链接关系，不是从任意六位hre
  f猜；接受目标官方同域https基金详情URL且代码不同于联接基金，仅允许固定主机/路径拼接。
────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
```

Not submitted (the account was close to its weekly limit), so what the transcript and the box look
like after a submit, after a long paste, or after an Enter into an empty box was not seen.

## OpenCode 1.19.5

A box bordered by `┃` on the left and closed by a `▀` row; the agent and model line is inside it.

```
             ┃
             ┃  [router] Answer to question 174: 批准page-scrape，
             ┃  通过现有统一UpstreamFetcher只读抓取官方基金页。条件：
             ┃  ...
             ┃  且代码不同于联接基金，仅允许固定主机/路径拼接。
             ┃
             ┃  Build · DeepSeek V4.1 Flash 1017 DeepSeek · max
             ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
                                                             tab agents  ctrl+p commands
```

OpenCode draws user messages in the transcript with a `┃` border too, and it wraps at word
boundaries rather than at the width. Not submitted either; so for Claude Code and OpenCode the router
keeps one Enter.
