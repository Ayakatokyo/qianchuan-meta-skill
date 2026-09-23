#!/usr/bin/env python3
"""Resolve the report advertiser ID from a selected RPA authorization."""

import argparse
import json

from qianchuan_report_client import list_authorized_advertisers
from rpa_dynamic_client import fetch_connector_detail, list_accounts


def resolve_advertiser_id(api_shop_id, rpa_shop_id, *, rpa_accounts=None, api_authorizations=None):
    api_shop_id = str(api_shop_id or "").strip()
    rpa_shop_id = str(rpa_shop_id or "").strip()
    if not api_shop_id or not rpa_shop_id:
        raise ValueError("API shop ID and RPA authorization ID are required")
    response = rpa_accounts if rpa_accounts is not None else list_accounts(fetch_connector_detail())
    rows = response.get("data") if isinstance(response, dict) and response.get("success") is True else None
    if not isinstance(rows, list):
        raise ValueError("RPA authorization query has no usable account list")
    matches = [row for row in rows if isinstance(row, dict)
               and str(row.get("shop_id") or "").strip() == rpa_shop_id]
    if len(matches) != 1:
        raise ValueError("selected RPA authorization ID does not resolve to one account")
    advertiser_id = str(matches[0].get("account") or "").strip()
    if not (advertiser_id.isascii() and advertiser_id.isdecimal()):
        raise ValueError("selected RPA account is not a numeric advertiser ID")
    authorized = api_authorizations if api_authorizations is not None else list_authorized_advertisers()
    matches = [row for row in authorized if isinstance(row, dict)
               and str(row.get("shop_id") or "").strip() == api_shop_id
               and str(row.get("advertiser_id") or "").strip() == advertiser_id]
    if len(matches) != 1:
        raise ValueError("selected RPA account is not uniquely authorized under the API shop")
    return advertiser_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-id", required=True, help="api_shop.extra.shop_id")
    parser.add_argument("--rpa-shop-id", required=True, help="rpa_shop.extra.shop_id")
    args = parser.parse_args(argv)
    try:
        advertiser_id = resolve_advertiser_id(args.shop_id, args.rpa_shop_id)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps({"advertiserId": advertiser_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
