"""A jinja2 environment with the few Ansible filters and tests the validate task
files use, so a test can evaluate an assert's expressions against sample facts.
"""
from __future__ import annotations

import base64
import json
import re

import jinja2
import yaml


def env() -> jinja2.Environment:
    e = jinja2.Environment()
    e.filters["bool"] = lambda v: str(v).lower() in ("true", "1", "yes")
    e.filters["b64decode"] = lambda v: base64.b64decode(v).decode()
    e.filters["from_json"] = json.loads
    e.filters["from_yaml"] = yaml.safe_load
    e.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    e.tests["match"] = lambda v, pat: re.match(pat, v) is not None
    return e


def holds(task: dict, key: str, **ctx: object) -> bool:
    """Whether every `that` expression of an assert task holds."""
    return all(env().compile_expression(str(expr))(**ctx)
               for expr in task[key]["that"])


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()
