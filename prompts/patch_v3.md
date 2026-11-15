Fix the crash described in the crash report.

Find what causes the crash and remove that cause. Make the smallest change to the library's
source code that does so and keeps the library's behaviour for every other input.

How to judge your fix:
1. Remove the mechanism that causes the bug. Do not let the bug happen and then undo its effect
   for one particular case.
2. Keep what the caller asked for. Arguments and options the caller passed, public attributes,
   return types and documented behaviour stay as they are. Never drop, override or weaken an
   argument to avoid the error.
3. Make the fix hold beyond the input in the crash report: think of neighbouring inputs such as
   empty values, None, other valid configurations and user-defined subclasses.
4. Fix it where it belongs. A conversion or an adapter is right at the interface where that
   conversion belongs; a special case added where the symptom shows usually is not.
5. Do not catch and silence the exception unless that is the correct behaviour.

Rules for the change:
- Edit only library source files shown in the source data block. Do not edit or create tests,
  `conftest.py`, packaging or configuration files.
- At most 3 files and 40 changed lines in total.
- Do not special-case tests, test runners or the exact values from the crash report.

Write the change as one or more edit blocks, in this exact format, outside any code fence:

FILE: path/to/file.py
<<<<<<< SEARCH
exact lines copied from the current file
=======
the lines that replace them
>>>>>>> REPLACE

- `FILE:` gives the path exactly as shown in the source block header, without line numbers.
- The SEARCH part must match the current file exactly, character for character, including
  indentation and blank lines. Copy the code only: the source block shows a line number and two
  spaces before each line; leave those out.
- Include enough lines in SEARCH that they appear only once in the file.
- Use several blocks for several places. Blocks for the same file apply in order.

After the edit blocks, add a crash check: one pytest file in a single ```python code block. It
is run on the current code and again with your fix, and you are told the result.
- A test named `test_crash` makes the call described in the crash report, so that on the
  current code it raises the reported exception from the reported function.
- Two or three more tests call the code with neighbouring inputs that should work after your
  fix (for example an empty value, None, or another valid configuration). Do not assert exact
  values you are unsure of; it is enough that these calls run without the reported error.
- Import the library as a user would. Do not read or write files, use the network, or start
  processes.
If you cannot write a check, leave it out: your fix is judged either way.

Before the edit blocks, explain the cause in two or three sentences.
