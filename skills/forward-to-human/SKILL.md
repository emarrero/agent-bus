# Forward-to-Human — Bridge AgentBus → Telegram

**Purpose:** When Faye receives a message on the AgentBus that is clearly intended for the human user (Efrain), she must forward it to Telegram.

## How It Works

The `node.py` runs Faye as a permanent agent on the AgentBus. Each incoming message is processed by Hermes AI. When `--tools messaging` is enabled, the agent can use `send_message()` to deliver messages to Telegram.

## Rule

When processing an incoming bus message, if the **content or context** indicates it's meant for the human user (Efrain) — for example:

- "Tell Efrain that..."
- "Forward this to the human"
- "This is for the user"
- "Message for Efrain: ..."
- A task with `target: human`, `target: user`, or similar
- Any message where a peer explicitly asks you to relay information to the human

Then you MUST:

1. **Acknowledge receipt** on the bus to the sender
2. **Forward the message** to Efrain via Telegram using:
   ```
   send_message(target="telegram", message="[AgentBus] <sender>: <content>")
   ```
3. Report back to the sender that the message was delivered

## When NOT to forward

- Normal inter-agent conversation (tasks, questions between agents)
- System messages, status updates, heartbeats
- Messages that are idle chatter or no-op
- Broadcasts unless they explicitly say "for the human"

## Configuration

The node must be started with `--tools messaging` for Telegram access:

```bash
python3 node.py --token TOKEN --server ws://... --tools messaging
```

Or via env var:
```bash
export AGENT_BUS_TOOLS=messaging
```

## Environment Variables

| Variable | Value | Purpose |
|---|---|---|
| `AGENT_BUS_TOOLS` | `messaging` | Enable Telegram send capability |
| `AGENT_BUS_SYSTEM` | *(see below)* | System prompt with forwarding rules |

### System Prompt Addition

Add to `AGENT_BUS_SYSTEM` or the node's `--system` flag:

> **HUMAN FORWARDING RULE:** If a message on the bus is clearly meant for the human user (Efrain) — someone asks you to "tell Efrain", "forward to the human", or the message content addresses Efrain directly — you MUST forward it to him via Telegram. Use `send_message(target="telegram", message="[AgentBus] <sender>: <content>")`. Acknowledge to the sender, then forward. Do NOT forward normal agent-to-agent conversation.
