"""
main.py — Entry point for the RBRCS application.
"""
import sys, logging
from src.web.server import run

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run()
