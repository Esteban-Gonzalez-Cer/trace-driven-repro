If the code you need to change, or code you need to understand the crash, is in a library file
that is not shown, ask for it before you answer instead of guessing what it does. Reply with
only this line instead of a fix:

REQUEST FILES: path/to/one.py, path/to/other.py::ClassName.method, path/to/other.py::function

- Ask when the cause seems to lie outside the code shown, or when your fix would rest on a
  function, class or pattern whose code you cannot see.
- At most 3 library source files, with paths as the package lays them out (for example
  `src/package/module.py`). Tests, configuration and packaging files are never shown.
- A whole file is shown only if it fits. For a large file, or one already shown as an outline,
  name what you need: `path::function`, `path::ClassName` or `path::ClassName.method`. Up to 10
  such names, from at most 3 files.
- You get one request. If nothing new could be shown, you are told so and may ask once more.
  After that, give your fix with what you have.
