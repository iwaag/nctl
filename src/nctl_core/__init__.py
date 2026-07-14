"""nctl_core: library backing the nctl CLI.

The CLI (nctl_core.cli) is a thin wrapper: all business logic lives in this
package and returns pydantic models; nothing here prints or exits.
"""

__version__ = "0.0.1"
