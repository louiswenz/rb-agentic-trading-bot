# Price CSV Format

Place daily OHLC CSV files in this directory or another directory of your choice. Use one file per symbol, named like `SPY.csv` or `AAPL.csv`.

Required columns:

```csv
Date,Open,High,Low,Close
2025-01-02,100.00,102.00,99.50,101.25
```

Then run:

```bash
python3 ../swing_strategy.py --prices-dir . --account-value 10000 --settled-cash 10000 --monthly-start-equity 10000
```
