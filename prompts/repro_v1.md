Write a pytest test that reproduces the crash described in the crash report.

The test must:
- fail on the current code with the same exception type, raised from the same library function
  as in the crash report;
- pass once the bug is fixed, without being changed, so it must not expect the exception;
  assert the correct behaviour instead;
- import the library the way its users do, and use no network, no files outside a temporary
  directory, and no extra packages besides pytest and the library's own dependencies.

Answer with exactly one fenced Python code block holding the whole test file:

```python
import pytest


def test_crash_is_fixed(): ...
```

Before the code block, explain in two or three sentences which input triggers the crash.
Do not include any other code block.
