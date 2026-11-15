"""Decision logic: what an employee's account should look like.

Pure functions of an employee record and the configuration. The only network
access is pick_upn asking the Graph client which addresses are taken.

Adapted from provision.py in employee-provisioning-tool:

- Copied unchanged: sanitize_local and its transliteration table,
  temp_password, tenant_domain, and pick_upn's candidate ladder and
  collision checks. pick_upn reports through logging rather than print,
  since nobody is watching a console.
- Rewritten: groups_for and display_name_for. The CLI's versions are built
  around that tool's own business model (numbered sites, a head office,
  role-named accounts). Here the HR feed describes people by department and
  title, so groups come from a department map and the display name is the
  person's name.
- Left behind: licensing (the lab tenant has no licenses to assign) and
  everything tied to the CLI's hire file.
"""

import logging
import secrets
import string
import unicodedata

from shared.graph_api import GraphError

logger = logging.getLogger(__name__)


class ProvisionError(Exception):
    """A provisioning step can't proceed; the message says why."""


def temp_password():
    """Random temporary password meeting Entra's default complexity rules."""
    rng = secrets.SystemRandom()
    chars = (
        [rng.choice(string.ascii_uppercase) for _ in range(4)]
        + [rng.choice(string.ascii_lowercase) for _ in range(6)]
        + [rng.choice(string.digits) for _ in range(3)]
        + [rng.choice("!@#$%")]
    )
    rng.shuffle(chars)
    return "".join(chars)


def tenant_domain(config, required=True):
    domain = (config.get("tenant") or {}).get("domain")
    if not domain and required:
        raise ProvisionError("no UPN domain configured (tenant.domain)")
    return domain


# Letters that NFKD decomposition can't reduce to an ASCII base letter — they
# have no decomposition, so without this table they would simply vanish
# ("Øyvind Strauß" -> ystrau@, wrong initial and a cut-off surname).
TRANSLITERATIONS = str.maketrans({
    "ß": "ss", "ẞ": "SS",
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th",
    "ı": "i",
    "ħ": "h", "Ħ": "H",
    "ŧ": "t", "Ŧ": "T",
})


def sanitize_local(text):
    """Reduce a name fragment to the ASCII letters/digits a UPN allows.

    Accented letters lose their accents (NFKD, then drop the combining
    marks); letters with no accent form to fall back to are transliterated
    first. Anything else that isn't an ASCII letter or digit is dropped.
    """
    text = str(text).translate(TRANSLITERATIONS)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in ascii_text if ch.isalnum()).lower()


def pick_upn(client, hire, config):
    """Find the first available UPN for the hire's name.

    Tries first initial + last name, then two letters of the first name,
    three, and so on through the full first name; falls back to numbered
    variants if every letter-based one is taken.

    "Taken" means an existing UPN, or an address some user or group
    already receives mail at — a primary address, an alias in
    proxyAddresses or a mail nickname — since a UPN that collides with
    one of those would be an account whose mail lands somewhere else.
    """
    domain = tenant_domain(config)
    first = sanitize_local(hire["first_name"])
    last = sanitize_local(hire["last_name"])
    if not first and not last:
        raise ProvisionError("could not build a UPN from the employee's name")
    if first and last:
        stems = [f"{first[:i]}{last}" for i in range(1, len(first) + 1)]
    else:
        stems = [first or last]
    stems = list(dict.fromkeys(stems))
    candidates = stems + [f"{stems[-1]}{n}" for n in range(2, 10)]

    alias_check = True
    for local in candidates:
        upn = f"{local}@{domain}"
        existing = client.get_user(upn, "id,displayName")
        if existing is not None:
            holder = existing.get("displayName") or "existing account"
            logger.info("%s taken (%s) — trying next", upn, holder)
            continue
        if alias_check:
            try:
                holder = client.address_holder(local, domain)
            except GraphError as exc:
                # Still a usable answer: the UPN itself is free. But say so
                # loudly, since an alias collision would now go unnoticed.
                logger.warning(
                    "could not check whether %s is already an email alias (%s) "
                    "— verify in the admin center before relying on it", upn, exc,
                )
                alias_check = False
                holder = None
            if holder:
                logger.info("%s taken as an email address (%s) — trying next", upn, holder)
                continue
        return upn
    raise ProvisionError("no available UPN found after trying every variant")


def display_name_for(hire):
    """The account's display name: the person's name, as HR recorded it."""
    return f"{hire['first_name']} {hire['last_name']}".strip()


def groups_for(hire, config):
    """Every group the employee should be in, by name, from three sources in
    the department map (config["groups"]):

    - all_employees: groups everyone joins
    - departments.<department>: the department's own groups
    - titles.<title>: groups that follow a job title

    Department and title match exactly after trimming and ignoring case.
    Duplicates are collapsed, order preserved.
    """
    group_cfg = config.get("groups") or {}
    selected = list(group_cfg.get("all_employees") or [])

    for key, field in (("departments", "department"), ("titles", "title")):
        value = (hire.get(field) or "").strip().lower()
        if not value:
            continue
        for name, names in (group_cfg.get(key) or {}).items():
            if str(name).strip().lower() == value:
                selected += names or []

    return list(dict.fromkeys(selected))


def managed_groups(config):
    """Every group named anywhere in the department map.

    This is the full set the pipeline is allowed to touch. A membership in
    any other group was granted by someone else and is never removed.
    """
    group_cfg = config.get("groups") or {}
    selected = list(group_cfg.get("all_employees") or [])
    for key in ("departments", "titles"):
        for names in (group_cfg.get(key) or {}).values():
            selected += names or []
    return list(dict.fromkeys(selected))


def known_departments(config):
    """Department names the map has an entry for, lower-cased."""
    departments = (config.get("groups") or {}).get("departments") or {}
    return {str(name).strip().lower() for name in departments}
