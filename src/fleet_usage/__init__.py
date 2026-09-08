"""Fleet-wide collection and reporting of AI coding agent usage.

The package provides the ``fleet-usage`` command line interface which
collects ``ccusage`` output on every machine of a personal fleet, uploads
immutable snapshots to a private GitHub data repository and aggregates the
per-machine ledgers on read.
"""

__all__ = ['__version__']

__version__ = '0.1.0'
