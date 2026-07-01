#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# The __init__ module of an Electrum plugin is imported first, before the
# GUI-specific module (``qt.py``).  We use it to register the one config
# variable that is specific to *this* plugin.
#
# Note: the swap-server settings the user actually edits
# (``plugins.swapserver.port``, ``plugins.swapserver.fee_millionths``,
# ``plugins.swapserver.ann_pow_nonce``) are registered by Electrum's bundled
# ``swapserver`` plugin and are pulled in transitively when we import
# ``electrum.plugins.swapserver.server`` from :mod:`swapserver_gui`.  We must
# NOT register them a second time here: ``ConfigVar`` asserts that every config
# key is registered exactly once (see ``electrum/simple_config.py``).

from electrum.simple_config import ConfigVar, SimpleConfig

# Whether the swap server should be (re)started automatically the next time a
# lightning-enabled wallet is opened.  Toggled by the "Swap Server" tab.
#
# ``plugin`` must be the bare plugin name: when this plugin is loaded as an
# external zip its ``__name__`` is ``electrum_external_plugins.swapserver_gui``,
# which would fail ConfigVar's "no dots in plugin name" assertion.
SimpleConfig.SWAPSERVER_GUI_AUTOSTART = ConfigVar(
    'plugins.swapserver_gui.autostart',
    default=False,
    type_=bool,
    plugin='swapserver_gui',
)
