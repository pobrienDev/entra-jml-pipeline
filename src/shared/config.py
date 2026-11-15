"""Loads the pipeline's configuration into the one dict the rules read.

Two sources: the department map, a JSON file in the repo that says which
groups each department and title gets; and the tenant's UPN domain, which is
never committed and is passed in by whoever starts the run.
"""

import json
from pathlib import Path

from shared.rules import ProvisionError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPARTMENT_MAP = REPO_ROOT / "config" / "department_groups.json"


def load_department_map(path=DEPARTMENT_MAP):
    """Read and shape-check the department map.

    A typo here decides which groups people land in, so anything that isn't
    the expected shape stops the run instead of being skipped.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProvisionError(f"cannot read the department map {path}: {exc}") from exc
    except ValueError as exc:
        raise ProvisionError(f"the department map {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProvisionError("the department map must be a JSON object")

    def group_list(value, where):
        if not isinstance(value, list) or not all(isinstance(g, str) and g.strip() for g in value):
            raise ProvisionError(f"department map: {where} must be a list of group names")
        return value

    group_list(data.get("all_employees", []), "all_employees")
    for key in ("departments", "titles"):
        section = data.get(key, {})
        if not isinstance(section, dict):
            raise ProvisionError(f"department map: {key} must be an object")
        for name, groups in section.items():
            group_list(groups, f"{key}.{name}")
    if not data.get("departments"):
        raise ProvisionError("department map: no departments defined")
    return {
        "all_employees": data.get("all_employees", []),
        "departments": data.get("departments", {}),
        "titles": data.get("titles", {}),
    }


def build_config(domain, usage_location="US", department_map=None):
    """The config dict the rules expect: tenant settings plus the map."""
    return {
        "tenant": {"domain": domain, "usage_location": usage_location},
        "groups": department_map if department_map is not None else load_department_map(),
    }
