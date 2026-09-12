"""
Minimal logger stub for signal_copy pipeline.
"""
import logging
import sys

# Create logger
logger = logging.getLogger("fusion_nexus")
logger.setLevel(logging.INFO)

# Console handler
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
