"""Tests for the decision logic written for the pipeline: which groups an
employee gets from the department map, and the pieces around it. (The copied
UPN logic has its own file, test_upn_generation.py.)

Pure functions, no network.

Run from the repo root:  python -m pytest -q
"""

import string

from shared.rules import (
    display_name_for,
    groups_for,
    known_departments,
    managed_groups,
    temp_password,
)

CONFIG = {
    "tenant": {"domain": "d.com"},
    "groups": {
        "all_employees": ["all-staff"],
        "departments": {
            "Engineering": ["dept-eng", "vpn-users"],
            "Sales": ["dept-sales"],
        },
        "titles": {
            "Engineering Manager": ["managers"],
            "Sales Manager": ["managers"],
        },
    },
}


def person(department="Engineering", title="Software Engineer", **extra):
    return {"employee_id": "E1", "first_name": "Ada", "last_name": "Byron",
            "department": department, "title": title, **extra}


# --- groups_for ---------------------------------------------------------------

def test_groups_merge_everyone_department_and_title_in_order():
    assert groups_for(person(title="Engineering Manager"), CONFIG) == [
        "all-staff", "dept-eng", "vpn-users", "managers",
    ]


def test_a_title_with_no_entry_adds_nothing():
    assert groups_for(person(), CONFIG) == ["all-staff", "dept-eng", "vpn-users"]


def test_department_and_title_match_after_trimming_and_ignoring_case():
    assert groups_for(person(department="  sales ", title="SALES MANAGER"), CONFIG) == [
        "all-staff", "dept-sales", "managers",
    ]


def test_a_title_match_is_exact_not_a_substring():
    assert "managers" not in groups_for(person(title="Engineering Manager II"), CONFIG)


def test_an_unknown_department_gets_only_the_everyone_groups():
    assert groups_for(person(department="Legal", title=""), CONFIG) == ["all-staff"]


def test_a_group_named_twice_is_listed_once():
    config = {"groups": {
        "all_employees": ["shared"],
        "departments": {"Engineering": ["shared", "dept-eng"]},
        "titles": {},
    }}
    assert groups_for(person(), config) == ["shared", "dept-eng"]


def test_an_empty_map_means_no_groups():
    assert groups_for(person(), {}) == []


# --- the set the pipeline may touch ---------------------------------------------

def test_managed_groups_is_every_group_the_map_names_once():
    assert managed_groups(CONFIG) == ["all-staff", "dept-eng", "vpn-users", "dept-sales", "managers"]


def test_known_departments_are_lower_cased():
    assert known_departments(CONFIG) == {"engineering", "sales"}


# --- display name and password --------------------------------------------------

def test_display_name_is_the_persons_name():
    assert display_name_for(person(first_name="Chloé", last_name="Lefèvre")) == "Chloé Lefèvre"


def test_temp_password_meets_entra_complexity_and_is_not_repeated():
    passwords = {temp_password() for _ in range(50)}
    assert len(passwords) == 50
    for password in passwords:
        assert len(password) == 14
        assert any(c in string.ascii_uppercase for c in password)
        assert any(c in string.ascii_lowercase for c in password)
        assert any(c in string.digits for c in password)
        assert any(c in "!@#$%" for c in password)
