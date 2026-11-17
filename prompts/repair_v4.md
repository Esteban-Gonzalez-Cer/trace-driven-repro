Your fix applied and the library's existing tests still pass, but your crash check could not be
used. What happened when it ran on the current code, without your fix, is in the data block
below.

A crash check is only useful if, on the current code, `test_crash` raises the exception from the
crash report, from the function the crash report names. Look again at the crash report: the
values, types and calls in its frames show what input sets the crash off.

Send a corrected crash check: one pytest file in a single ```python code block, written as
before (`test_crash` and two or three neighbouring tests). Do not send edit blocks: your fix
stays as it is and is run with the corrected check. If you cannot write a check that reproduces
the crash, reply with only the words NO CHECK.
