"""Central configuration. Every tunable lives here so the README can point at one file."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- data ---------------------------------------------------------------------
STARTER_SEED = ROOT / "data" / "starter-pack" / "seed.json"
ADDITIONS = ROOT / "data" / "additions.json"
REFERENCE_CASES = ROOT / "data" / "reference-cases.json"
SOURCE_CHANGE_DIR = ROOT / "data" / "source-change"
DB_PATH = Path(os.environ.get("APP_DB", ROOT / "data" / "workspace.sqlite"))

# --- model ---------------------------------------------------------------------
# MODEL_MODE controls where model responses come from:
#   live   - call the provider API, and record every response into CACHE_DIR
#   replay - read previously recorded responses from CACHE_DIR, never touch the network
#   stub   - offline keyword-overlap stand-in, for development without any credentials.
#            Clearly labelled everywhere it is used. NOT a substitute for a real run.
MODEL_MODE = os.environ.get("MODEL_MODE", "replay").strip().lower()
CACHE_DIR = Path(os.environ.get("MODEL_CACHE", ROOT / "cache"))

MODEL_ID = os.environ.get("MODEL_ID", "claude-sonnet-4-5")
MODEL_MAX_TOKENS = int(os.environ.get("MODEL_MAX_TOKENS", "1024"))
# Temperature 0 so that a recorded response is a fair representative of a repeat call.
MODEL_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0"))
API_KEY_ENV = "MODEL_API_KEY"

# --- validation ---------------------------------------------------------------
# When on, a question is only answered if the cited passages lexically cover the
# question's content words. Trades false "unresolved" on paraphrases for protection
# against confident answers built on unrelated evidence. See README limitations.
STRICT_COVERAGE = os.environ.get("STRICT_COVERAGE", "1") not in ("0", "false", "False")

# Generic English function words and question-form modifiers ("how long", "how often").
# Contains no product or domain terms, so this list adds no business rules.
STOPWORDS = frozenset(
    """
    a about an and any are as at be been being by can could do does did for from
    get got had has have how i if in into is it its may me must my of on once one
    only or our out over should so some such than that the their them then there
    these they this those to under until up was we were what when where whether
    which who whom why will with would you your
    far long many much often soon
    """.split()
)
