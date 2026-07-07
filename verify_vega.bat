@echo off
REM VEGA verification — compiles all modules and runs the data smoke test.
REM Output is written to verify_output.txt so it can be read back programmatically.
cd /d "C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence"

echo ===== VEGA VERIFY START ===== > verify_output.txt 2>&1
echo. >> verify_output.txt 2>&1
python --version >> verify_output.txt 2>&1
echo. >> verify_output.txt 2>&1

echo ===== PY_COMPILE ===== >> verify_output.txt 2>&1
python -m py_compile config.py main.py analysis/edge_calculator.py analysis/strike_validator.py analysis/synthesizer.py data/fetcher.py data/technicals.py data/fundamentals.py data/news.py output/renderer.py output/emailer.py analysis/outcome_logger.py log_outcome.py vega_ingest.py smoke_test_data.py >> verify_output.txt 2>&1
if %errorlevel%==0 (echo COMPILE_OK >> verify_output.txt 2>&1) else (echo COMPILE_FAILED errorlevel=%errorlevel% >> verify_output.txt 2>&1)
echo. >> verify_output.txt 2>&1

echo ===== SMOKE TEST (live yfinance) ===== >> verify_output.txt 2>&1
python smoke_test_data.py >> verify_output.txt 2>&1
echo. >> verify_output.txt 2>&1

echo ===== VEGA VERIFY DONE ===== >> verify_output.txt 2>&1
