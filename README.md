# Intesa Sanpaolo Swing Dashboard

A decision-support dashboard for **Intesa Sanpaolo S.p.A. (ISP.MI)**, built for investors who want to combine a long-term core position with swing trading.

The dashboard is written in **Python + Flask + HTML/JavaScript** and is designed to run both on a desktop computer and on a Raspberry Pi as a lightweight always-on web dashboard.

## Features

- Multi-timeframe analysis:
  - Daily
  - 1H
  - 15m
- Previous day Range High / Range Low
- Swing High / Swing Low detection
- Historical swing levels
- EMA 20 / EMA 50
- RSI
- MACD
- ATR
- Relative Volume
- VCP-style contraction detection
- Tight Base detection
- Breakout / Retest / Rejection / Breakdown / Reclaim
- Core vs Swing decision model
- Rule-based score system
- Fundamental snapshot
- Telegram alerts
- Important news monitoring
- Daily Telegram summary
- Desktop mode
- Raspberry Pi server mode

## Core + Swing concept

The dashboard separates the long-term position from the shorter swing position.

Typical states:

- `HOLD CORE`
- `REVIEW CORE`
- `ADD SWING`
- `HOLD / WAIT`
- `REDUCE SWING`

Swing High and Swing Low levels are used as technical reference points and are **not automatic buy or sell signals**.

## Telegram alerts

The dashboard can send Telegram notifications for:

- potential ADD SWING setups
- REDUCE SWING / risk setups
- important company news
- daily market summaries

News alerts are intentionally short and include a link to the original source.

## Raspberry Pi

The project can run continuously on a Raspberry Pi and be accessed from another device on the same network through a web browser.

Example:

```text
http://raspberrypi.local:5000 or http://192.168.x.x:5000

