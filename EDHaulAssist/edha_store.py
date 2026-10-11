"""Where a haul's progress lives between launches of EDMC.

One JSON file holding the imported plan and how far through it you are. It is a
file rather than an EDMC config value because a plan is far too big for a
registry string, and it sits outside the plugin folder so that replacing the
folder to upgrade the plugin does not throw away a half-finished haul.

Nothing here imports EDMC or tkinter.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FILENAME = "state.json"


def load(path: str) -> Optional[Dict[str, Any]]:
    """The saved state, or None if there is none or it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def save(path: str, state: Dict[str, Any]) -> bool:
    """Write the state so that a crash mid-write cannot leave half a file."""
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("could not write %s: %s", path, exc)
        return False
    return True


def _self_test() -> None:
    import tempfile

    logging.disable(logging.CRITICAL)      # the failures below are the point
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, FILENAME)
            assert load(path) is None
            assert save(path, {"run_index": 3})
            assert load(path) == {"run_index": 3}
            assert not os.path.exists(path + ".tmp")

            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            assert load(path) is None

            assert save(os.path.join(directory, "missing", FILENAME), {}) is False
    finally:
        logging.disable(logging.NOTSET)
    print("edha_store self-test: OK")


if __name__ == "__main__":
    _self_test()
