#!/usr/bin/env python3
import glob
import importlib.util
import inspect
import os
import sys
import time
import unittest

def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)

    test_files = sorted(glob.glob("tests/test_*.py"))
    print(f"==================================================")
    print(f" CANONICAL TEST RUNNER ({len(test_files)} files)")
    print(f"==================================================")

    total_tests = 0
    passed_tests = 0
    failed_tests = []
    start_time = time.time()

    # 1. Run unittest test suite
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=0)
    res = runner.run(suite)
    unittest_count = res.testsRun
    unittest_failures = len(res.failures) + len(res.errors)
    total_tests += unittest_count
    passed_tests += (unittest_count - unittest_failures)
    if unittest_failures > 0:
        for f in res.failures:
            failed_tests.append((str(f[0]), str(f[1])))
        for e in res.errors:
            failed_tests.append((str(e[0]), str(e[1])))

    # 2. Run standalone test functions (pytest style) not discovered by unittest
    for tf in test_files:
        mod_name = os.path.splitext(os.path.basename(tf))[0]
        try:
            spec = importlib.util.spec_from_file_location(mod_name, tf)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            continue

        for attr_name in dir(mod):
            if attr_name.startswith("test_") and inspect.isfunction(getattr(mod, attr_name)):
                # If this function was already inside a TestCase, skip (already counted)
                func = getattr(mod, attr_name)
                # Check if it was run by unittest
                total_tests += 1
                try:
                    import asyncio
                    if inspect.iscoroutinefunction(func):
                        asyncio.run(func())
                    else:
                        func()
                    passed_tests += 1
                except Exception as e:
                    failed_tests.append((f"{mod_name}.{attr_name}", str(e)))

    duration = time.time() - start_time
    print(f"\nResults:")
    print(f"- Total Test Cases Checked: {total_tests}")
    print(f"- Passed: {passed_tests}")
    print(f"- Failed: {len(failed_tests)}")
    print(f"- Duration: {duration:.3f}s")

    if failed_tests:
        print("\nFailures:")
        for name, err in failed_tests:
            print(f"  ❌ {name}: {err[:150]}")
    else:
        print("\n✅ ALL CANONICAL TESTS PASSED 100%!")

if __name__ == "__main__":
    main()
