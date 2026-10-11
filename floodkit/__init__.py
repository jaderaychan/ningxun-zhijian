"""floodkit — urban ponding analysis toolkit for the AGH-based flood agent.

Public entry points:
    python -m floodkit selftest
    python -m floodkit fetch-dem --bounds W S E N
    python -m floodkit run --bounds W S E N --rain-mm 100 --cn 85 --tag demo
"""

import os as _os

# matplotlib rebuilds a font cache on first use and writes it to ~/.matplotlib.
# Under a restricted sandbox that write is denied, and the failure arrives as a
# warning in the middle of a figure — which is how one script in this project
# acquired the problem while the others did not. Setting it here, before anything
# can import matplotlib, is the only place that cannot be forgotten.
_os.environ.setdefault(
    "MPLCONFIGDIR",
    _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".mplconfig"),
)

__version__ = "0.1.0"
