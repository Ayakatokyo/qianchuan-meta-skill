"""Runtime output path policy for keeping generated artifacts outside the skill."""

from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


class RuntimePathError(ValueError):
    """Raised when a generated artifact would be written into the skill tree."""


def external_runtime_path(value, *, label="runtime output"):
    """Resolve a path and reject targets inside the installed skill directory."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    absolute = path.absolute()
    resolved = absolute.resolve()
    skill_absolute = SKILL_ROOT.absolute()
    skill_resolved = SKILL_ROOT.resolve()
    for candidate, root in ((absolute, skill_absolute), (resolved, skill_resolved)):
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        break
    else:
        return absolute
    raise RuntimePathError(
        f"{label} must be outside the skill installation directory; "
        "use an explicit path under the current work object directory"
    )
