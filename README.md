# Polymarket Time Decay Bot

Bot para explotar mercados narrativos de Polymarket donde el YES suele quedarse caro demasiado tiempo por sesgo humano. La idea es sencilla: si el mercado necesita que pase algo antes de una fecha fija y el reloj ya consumió casi toda la ventana sin que ocurra, la probabilidad justa del NO sube mucho más rápido de lo que suele reflejar el libro.

Ejemplos típicos:

- `Will Elon Musk tweet more than 15 times today?`
- `Will Apple announce an AR device this week?`
- `Will Company X file an 8-K today?`

El bot:

1. Lee mercados activos desde Gamma.
2. Filtra mercados binarios con ventana diaria o semanal y volumen/liquidez suficientes.
3. Descarta superficies obvias donde el modelo no aplica bien, como mercados meteorológicos, deportes o cruces de precio.
4. Modela una probabilidad generosa para el YES al inicio de la ventana y la hace decaer con un modelo de hazard constante sobre el tiempo restante.
5. Compra NO cuando la probabilidad modelada del NO supera al precio de mercado por el edge mínimo configurado.

Las órdenes live usan `py-clob-client-v2` y se envían como compras límite `GTC` sobre el token `NO` recomendado.

## Requisitos

```bash
python3 -m pip install -r requirements.txt
```

La única dependencia externa del repo es el cliente oficial del CLOB para ejecución real opcional. La lectura de Gamma, Telegram, SQLite, logging y el resto del flujo usan librería estándar.

## Configuración

### Estrategia

La estrategia versionada vive en `config/time_decay_strategy.json`.

Campos principales:

- `required_keywords`: verbos o términos que indican un evento discreto que todavía puede ocurrir.
- `blocked_keywords`: superficies a excluir, como mercados meteorológicos, deportes o mercados de precio.
- `keyword_filters`: filtros adicionales para concentrar el bot en un tema concreto sin tocar código.
- `optimistic_yes_prior`: probabilidad base generosa para el YES al inicio de la ventana.
- `yes_bias_buffer`: sesgo extra que se suma al YES cotizado para penalizar el optimismo residual.
- `min_hours_remaining` y `max_hours_remaining`: controlan cuán tarde o cuán temprano puede entrar el bot.
- `min_elapsed_fraction`: exige que ya se haya consumido una parte grande de la ventana.
- `min_edge`: edge mínimo requerido sobre el NO de mercado.
- `min_confidence`: confianza mínima exigida al NO modelado.
- `max_entry_price`: precio máximo aceptado para comprar NO.
- `entry_price_buffer`: colchón para construir la orden límite.
- `trade_amount_usdc`: tamaño por trade.
- `max_trades_per_cycle`: límite de órdenes nuevas por iteración.

### Telegram

Ejemplo en `config/telegram.env.example`:

```text
POLYMARKET_TELEGRAM_BOT_TOKEN=tu_token
POLYMARKET_TELEGRAM_CHAT_ID=tu_chat_id
```

### Credenciales CLOB

Ejemplo en `config/trading.env.example`:

```text
POLYMARKET_EXECUTE_TRADES=false
POLYMARKET_PRIVATE_KEY=tu_private_key
POLYMARKET_FUNDER_ADDRESS=
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_CLOB_API_KEY=
POLYMARKET_CLOB_API_SECRET=
POLYMARKET_CLOB_API_PASSPHRASE=
```

Si `POLYMARKET_EXECUTE_TRADES=true`, el bot entra en modo live aunque no pases `--execute-trades` por CLI.

## Uso

Escaneo puntual en paper mode:

```bash
python3 polymarket_time_decay_bot.py
```

Escaneo enfocado en narrativa de Apple o AR:

```bash
python3 polymarket_time_decay_bot.py \
  --keyword Apple \
  --keyword "AR device"
```

Monitor continuo con Telegram:

```bash
python3 polymarket_time_decay_bot.py \
  --watch \
  --interval 60 \
  --telegram \
  --telegram-env-file config/telegram.env
```

Orden real puntual usando credenciales del CLOB:

```bash
python3 polymarket_time_decay_bot.py \
  --execute-trades \
  --polymarket-env-file config/trading.env
```

Servicio local con logs rotativos:

```bash
python3 polymarket_time_decay_bot.py \
  --service \
  --telegram \
  --telegram-env-file config/telegram.env \
  --polymarket-env-file config/trading.env
```

Generar plist de `launchd`:

```bash
python3 polymarket_time_decay_bot.py \
  --write-launchd-plist \
  --service \
  --telegram \
  --telegram-env-file config/telegram.env \
  --polymarket-env-file config/trading.env
```

Mensaje de prueba a Telegram:

```bash
python3 polymarket_time_decay_bot.py --telegram --telegram-test-message "Prueba Time Decay Bot"
```

## Flags importantes

- `--keyword`: filtro adicional por palabra o frase. Se puede repetir.
- `--strategy-file`: permite cambiar sesgo, edge, filtros y sizing sin tocar código.
- `--execute-trades`: habilita envío de órdenes reales.
- `--polymarket-env-file`: carga private key, funder y API creds del CLOB.
- `--min-liquidity` y `--min-volume-24h`: endurecen o relajan el universo analizado.
- `--min-hours-remaining`, `--max-hours-remaining` y `--min-elapsed-fraction`: mueven el punto exacto de entrada en la curva de decaimiento.

## Despliegue en Oracle Cloud Free

1. Instala base mínima:

```bash
sudo apt update
sudo apt install -y git python3 python3-pip
```

2. Clona o sincroniza el repo en `/home/ubuntu/polymarket_time_decay_bot`.

3. Instala dependencias y prepara config:

```bash
cd /home/ubuntu/polymarket_time_decay_bot
python3 -m pip install --user -r requirements.txt
mkdir -p data logs
cp config/telegram.env.example config/telegram.env
cp config/trading.env.example config/trading.env
```

4. Ajusta `config/time_decay_strategy.json` y `config/trading.env`.

5. Valida antes de levantar el servicio:

```bash
python3 -m unittest -v test_polymarket_time_decay_bot.py
python3 polymarket_time_decay_bot.py --keyword Apple
python3 polymarket_time_decay_bot.py --telegram --telegram-test-message "Prueba Oracle"
```

6. Instala la unidad `systemd` incluida:

```bash
sudo install -m 644 systemd/polymarket-time-decay-bot.service /etc/systemd/system/polymarket-time-decay-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-time-decay-bot
sudo systemctl status polymarket-time-decay-bot
```

7. Revisa logs:

```bash
journalctl -u polymarket-time-decay-bot -f
tail -f /home/ubuntu/polymarket_time_decay_bot/logs/polymarket_time_decay_bot.log
```

El servicio de ejemplo queda en paper mode mientras `POLYMARKET_EXECUTE_TRADES=false`. Para pasar a live, cambia ese valor en `config/trading.env` y reinicia el servicio.

## Helper de despliegue

El script `scripts/deploy_oracle_vm.sh` sincroniza el repo por `rsync`, excluye `config/trading.env`, intenta instalar `requirements.txt` en remoto y reinstala la unidad `systemd` del bot.

Uso típico:

```bash
cp config/deploy.env.example config/deploy.env
./scripts/deploy_oracle_vm.sh
```

Opciones útiles:

```bash
./scripts/deploy_oracle_vm.sh --dry-run
./scripts/deploy_oracle_vm.sh --sync-only
```

## Persistencia local

SQLite se guarda en `data/polymarket_time_decay.sqlite3` y deduplica:

- alertas de Telegram por mercado y lado
- órdenes ya enviadas para no repetir la misma entrada en cada ciclo

Si el fichero nuevo todavía no existe pero hay una única base `.sqlite3` previa en `data/`, el bot la reutiliza automáticamente.

## Limitaciones

- El modelo no sabe si el evento ya ocurrió fuera de mercado; solo ve precio, texto y ventana temporal.
- La señal está pensada para eventos discretos con resolución por ocurrencia antes de un deadline, no para superficies continuas o deportivas.
- Los filtros por keywords son heurísticos. Si Polymarket cambia mucho la redacción de estos mercados, habrá que ajustar `required_keywords` y `blocked_keywords`.
- El bot manda órdenes límite `GTC`. Eso protege el precio, pero no garantiza fill inmediato.
