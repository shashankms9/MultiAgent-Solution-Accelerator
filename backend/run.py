"""Development server launcher.

Usage:
    python run.py              # with --reload (default for dev)
    python run.py --no-reload  # without --reload
"""

import sys
import uvicorn

if __name__ == "__main__":
    reload_mode = "--no-reload" not in sys.argv
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=reload_mode,
    )
