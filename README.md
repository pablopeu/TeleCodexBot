# telecodexbot

`telecodexbot` convierte una sesion interactiva de Codex en algo utilizable tambien desde Telegram, sin abrir APIs privadas ni tocar el repo destino.

Hace cuatro cosas:

- arranca `codex` dentro de una sesion `tmux` dedicada;
- refleja a Telegram los mensajes del usuario por CLI y las respuestas del asistente;
- mete en la sesion actual los mensajes que llegan por Telegram;
- mantiene config global del bot y estado separado por workspace.

## Requisitos

- `codex`
- `python3`
- `tmux`
- `curl`
- `ngrok`
- Linux/macOS con acceso a `~/.codex`

## Donde guarda datos

Config global:

- `~/.config/telecodexbot/config.json`

Estado por workspace:

- `~/.local/state/telecodexbot/<workspace_hash>/...`

No escribe nada dentro del repo donde corre Codex.

## Instalacion

Instala el launcher en `~/.local/bin` y, si queres, guarda la config del bot.

Modo interactivo:

```bash
cd /home/pablo/telecodexbot
./install
```

Modo no interactivo por flags:

```bash
cd /home/pablo/telecodexbot
./install \
  --bot-token 'TU_BOT_TOKEN' \
  --chat-id 123456789 \
  --user-id 123456789 \
  --username '@tu_usuario'
```

## Uso basico

Desde cualquier repo donde quieras trabajar con Codex:

```bash
cd /ruta/al/repo
telecodexbot up
```

Eso:

- crea o reutiliza una sesion `tmux` dedicada por workspace (`telecodexbot-<hash>` por defecto);
- abre `codex resume --last` dentro de ese workspace, o `codex` si no habia sesion previa;
- levanta webhook local + `ngrok`;
- arranca el relay Telegram <-> Codex;
- te adjunta a la sesion `tmux`.

Cuando quieras mirar solo los logs del relay de ese workspace:

```bash
telecodexbot logs
```

Para detener relay y webhook del workspace actual:

```bash
telecodexbot down
```

## Usar otro repo sin cambiar de directorio

```bash
telecodexbot --workspace /ruta/al/otro/repo up
```

## Comandos utiles

Verificar conectividad Telegram:

```bash
telecodexbot doctor
```

Enviar un mensaje manual:

```bash
telecodexbot send --text 'Necesito que pruebes esto en Windows'
```

Ver webhook actual:

```bash
telecodexbot webhook-info
```

Consumir el siguiente mensaje ya recibido por webhook:

```bash
telecodexbot inbox-next
```

## Variables de entorno utiles

- `TELECODEXBOT_WORKSPACE_DIR`: workspace objetivo si no queres usar el cwd.
- `TELECODEXBOT_ATTACH=0`: arranca todo pero no hace `tmux attach`.
- `TELECODEXBOT_AUTONOMOUS=1`: habilita fallback por `codex exec resume` en vez de solo `tmux`.
- `TELECODEXBOT_TMUX_TARGET=%3`: fija un pane de tmux explicito.
- `TELECODEXBOT_NOTIFY_WEBHOOK_START=1`: manda aviso cuando arranca webhook/ngrok.
- `TELECODEXBOT_WEBHOOK_PORT=8765`: cambia el puerto local del webhook.

## Arquitectura

- `bin/telecodexbot`: wrapper de alto nivel.
- `scripts/telecodexbot.py`: bridge Python con Telegram, webhook, inbox y relay.
- `scripts/start_tmux.sh`: bootstrap de la sesion `tmux` + Codex.
- `scripts/start_relay.sh`: daemon que espeja CLI/assistant y mete Telegram en `tmux`.
- `scripts/start_webhook.sh`: receptor local + `ngrok` + `setWebhook`.

## Notas

- Para chats privados, `chat_id` suele coincidir con `user_id`.
- Primero hay que abrir el bot en Telegram y tocar `Start`.
- El relay detecta la sesion activa de Codex por `session_meta.cwd`, asi que el mirroring queda atado al workspace real.
- El ingreso por Telegram usa `tmux send-keys -l` y la sesion de Codex arranca con `disable_paste_burst=true` para evitar prompts pegados que no se envian.
