"""Run on loopback Ollama; no cloud credentials are loaded."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['BILLING_PROVIDER'] = 'ollama'
os.environ.setdefault('BILLING_MODEL', 'qwen3:8b')
from billing_agent.__main__ import main
raise SystemExit(main())
