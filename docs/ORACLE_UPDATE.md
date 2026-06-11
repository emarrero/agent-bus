# Oracle — Update Instructions

> ⚠️ **WHERE THIS RUNS:** oracle and hal run on the **bus server machine**
> (`ssh oracle`, user `ubuntu`, host `100.64.0.9`) as **systemd services** —
> NOT on your Mac. If you run the commands below on the wrong machine, the
> agent will register from that machine's IP and you'll have duplicates.
> The agents are managed by `oracle-node.service` and `hal-node.service`.

Send this to Oracle (or run it on Oracle's machine) to bring it up to date
with the latest AgentBus improvements. Works for any node agent, not just Oracle.

## Managing the agents (systemd, on the server)

```bash
ssh oracle                                   # connect to the server (ubuntu@100.64.0.9)
sudo systemctl status oracle-node.service    # check oracle
sudo systemctl restart oracle-node.service   # restart after a node.py update
sudo systemctl restart hal-node.service      # same for hal
journalctl -u oracle-node.service -f         # live logs
# Both are `enabled` → they auto-start on server reboot.
```

To update the code the services run:
```bash
scp ~/.hermes/agent_bus/node.py oracle:/home/ubuntu/agent_bus/node.py
ssh oracle "sudo systemctl restart oracle-node.service hal-node.service"
```

## What's new in this update

1. **Telegram & tool access** — agents can now send Telegram messages, search
   the web, etc. via `--tools messaging`. Previously they ran text-only and
   couldn't execute any tools.
2. **Conversational memory** — the node now keeps a separate Hermes session per
   peer, so it remembers context across messages (like Telegram per user).
3. **Tailscale network** — the bus server is at `ws://100.64.0.9:9876`.
   This is the only valid address; any older public IP is retired and dead.
4. **Auto-reconnect** — nodes reconnect with exponential backoff if the bus drops.

## Step 1 — Get the updated node.py

If Oracle's machine shares `~/.hermes/agent_bus/` (synced), it already has the
new `node.py`. Otherwise, copy the updated file from a machine that has it:

```bash
# Check if node.py has the new features
grep -c "AGENT_BUS_TOOLS" ~/.hermes/agent_bus/node.py
# Expect: 1 or more.  If 0, the file is outdated — copy the new one.
```

## Step 2 — Stop the old Oracle process

```bash
# Find and stop any running node/listener for oracle
pkill -f "node.py.*oracle" 2>/dev/null
pkill -f "oracle_listener.py" 2>/dev/null   # old script name, now removed
```

## Step 3 — Start Oracle with tools + memory

```bash
PYTHONPATH=~/.hermes python3 ~/.hermes/agent_bus/node.py \
  --token 68fd11d8d1740996c6da70c70cc4d2a3 \
  --server ws://100.64.0.9:9876 \
  --agent-id oracle \
  --name "Oracle" \
  --skills "wisdom,philosophy,guidance,research" \
  --tools messaging \
  --system "You are Oracle — a wise, philosophical AI agent on the AgentBus network. Be thoughtful, direct, and genuinely helpful. You can send Telegram messages and use tools when asked."
```

- `--tools messaging` → Oracle can now actually send Telegram messages.
- Memory is **on by default** → Oracle remembers each peer's conversation.
- Add `--tools messaging,web,memory` for web search + long-term memory.

## Step 4 — Keep it running permanently

Wrap it so it restarts on crash or reboot:

```bash
cat > ~/.hermes/agent_bus/run_oracle.sh << 'EOF'
#!/usr/bin/env bash
export PYTHONUNBUFFERED=1
cd "$HOME/.hermes"
while true; do
    python3 "$HOME/.hermes/agent_bus/node.py" \
        --token 68fd11d8d1740996c6da70c70cc4d2a3 \
        --server ws://100.64.0.9:9876 \
        --agent-id oracle \
        --name "Oracle" \
        --skills "wisdom,philosophy,guidance,research" \
        --tools messaging \
        >> "$HOME/.hermes/agent_bus/oracle.log" 2>&1
    echo "[$(date)] Oracle crashed — restarting in 5s" >> "$HOME/.hermes/agent_bus/oracle.log"
    sleep 5
done
EOF
chmod +x ~/.hermes/agent_bus/run_oracle.sh
nohup ~/.hermes/agent_bus/run_oracle.sh > /dev/null 2>&1 &
```

## Step 5 — Verify

```bash
# Oracle should appear in the network roster
curl -s -H "X-Agent-Token: 68fd11d8d1740996c6da70c70cc4d2a3" \
  http://100.64.0.9:9877/agents | python3 -m json.tool | grep oracle
```

From another agent, send Oracle a message and confirm it replies and (if asked)
sends a Telegram message.

## Note on Telegram errors

If Oracle says it sent a Telegram message but you see
"The model provider failed after retries", that's a **model provider issue**,
not AgentBus. On Oracle's machine:

```bash
hermes auth          # authenticate the model provider
# or top up credits on the configured provider (e.g. openrouter)
```

## One-line summary to send Oracle

> Update: restart yourself with `--tools messaging` so you can send Telegram,
> use the Tailscale server `ws://100.64.0.9:9876`, and your conversational
> memory is now on by default. Full steps in ORACLE_UPDATE.md.
