"""Start the pipeline server.

    python run.py                 # http://127.0.0.1:8765, real-time clock
    python run.py --speed 10      # demo mode: the 5-minute clock runs in 30 seconds
    S2L_SPEED=10 python run.py    # same thing via environment
"""
import argparse
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--speed", type=float, default=None, help="clock multiplier for demos (10 = 5 min in 30 s)")
ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
args = ap.parse_args()
if args.speed:
    os.environ["S2L_SPEED"] = str(args.speed)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s2l import server  # noqa: E402

if __name__ == "__main__":
    server.serve(port=args.port)
