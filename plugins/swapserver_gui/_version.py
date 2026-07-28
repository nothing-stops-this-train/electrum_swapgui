#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# The plugin version, stamped into the zip at build time.
#
# The value committed here is deliberately the placeholder ``dev``: the release
# version is the *git tag*, and ``contrib/make_zip.sh`` rewrites the literal
# below in a staging copy of the package while building the archive (the working
# tree is never modified).  ``.github/workflows/build-plugin.yml`` passes
# ``${GITHUB_REF_NAME#v}`` for ``refs/tags/v*`` builds and ``dev-g<sha7>`` for
# every other build, so a release zip can never disagree with the tag it was
# built from and a dev zip is always traceable to the commit that produced it.
#
# Keep this module free of imports: ``contrib/make_zip.sh`` rewrites it with a
# line-oriented substitution, and the test suite parses it with ``ast`` without
# executing it.

__version__ = "dev"


def format_version(version: str) -> str:
    """Render a version string for display in the GUI.

    Real releases are numeric and conventionally shown with a leading ``v`` to
    match the git tag / GitHub release name (``0.2.1`` -> ``v0.2.1``).  The
    placeholder and CI dev stamps (``dev``, ``dev-g1a2b3c``) are not versions in
    that sense, so they are shown verbatim -- ``vdev`` would read as a release.
    """
    version = (version or "").strip()
    if not version:
        return "unknown"
    return "v" + version if version[0].isdigit() else version
