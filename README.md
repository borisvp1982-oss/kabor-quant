# KaBor Quant

**Modular trading system for MyFundedPerps · Powered by Claude via MCP**

Self-hosted trading-bot platform for **MyFundedPerps (MFP) sandbox accounts**: many strategy bots on one
Raspberry Pi, paper or sandbox execution through the MFP REST API, web panel with login and HTTPS, Telegram
alerts with charts, and **SMT**, an MCP connector that lets Claude read the bots and draft trade plans that you confirm in the panel.

> **Independent project, not affiliated with or endorsed by MyFundedPerps.**

**Sandbox only.** The client refuses `fp_live_` keys and any host other than `sandbox.myfundedperpetuals.com`.
Nothing here is financial advice or a promise of profits or challenge passes; backtest and paper results do
not guarantee future results. Example strategies are for learning; backtest before you trade them.

## MFP API usage

- Market data comes from the MFP WebSocket stream; account and position state is still read over REST
  (every 60 s and on each closed candle). Moving it to the account stream is the next item on the roadmap.
- Every order carries a fixed `Idempotency-Key`; retries honour `Retry-After` on 429/503.
- API keys stay in `data/cuentas.json` (owner-only) and never appear in URLs, logs or the panel (last 4 chars only).

## Quick start (Raspberry Pi OS, Python 3)

```
unzip kabor_quant_publico_*.zip && cd kabor_quant
chmod +x install.sh bot.sh
./install.sh              # virtualenv, dependencies and system service
./bot.sh usuario          # panel user (password stored as a scrypt hash)
./bot.sh configurar       # answer 1 to HTTPS
./bot.sh comprobar        # checks everything, sends no orders
./bot.sh reiniciar && ./bot.sh panel
```

Then: panel → Accounts → add your `fp_test_` key (MFP → Settings → API Keys, Test, Read And Trade).

## Strategies

A strategy is one `.py` file in `estrategias/`. Start with `estrategias/cruce_emas.py` (EMA crossover, fully
commented) and read `ESTRATEGIAS.md`. Test any file on real MFP candles without installing it:

```
./bot.sh probar estrategias/cruce_emas.py --simbolo BTC --tf 15m --dias 60
```

The panel and the Spanish docs (`LEEME.md`, `ESTRATEGIAS.md`) cover everything else.

## License

Copyright 2026 Borislav Vladimirov Petrakiev. Licensed under the [Apache License 2.0](LICENSE): free to use,
modify and redistribute, including commercially, keeping the `NOTICE` file and the copyright notices.
The "KaBor Quant" name and logo are not licensed for other products: forks must use their own (see `NOTICE`).
