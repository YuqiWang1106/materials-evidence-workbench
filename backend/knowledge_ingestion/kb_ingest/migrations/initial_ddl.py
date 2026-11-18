"""Keep the frozen physical table definitions independent of the ORM registry."""

_LAYOUTS = {
    "orm": ("\n\t", ", \n\t", "\n"),
    "indented": ("\n    ", ",\n    ", "\n"),
    "inline": ("", ", ", ""),
}


def table(name: str, *declarations: str, layout: str) -> str:
    prefix, separator, suffix = _LAYOUTS[layout]
    return f"CREATE TABLE {name} ({prefix}{separator.join(declarations)}{suffix})"
