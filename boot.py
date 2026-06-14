# boot.py -- runs first on every boot (including wake from deepsleep).
# Keep this minimal: the application logic lives in main.py, which the
# firmware runs automatically right after this file.
import gc

gc.collect()
