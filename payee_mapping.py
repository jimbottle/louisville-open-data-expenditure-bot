"""
Canonical payee/contractor name mapping for Louisville Metro expenditure data.

Maps common abbreviations, alternate names, and order number suffixes
to a single canonical name. Used at data load time to add a `payee_canonical` column.
"""

# Direct name mappings: variant -> canonical
PAYEE_MAP = {
    # Louisville Gas & Electric
    "LG&E": "Louisville Gas & Electric Company",
    "LOUISVILLE GAS & ELECTRIC COMPANY": "Louisville Gas & Electric Company",
    "LOUISVILLE GAS AND ELE": "Louisville Gas & Electric Company",

    # Humana
    "HUMANA": "Humana Health Plan Inc",
    "HUMANA INC": "Humana Health Plan Inc",
    "HUMANA HEALTH PLAN INC": "Humana Health Plan Inc",
    "Humana Insurance Company": "Humana Health Plan Inc",

    # Motorola
    "MOTOROLA SOLUTIONS INC": "Motorola Solutions Inc",
    "MOTOROLA COMMUNICATIONS ENTERPRISE": "Motorola Solutions Inc",
    "MOTOROLA SOLUTIONS ONL": "Motorola Solutions Inc",

    # Republic Services
    "REPUBLIC SERVICES OF KENTUCKY LLC": "Republic Services of Kentucky LLC",
    "REPUBLIC SERVICES TRAS": "Republic Services of Kentucky LLC",

    # Waste Management
    "WASTE MANAGEMENT OF KY LLC": "Waste Management of Kentucky LLC",

    # CDW (many order-number variants)
    "CDW LLC": "CDW LLC",

    # Hall Contracting
    "HALL CONTRACTING OF KENTUCKY INC": "Hall Contracting of Kentucky Inc",
    "HALL CONTRACTING CORPORATION": "Hall Contracting of Kentucky Inc",

    # Sullivan & Cozart
    "SULLIVAN & COZART INC": "Sullivan & Cozart Inc",
    "SULLIVAN AND COZART": "Sullivan & Cozart Inc",

    # Louisville Arena Authority
    "LOUISVILLE ARENA AUTHORITY INC": "Louisville Arena Authority Inc",

    # Louisville Paving
    "LOUISVILLE PAVING COMPANY INC": "Louisville Paving Company Inc",
    "LOUISVILLE PAVING CO": "Louisville Paving Company Inc",

    # Kentucky Retirement System
    "KENTUCKY RETIREMENT SYSTEM": "Kentucky Retirement Systems",
    "KENTUCKY RETIREMENT SYSTEMS": "Kentucky Retirement Systems",
    "KY RETIREMENT SYSTEM": "Kentucky Retirement Systems",

    # Louisville Water Company
    "LOUISVILLE WATER COMPANY": "Louisville Water Company",
    "LOUISVILLE WATER CO": "Louisville Water Company",

    # Dell
    "DELL MARKETING LP": "Dell Marketing LP",
    "DELL MARKETING L P": "Dell Marketing LP",

    # SHI
    "SHI INTERNATIONAL CORP": "SHI International Corp",
    "SHI INTERNATIONAL COR": "SHI International Corp",
}

# Prefix patterns: if a payee starts with this prefix, map to canonical
PAYEE_PREFIX_MAP = {
    "CDW GOVT #": "CDW LLC",
    "WALGREENS #": "Walgreens",
    "WALGREENS CO": "Walgreens",
    "WALGREENS HEALTH": "Walgreens",
}


def normalize_payee(name: str) -> str:
    """Return canonical payee name, or original if no mapping exists."""
    if name is None:
        return None
    stripped = name.strip()
    upper = stripped.upper()

    # Check direct mapping
    if upper in {k.upper(): k for k in PAYEE_MAP}:
        for k, v in PAYEE_MAP.items():
            if k.upper() == upper:
                return v

    # Check prefix patterns
    for prefix, canonical in PAYEE_PREFIX_MAP.items():
        if upper.startswith(prefix.upper()):
            return canonical

    return stripped
