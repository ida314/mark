# End-to-end walkthrough

The automated suite covers the logic; this covers the wiring. Run it after a fresh install
or any change to the model endpoint, the policy file or the schema.

## 0. Bring it up

```bash
agent db up && agent db migrate && agent init && agent sandbox build && agent doctor
```

Everything should be `ok` except possibly the daemon. **`tool calling` must be `ok`** — if
it says the endpoint returned no tool calls, the router is dropping them and nothing
agentic will work. Point `llm.base_url` at the engine directly, or deploy the fix.

## 1. It remembers what you tell it

```bash
agent chat
› I moved to Brooklyn in March 2026, and I prefer terse answers.
› /exit
agent consolidate --session <id printed on exit>
agent memory facts
```

Expect a fact about Brooklyn and one about terse answers, each with confidence and a
category. `agent memory search "where do I live"` should return the Brooklyn fact.

## 2. It updates rather than contradicts itself

```bash
agent remember "I moved to Queens in August 2026"
agent memory history Dylan
```

Expect the Brooklyn fact to show as `superseded` with a `valid_to`, and the Queens fact as
`active`. Then:

```bash
agent memory search "where do I live"                     # Queens
agent memory search "where do I live" --as-of 2026-05-01  # Brooklyn, marked until 2026-08
```

That is the whole point of the bitemporal model: changing your mind does not corrupt
history, and the past stays answerable.

## 3. Writes ask first, and can be undone

```bash
agent chat
› Write a short note to my workspace at notes.md summarising what you know about me.
```

Expect an approval panel with a unified diff, the tool's risk and the rule that fired.
Approve it, then:

```bash
agent trace <turn-id>     # the tree: retrieval → llm calls → policy outcome → tool
agent undo <action-id>    # the file goes back
```

## 4. The sandbox is sealed

```bash
agent chat --autonomy act
› Run `python -c "print(2+2)"; whoami; touch /etc/x` in the sandbox.
```

Expect `4`, user `sandbox`, and a read-only filesystem error for `/etc/x`. Ask it to
`curl https://example.com` with `network` off and expect a failure: the container has no
network.

## 5. Untrusted content cannot drive actions

Ask it to fetch a web page, then to write a file in the same turn. Even at `--autonomy act`,
where workspace writes are normally automatic, the write asks for approval: the turn is
tainted. `agent trace` shows `+taint` appended to the rule id.

## 6. Delegation

```bash
› Research <topic> and summarise the three best sources.
```

Expect a `delegate` call to the researcher, then a summary with citations. The sub-agent's
transcript is archived but never enters your context — check with
`agent memory search` and the `raw_events` actor `subagent:researcher`.

## 7. Background work reaches you

```bash
agent daemon run &        # or: agent daemon install-unit
agent remind "stretch" --in 2m
agent chat                # leave it open
```

The notification prints above your prompt when it fires. `agent inbox` lists it too.
Watch the daemon log: with nothing actionable, the heartbeat spends **no** model call.

## 8. Memory becomes readable and revertible

```bash
agent consolidate --nightly
cd ~/.local/share/agent/memory && git log --oneline && cat profile/core.md
```

Expect a commit by `agent-consolidator` and a generated block listing facts with their ids.
Write a line of your own above the marker, then run `agent profile --edit` or wait for the
daemon: your text is committed as `user` and mined for candidates, and never overwritten.

## 9. Observability

`agent actions` for the audit tail, `agent why <action-id>` for the causal chain, and the
Jaeger link printed by `agent trace` for the spans.

## 10. MCP surface

```bash
agent mcp serve          # stdio
npx @modelcontextprotocol/inspector   # optional, to poke at it
```

`memory_search`, `profile_read`, `goals_list` and friends should be listed. This is what a
front end like OpenClaw would consume in pass 2.
