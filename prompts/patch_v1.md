Fix the crash described in the crash report.

Make the smallest change to the library's source code that removes the cause of the crash and
keeps the library's existing behaviour for every other input. Fix the cause, not only the
symptom: do not catch and silence the exception unless that is the correct behaviour.

Rules for the change:
- Edit only library source files shown in the source data block. Do not edit or create tests,
  `conftest.py`, packaging or configuration files.
- At most 3 files and 40 changed lines in total.
- Do not special-case tests, test runners or the exact values from the crash report.

Write the change as one or more edit blocks, in this exact format:

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

Before the edit blocks, explain the cause in two or three sentences.
