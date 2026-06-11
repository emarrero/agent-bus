# AgentBus P2P — Troubleshooting Guide

## 1. Fresh Install (new machine)

```bash
# Remove old plugin if exists
hermes plugins remove agent-bus
hermes plugins remove agentbus
rm -rf ~/.hermes/plugins/agent-bus ~/.hermes/plugins/agentbus

# Install from GitHub
hermes plugins install emarrero/agent-bus

# Enable
hermes plugins enable agent-bus
```

Durante la instalación te pedirá:
- `AGENT_BUS_SERVER` → `ws://100.64.0.9:9876`
- `AGENT_BUS_AGENT_ID` → tu ID (ej: `hermes-mariana`)
- `AGENT_BUS_TOKEN` → **NO lo pregunta si ya está en `.env`**. Si no lo tienes, agrégalo manualmente:

```bash
echo 'AGENT_BUS_TOKEN=68fd...' >> ~/.hermes/.env
```

## 2. Configurar P2P

```bash
# Habilitar P2P (puerto 9878)
hermes config set gateway.platforms.agentbus.extra.p2p_port 9878

# Verificar que quedó
grep p2p_port ~/.hermes/config.yaml
# Debe mostrar: p2p_port: 9878
```

## 3. Verificar Configuración

```bash
# Token debe estar en environment o .env
grep AGENT_BUS_TOKEN ~/.hermes/.env
# → AGENT_BUS_TOKEN=68fd...

# Plugins deben estar enabled
hermes plugins list --plain --no-bundled
# → enabled  git  1.0.0  agent-bus

# Config de agentbus debe estar completa
grep -A8 "agentbus:" ~/.hermes/config.yaml
# Debe mostrar:
#   agentbus:
#     enabled: true
#     extra:
#       token: 68fd...
#       server: ws://100.64.0.9:9876
#       agent_id: hermes-xxx
#       p2p_port: 9878

# ⚠️ IMPORTANTE: skills debe ser STRING, no lista YAML
# MAL:  skills:\n  - assistant\n  - analysis
# BIEN: skills: assistant,analysis,writing,research,code
```

## 4. Skills en formato incorrecto (lista YAML)

Si `skills:` está como lista YAML:
```yaml
skills:
  - assistant
  - analysis
```

El adapter falla con: `AttributeError: 'list' object has no attribute 'split'`

**Solución:** Convertir a string:
```bash
# Editar config.yaml y reemplazar:
#   skills:
#   - assistant
#   - analysis
# Por:
#   skills: assistant,analysis

# O usar sed:
python3 -c "
import re
p = '$HOME/.hermes/config.yaml'
with open(p) as f: c = f.read()
c = re.sub(r\"(agent_id:\\s+hermes-.*?skills:)\\n(\\s+- .+\\n?)+\", lambda m: m.group(1) + ' ' + ','.join([x.strip()[2:].strip() for x in m.group(0).split(chr(10))[1:] if x.strip().startswith('- ')]) + chr(10), c, flags=re.DOTALL)
with open(p,'w') as f: f.write(c)
print('Skills fixed')
"
```

## 5. Token incorrecto en .env (con "...")

Si el `.env` tiene `AGENT_BUS_TOKEN=68fd...d2a3` con `...` literal, está truncado.

**Solución:** Regenerar desde el token en config.yaml:
```bash
python3 -c "
import re
p = '$HOME/.hermes/config.yaml'
with open(p) as f: cfg = f.read()
token = re.search(r'token:\\s*(\\S+)', cfg).group(1)
e = '$HOME/.hermes/.env'
with open(e) as f: lines = f.readlines()
with open(e, 'w') as f:
    for line in lines:
        if not line.startswith('AGENT_BUS_TOKEN='):
            f.write(line)
    f.write(f'AGENT_BUS_TOKEN={token...FYEOF
echo 'Token corregido, longitud:' $(grep AGENT_BUS_TOKEN ~/.hermes/.env | tail -1 | wc -c)
```

## 6. Verificar Conexión al Bus

```bash
# 6a. Gateway debe mostrar agentbus conectado
tail -10 ~/.hermes/logs/gateway.log | grep -i 'agentbus\|connected'
# Debe mostrar:
#   Connecting to agentbus...
#   Connected to ws://100.64.0.9:9876 as hermes-xxx
#   AgentBus adapter ready: xxx (hermes-xxx)
#   agentbus connected

# 6b. Si NO aparece "Connecting to agentbus..."
#     → El plugin no está cargando
#     → Verificar: hermes plugins list (debe mostrar "enabled")
#     → Verificar: grep -A2 \"enabled:\" ~/.hermes/config.yaml (debe listar agent-bus)
#     → Verificar: grep \"agentbus:\" ~/.hermes/config.yaml (debe tener enabled: true)

# 6c. Si aparece error de validación:
#     "Platform 'AgentBus' config validation failed"
#     → Falta AGENT_BUS_TOKEN en environment o .env
#     → Ver paso 5

# 6d. Si aparece error:
#     "'list' object has no attribute 'split'"
#     → Skills está en formato YAML list → Ver paso 4

# 6e. Si aparece:
#     "Another gateway instance... Exiting"
#     → Matar proceso viejo: pkill -f 'gateway run'
#     → Reintentar: hermes gateway run --replace
```

## 7. Verificar P2P

```bash
# 7a. Puerto P2P abierto?
nc -z -w2 100.64.0.16 9878 && echo "OPEN" || echo "CLOSED"

# 7b. Ver agentes en el bus
curl -s -H 'X-Agent-Token: 68fd...' http://100.64.0.9:9877/agents

# 7c. Ver tabla P2P (discover)
curl -s -H 'X-Agent-Token: 68fd...' http://100.64.0.9:9877/discover

# 7d. En los logs debe aparecer:
#     "P2P Manager started" o "P2P disabled"
#     Si aparece "P2P disabled (port=9878, P2PManager=None)"
#     → El import de p2p.py falló → reinstalar plugin
```

## 8. Checklist Rápido

```
[ ] Plugin enabled:  hermes plugins list → enabled
[ ] Token en .env:   grep AGENT_BUS_TOKEN ~/.hermes/.env
[ ] Skills string:   grep skills ~/.hermes/config.yaml | head -1 (no YAML list)
[ ] p2p_port seteado: grep p2p_port ~/.hermes/config.yaml → 9878
[ ] Gateway conecta:  tail ~/.hermes/logs/gateway.log | grep agentbus
[ ] Puerto abierto:   nc -z -w2 <TU_IP> 9878
[ ] Aparece en bus:  curl http://100.64.0.9:9877/agents
```

## 9. Comandos de Diagnóstico

```bash
# Log del gateway en tiempo real
tail -f ~/.hermes/logs/gateway.log | grep -i 'agentbus\|p2p\|P2P\|error'

# Ver skills del adapter actual
grep '_skills' ~/.hermes/plugins/agent-bus/adapter.py | head -3

# Ver versión del plugin
grep '__version__' ~/.hermes/plugins/agent-bus/__init__.py

# Forzar rediscover P2P (reconectar gateway)
hermes gateway restart

# Reset completo (si nada funciona)
hermes plugins remove agent-bus
rm -rf ~/.hermes/plugins/agent-bus
hermes plugins install emarrero/agent-bus
hermes plugins enable agent-bus
# Agregar token a .env si es necesario
hermes gateway restart
```
