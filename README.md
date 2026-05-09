# Volatility Crush Mode

If you want the system to surface and tag trades that sell premium ahead of earnings (to capture post-earnings IV collapse), set `ENABLE_VOL_CRUSH_MODE = True` in `config.py`.

- Trades within the earnings blackout window will be tagged as `volatility_crush` and flagged with special warnings in the output and narrative.
- Standard premium selling trades (no earnings risk) are tagged as `standard_premium`.
- The narrative and output will clearly distinguish between these two strategies.
# options-analyzer