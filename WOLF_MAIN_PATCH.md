# main.py — WOLF Ingest Patch

Apply these two changes to `options_intelligence/main.py` to enable JARVIS ingest.

---

## Change 1: Add import at top of file

After the existing imports block (around line 17), add:

```python
# WOLF: JARVIS integration (non-blocking — scan completes even if tower unreachable)
try:
    from wolf_ingest import post_to_jarvis
    WOLF_INGEST_ENABLED = True
except ImportError:
    WOLF_INGEST_ENABLED = False
```

---

## Change 2: Call post_to_jarvis at end of run_scan()

In `run_scan()`, after `append_scan_log(log_dir, scan_entry)` (around line 497), add:

```python
    # ── WOLF: Push scan results to JARVIS tower ──────────────────────────
    if WOLF_INGEST_ENABLED:
        # Read tipsheet HTML if available
        tipsheet_html = None
        if output_path and Path(output_path).exists():
            try:
                tipsheet_html = Path(output_path).read_text(encoding="utf-8")
            except Exception:
                pass

        # Build full qualified trade dicts (not just ticker/score summaries)
        full_scan_entry = dict(scan_entry)
        full_scan_entry["qualified_trades"] = [
            {k: v for k, v in t.items() if k != "component_breakdown"}
            for t in qualified_trades
        ]

        post_to_jarvis(
            scan_entry=full_scan_entry,
            session_type=session_type,
            market_context=market_context,
            tipsheet_html=tipsheet_html,
        )
```

---

## Notes

- `post_to_jarvis` is non-blocking. If the tower is unreachable, the scan still
  completes, logs locally, and emails normally. Zero risk to existing functionality.
- The `JARVIS_HOST` env variable must be set in GitHub Secrets (or `.env`).
- See networking options for reaching the home tower from GitHub Actions in the
  WOLF Integration Review document.
