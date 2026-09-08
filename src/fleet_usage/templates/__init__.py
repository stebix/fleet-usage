"""Files that are copied into the private data repository.

``readme.yml`` is the GitHub Actions workflow that regenerates the
README; the render script it calls is produced by copying
:mod:`fleet_usage.readme_render`, which is why the workflow installs no
dependencies.
"""

from pathlib import Path

__all__ = ['README_WORKFLOW', 'TEMPLATES_DIR', 'read_template']

TEMPLATES_DIR = Path(__file__).parent
README_WORKFLOW = 'readme.yml'


def read_template(name: str) -> str:
    """Return the contents of a packaged template.

    Parameters
    ----------
    name : str
        File name inside the templates directory.

    Returns
    -------
    str
        The file contents.
    """
    return (TEMPLATES_DIR / name).read_text(encoding='utf-8')
