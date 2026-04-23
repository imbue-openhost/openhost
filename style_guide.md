
- use frozen attrs classes, not dataclasses. use `attr.s(auto_attribs=True, frozen=True).
- don't write functions that return dictionaries; return an attrs class instead (unless a dictionary is clearly better for some reason)
- prefer to write code that fails loudly, vs continuing in a best-effort manner. eg if you expect an env var to be set, fail if it doesn't exist instead of silently just falling back to a default value. the latter is harder to debug and causes unpleasant surprises.
- don't write file-level docstrings unless the file is particularly confusing or the intent is especially obscure. these just get out of date and confusing. just keep the file simple and readable and concise instead.
- prefer many small code files, well organized in folders, than fewer long files.
