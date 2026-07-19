"""Ansible filters: assemble + project the multi-domain Cloudflare zone list.

The EE multi-domain overlay reconciles a canonical zone list from two sources
-- the CE install's primary domain (the CLOUDFLARE_ZONE scalars) and the
secondary domains the operator attached in the catena-admin Domains panel
(stored in config.CLOUDFLARE_ZONES + secrets.vault_cloudflare_api_tokens).
These filters do the list manipulation in Python (tested) rather than in
brittle nested-Jinja, matching the repo's Python-over-Jinja rule.

  cf_zones_merge_primary(primary, store_zones, tokens_map)
      -> the full [primary, secondary...] list, primary always element 0
         (secondaries de-duplicated against it), each element carrying its
         api_token (from the primary dict or the tokens map).
  cf_zones_strip_tokens(zones)  -> the same list with api_token removed
                                   (the world-readable panel projection).
  cf_zones_tokens_map(zones)    -> {zone: api_token} for the secret store.

End-to-end coverage:
    ansible/tests/unit/test_catena_multidomain.py
"""
from __future__ import annotations


def cf_zones_merge_primary(primary, store_zones, tokens_map):
    """Return the canonical tokened zone list: primary first, then each
    stored secondary (skipping any duplicate of the primary zone), with each
    element's api_token filled from the element itself or the tokens map."""
    if not isinstance(primary, dict) or not primary.get("zone"):
        raise ValueError("cf_zones_merge_primary: primary must be a dict with a 'zone'")
    tokens_map = tokens_map or {}
    primary_zone = primary["zone"]

    out = [{
        "zone": primary_zone,
        "account_id": primary.get("account_id", ""),
        "api_token": primary.get("api_token") or tokens_map.get(primary_zone, ""),
    }]
    for z in store_zones or []:
        if not isinstance(z, dict):
            continue
        name = z.get("zone")
        if not name or name == primary_zone:
            continue
        out.append({
            "zone": name,
            "account_id": z.get("account_id", ""),
            "api_token": z.get("api_token") or tokens_map.get(name, ""),
        })
    return out


def cf_zones_strip_tokens(zones):
    """The zone list with the api_token dropped (panel projection / store
    config -- never carries the secret)."""
    out = []
    for z in zones or []:
        if not isinstance(z, dict):
            continue
        out.append({"zone": z.get("zone", ""), "account_id": z.get("account_id", "")})
    return out


def cf_zones_tokens_map(zones):
    """{zone: api_token} for every zone carrying a non-empty token."""
    out = {}
    for z in zones or []:
        if not isinstance(z, dict):
            continue
        name = z.get("zone")
        token = z.get("api_token")
        if name and token:
            out[name] = token
    return out


class FilterModule:
    def filters(self):
        return {
            "cf_zones_merge_primary": cf_zones_merge_primary,
            "cf_zones_strip_tokens": cf_zones_strip_tokens,
            "cf_zones_tokens_map": cf_zones_tokens_map,
        }
