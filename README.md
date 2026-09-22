# ETH DGT Bot

Bot de **trading** de ETH (no acumulación) con estrategia **DGT**: combina un
motor de *grid* para mercados laterales y un motor de *breakout* de Donchian
para tendencias fuertes, decidido dinámicamente por el **ADX(14)**.

Fork de la infraestructura del bot BTC (FastAPI + SQLAlchemy + APScheduler +
Docker), con estrategia, modelos, API y dashboard nuevos. Corre **en paralelo**
al bot BTC sin interferencia (puerto, contenedor, DB y variables de entorno
separados).

> ⚠️ **Arranca siempre en modo SIMULADOR (`DRY_RUN=true`).** Nunca ejecuta
> órdenes reales salvo que se ponga `DRY_RUN=false` de forma explícita.

---

## Estrategia

| ADX(14)        | Régimen    | Lógica                                                        |
|----------------|------------|--------------------------------------------------------------|
| `< 25`         | **grid**   | Grilla asimétrica (70% compras abajo / 30% ventas arriba) en un rango de ±6% |
| `>= 25`        | **breakout** | Ruptura del canal de Donchian(20) con stop-loss 2% / take-profit 6% (ratio 1:3) |

**Gestión de riesgo** (siempre activa):
- Máximo **10%** del capital por operación.
- Si el **drawdown diario supera 5%** → el bot devuelve `hold` (`reason="daily_limit"`).

La matemática (ADX, Donchian) es pura y está cubierta por tests
(`tests/test_strategy_eth.py`).

## Fuente de datos

- **Velas OHLCV 15m**: API pública de Binance (`ETHUSDT`, sin auth), cacheadas
  en Redis (opcional) o memoria, y persistidas en SQLite (`eth_candles`).
- **Órdenes** (modo live): Bitso API v3 (`eth_ars` por defecto; también
  `eth_mxn` / `eth_usd`).

## Ciclo (cada 15 min, vía scheduler)

1. Descarga velas (o usa caché si tienen < 14 min).
2. Calcula ADX y detecta el régimen.
3. Llama a `EthStrategyEngine.decide()`.
4. **Simulador**: registra la operación en DB sin ejecutar nada real.
5. **Live**: ejecuta la orden vía Bitso.
6. Guarda un `EthBotState`.
7. Actualiza la caché con el estado actual.

## API

| Método | Ruta                    | Descripción                              |
|--------|-------------------------|------------------------------------------|
| GET    | `/api/eth/status`       | régimen, adx, precio, capital, P&L       |
| GET    | `/api/eth/trades`       | últimos 50 trades con P&L y win rate     |
| GET    | `/api/eth/grid`         | niveles activos del grid                 |
| GET    | `/api/eth/candles`      | últimas 100 velas 15m cacheadas          |
| GET    | `/api/eth/history`      | evolución del capital (para el gráfico)  |
| POST   | `/api/eth/bot/start`    | inicia el bot                            |
| POST   | `/api/eth/bot/stop`     | detiene el bot                           |
| POST   | `/api/eth/config`       | guarda capital / levels / range_pct      |
| POST   | `/api/eth/mode`         | cambia SIMULADOR ↔ LIVE                   |
| POST   | `/api/eth/tick`         | corre un ciclo manual                    |

## Puesta en marcha

### Docker (recomendado)

```bash
cp .env.example .env      # revisá SESSION_SECRET, WEB_PASSWORD, etc.
docker compose up -d --build
# Dashboard en http://localhost:7001  (Admin / 123456 por defecto)
```

El bot BTC sigue en el puerto 7000; este usa el **7001**, contenedor `eth-bot`,
DB `data/eth_bot.db`.

### Local (desarrollo)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 7001
```

### Tests

```bash
.venv/Scripts/python -m pytest tests/ -q
```

## Variables de entorno clave

Ver `.env.example`. Las más importantes:

- `ETH_BOT_ENABLED` — habilita el scheduler.
- `ETH_CAPITAL_USD` — capital simulado inicial (default `$100`).
- `ETH_GRID_LEVELS`, `ETH_GRID_RANGE_PCT`, `ADX_THRESHOLD`.
- `DRY_RUN` — `true` = simulador (default). **No cambiar sin querer.**
- `TRADING_PAIR` — `eth_ars` | `eth_mxn` | `eth_usd`.
- `REDIS_ENABLED` / `REDIS_URL` — caché opcional.

## Seguridad

- El bot **arranca en simulador**. Para operar en vivo hay que poner
  `DRY_RUN=false` (o usar el toggle del dashboard, que pide confirmación).
- El cliente Bitso tiene TODOs marcados (`[B2]`, `[B3]`, `[B6]`) que deben
  verificarse contra la documentación oficial antes de habilitar el modo live.
- Autenticación del dashboard con bcrypt + sesión firmada; bloqueo tras 5
  intentos fallidos.
