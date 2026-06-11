# AgentBus P2P — Plan de Implementación

## Concepto
Cada agente obtiene una tabla de agentes con su IP + puerto P2P.
Cuando quiere enviar un mensaje, intenta conexión directa (WebSocket P2P).
Si no hay ruta directa, cae por el server (relay).

## Fases

### Fase 1: Server — Endpoint /discover
- Incluir `p2p_port` en AgentCard
- Nuevo endpoint `/discover` que devuelve IP+puerto P2P de cada agente
- El `agents_list` ya incluye cards con IP, solo falta añadir p2p_port

### Fase 2: Protocolo — Tipos de mensaje P2P
- `p2p_hello`: autenticación entre peers directos
- `p2p_peer_update`: notificar cambios en tabla de rutas

### Fase 3: Adapter — Tabla de rutas + P2P
- Escuchar en puerto P2P para conexiones entrantes
- Al recibir `agents_list`, conectar directo a cada peer
- Tabla de rutas local: `{agent_id: WebSocket directo}`
- `send()`: priorizar P2P, fallback a server relay

### Fase 4: Prueba
- Verificar mensajes viajan P2P sin pasar por server
- Verificar fallback relay funciona
