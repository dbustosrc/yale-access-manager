"""Load public sources from stdin into an HA interpreter without remote files."""

import importlib.abc
import importlib.util
import json
import sys
import unittest

payload = json.load(sys.stdin)
sources = payload["sources"]


class MemoryModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in sources:
            return importlib.util.spec_from_loader(fullname, self, is_package=sources[fullname]["package"])
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = "<in-memory>/" + module.__name__.replace(".", "/") + ".py"
        if module.__name__ == "test_integration":
            module.TEST_ASSETS = payload["assets"]
        exec(compile(sources[module.__name__]["source"], module.__file__, "exec"), module.__dict__)


sys.meta_path.insert(0, MemoryModules())
if payload.get("authorized_live_temporary_test") is True:
    import asyncio
    import live_temporary_check
    try:
        asyncio.run(live_temporary_check.run(disable_enable_authorized=payload.get("authorized_disable_enable_test") is True))
    except Exception as exc:
        print(json.dumps({"stage": "live_check_failed", "error_type": type(exc).__name__}))
        sys.exit(1)
    sys.exit(0)
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName("test_integration"))
sys.exit(not result.wasSuccessful())
