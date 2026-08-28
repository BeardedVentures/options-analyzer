NL_ = chr(10)
"""
test_robinhood_mcp_connection.py -- one-time, standalone connectivity check for the
Robinhood Agentic Trading MCP integration (data/robinhood_mcp.py).

RUN THIS BEFORE the Robinhood path is trusted inside a real VEGA scan. It:
  1. Connects to Robinhood's official Trading MCP server (agent.robinhood.com/mcp/trading).
  2. The FIRST time you run it, a browser tab opens -- log into Robinhood and approve
     VEGA's read access. After that, the token is cached in
     data/.robinhood_mcp_tokens.json and this step is skipped on future runs.
  3. Prints the server's real tool list (names + input schemas) -- this is the part
     that matters most on the first run, since the integration in data/fetcher.py was
     written from Robinhood's public tool *names* (get_option_chains, get_option_quotes)
     without having seen a live response yet.
  4. Pulls a small SPY put chain and prints the raw response.

Usage:
    python test_robinhood_mcp_connection.py

If this fails or the tool names/params printed don't match what
data/robinhood_mcp.py assumes, paste the output back so the mapping in
fetcher.py's _parse_robinhood_options() can be corrected against real data.
"""

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

sys.path.insert(0, ".")

# This script IS the interactive authorization step, so it is the one place allowed to
# open a browser. Everything else -- above all the hourly scheduled cycle -- runs with
# ROBINHOOD_MCP_ALLOW_BROWSER unset and fails fast instead of blocking on a prompt no one
# is there to answer. Set before config is imported so it is picked up at module load.
os.environ.setdefault("ROBINHOOD_MCP_ALLOW_BROWSER", "true")
# The path is off by default until this script has proved it works; enable it for this
# process only, so the test can run without touching the engine's own setting.
os.environ.setdefault("ROBINHOOD_MCP_ENABLED", "true")

import config  # noqa: E402
from data import robinhood_mcp  # noqa: E402


def _closing_instructions():
    print(NL_ + "=" * 70)
    print("IF THE CHAIN ABOVE LOOKS RIGHT:")
    print("  The engine keeps Robinhood OFF until you switch it on explicitly.")
    print("  Enable it by setting  ROBINHOOD_MCP_ENABLED=true  in .env, then watch the")
    print("  next cycle for  chain_source=robinhood  in the chain-quality log.")
    print("IF IT DID NOT:")
    print("  Leave it off and send the tool list + raw response above for remapping.")
    print("=" * 70)


def main():
    server_url = config.ROBINHOOD_MCP_URL
    print(f"Connecting to {server_url} ...")
    print("(A browser tab should open for you to approve access, if this is the first run.)\n")

    print("── Step 1: listing available tools ──────────────────────────────")
    try:
        tools = robinhood_mcp.list_tools(server_url)
    except Exception as exc:
        print(f"FAILED to list tools: {exc}")
        print("\nThis usually means either the 'mcp' package isn't installed")
        print("(pip install mcp) or the OAuth approval didn't complete.")
        sys.exit(1)

    for t in tools:
        print(f"\n• {t['name']}")
        if t.get("description"):
            print(f"  {t['description']}")
        print(f"  input_schema: {json.dumps(t.get('input_schema'), indent=2)}")

    option_tool_names = [t["name"] for t in tools if "option" in t["name"].lower()]
    print(f"\nOption-related tools found: {option_tool_names or '(none found -- see full list above)'}")

    print("\n── Step 2: pulling a real SPY put chain ─────────────────────────")
    try:
        result = robinhood_mcp.fetch_put_chain("SPY", server_url)
    except Exception as exc:
        print(f"FAILED to fetch chain: {exc}")
        sys.exit(1)

    if result is None:
        print("fetch_put_chain returned None -- check the WARNING log line above for the cause.")
        sys.exit(1)

    print("\nRaw 'chains' response (first 2000 chars):")
    print(json.dumps(result.get("chains"), indent=2)[:2000])

    print("\nRaw 'quotes' response (first 2000 chars):")
    print(json.dumps(result.get("quotes"), indent=2)[:2000])

    print("\nDone. If both raw responses above show real bid/ask/greeks data for SPY puts,")
    print("the connection works end-to-end -- send this output back so fetcher.py's")
    print("_parse_robinhood_options() field-mapping can be checked against the real shape.")


if __name__ == "__main__":
    main()
    _closing_instructions()
