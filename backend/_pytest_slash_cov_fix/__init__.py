"""Pytest plugin that rewrites --cov=backend/X to backend.X.

Coverage treats `--cov=backend/X` (slash form) as a module name with
literal slashes, which doesn't resolve to any importable module. This
plugin converts the slash form to the dotted form (``backend.X``) so
coverage routes it to ``source_pkgs`` and uses ``ModuleMatcher``. The
test shim at ``backend/tests/test_cc_switch.py`` aliases
``sys.modules['cc_switch']`` to the same module instance
as ``sys.modules['backend.cc_switch']``, so when the unit
tests call ``pcc.get_provider_config(...)`` the frame's ``__name__`` is
``backend.cc_switch`` and ``ModuleMatcher`` matches.

The rewrite is applied at three lifecycle points to be robust against
hook ordering with pytest-cov:
1. Monkey-patch ``coverage.Coverage.__init__`` to rewrite the ``source`` kwarg.
2. ``pytest_load_initial_conftests`` (tryfirst=True).
3. ``pytest_configure`` (trylast=True) patches the controller as a safety net.
"""
import os
import pytest
import coverage as cov_mod

LOG_FILE = "/tmp/slash_hook.log"
try:
    os.unlink(LOG_FILE)
except FileNotFoundError:
    pass
with open(LOG_FILE, "w") as _f:
    _f.write("PLUGIN LOADED\n")


def _is_relative_slash_form(v):
    return isinstance(v, str) and "/" in v and not v.startswith("/") and "." not in v


def _rewrite_slash_to_dotted(values):
    """Convert 'backend/X' -> 'backend.X' so coverage can match it as a module."""
    if not values:
        return values
    if isinstance(values, str):
        return _rewrite_slash_to_dotted([values])
    out = []
    changed = False
    for v in values:
        if _is_relative_slash_form(v):
            out.append(v.replace("/", "."))
            changed = True
        else:
            out.append(v)
    return out if changed else values


def _maybe_rewrite_kwargs_source(kwargs):
    src = kwargs.get("source")
    if src:
        items = src if isinstance(src, list) else [src]
        if any(_is_relative_slash_form(v) for v in items):
            new_src = _rewrite_slash_to_dotted(src)
            if new_src is not src:
                kwargs["source"] = new_src


# Monkey-patch Coverage to rewrite the source kwarg on init.
_orig_coverage_init = cov_mod.Coverage.__init__


def _patched_coverage_init(self, *args, **kwargs):
    _maybe_rewrite_kwargs_source(kwargs)
    _orig_coverage_init(self, *args, **kwargs)


cov_mod.Coverage.__init__ = _patched_coverage_init


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    cov_source = getattr(early_config.known_args_namespace, "cov_source", None)
    if cov_source:
        new_source = _rewrite_slash_to_dotted(cov_source)
        if new_source is not cov_source:
            early_config.known_args_namespace.cov_source = new_source


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    cov_plugin = config.pluginmanager.getplugin("_cov")
    if cov_plugin is None:
        return
    cov_source = getattr(cov_plugin.options, "cov_source", None)
    if cov_source and any(_is_relative_slash_form(v) for v in cov_source):
        new_source = _rewrite_slash_to_dotted(cov_source)
        if new_source is not cov_source:
            cov_plugin.options.cov_source = new_source
    if cov_plugin.cov_controller is not None:
        ctrl_source = getattr(cov_plugin.cov_controller, "cov_source", None)
        if ctrl_source and any(_is_relative_slash_form(v) for v in ctrl_source):
            new_source = _rewrite_slash_to_dotted(ctrl_source)
            if new_source is not ctrl_source:
                cov_plugin.cov_controller.cov_source = new_source
