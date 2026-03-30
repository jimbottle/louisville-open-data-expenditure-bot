"""
Canonical agency name mapping for Louisville Metro expenditure data.

Maps all known agency name variants to a single canonical name.
Used at data load time to add an `agency_canonical` column.
"""

AGENCY_MAP = {
    # Air Pollution
    "APCD": "Air Pollution Control District",
    "Air Pollution Control District": "Air Pollution Control District",

    # Alcohol/Beverage Control
    "Alcohol Beverage Control": "Alcoholic Beverage Control",
    "Alcoholic Beverage Control": "Alcoholic Beverage Control",

    # Brightside
    "Brightside": "Brightside",

    # Codes & Regulations
    "Codes & Regulations": "Codes & Regulations",
    "Codes & Regulations Department": "Codes & Regulations",
    "Codes and Regulations": "Codes & Regulations",

    # Community Services
    "Community Services": "Community Services & Revitalization",
    "Community Services & Revitalization": "Community Services & Revitalization",
    "Office of Resilience and Community Services": "Community Services & Revitalization",

    # Corrections
    "Department of Corrections": "Department of Corrections",
    "Metro Corrections": "Department of Corrections",

    # County Clerk
    "County Clerk": "County Clerk",

    # Criminal Justice
    "Criminal Justice Commission": "Criminal Justice Commission",

    # Debt Service
    "Debt Service": "Debt Service",

    # Develop Louisville / Economic Development
    "Develop Louisville": "Develop Louisville",
    "Economic Development": "Economic Development",
    "Economic Growth & Innovation": "Economic Development",

    # Elected Officials
    "Elected Officials": "Elected Officials",
    "Other Elected Officials": "Elected Officials",

    # Emergency Services / MetroSafe
    "ES & MetroSafe": "Emergency Services & MetroSafe",
    "ES/MetroSafe": "Emergency Services & MetroSafe",
    "Emergency Management Agency MetroSafe": "Emergency Services & MetroSafe",
    "Emergency Management Agency/MetroSafe": "Emergency Services & MetroSafe",
    "Emergency Management Services": "Emergency Services & MetroSafe",
    "Emergency Services": "Emergency Services & MetroSafe",

    # EMS
    "Emergency Medical Services": "Emergency Medical Services",
    "Metro EMS": "Emergency Medical Services",

    # Facilities
    "Facilities and Fleet Management": "Facilities & Fleet Management",

    # Fire & Police Pension
    "Fire & Police Pension": "Fire & Police Pension",

    # Group Violence Intervention
    "Group Violence Intervention": "Group Violence Intervention",

    # Housing
    "Housing & Family Services": "Office of Housing & Community Development",
    "Office of Housing & Community Development": "Office of Housing & Community Development",

    # Human Relations
    "Human Relations Commission": "Human Relations Commission",

    # Human Resources
    "Human Resources": "Human Resources",

    # Human Services
    "Human Services": "Human Services",
    "Office of Social Services": "Human Services",

    # Insurance
    "Insurance / Claims": "Insurance & Claims",

    # Jefferson County Attorney
    "Jefferson County Attorney": "Jefferson County Attorney",

    # Jefferson County Coroner
    "Jefferson County Coroner": "Jefferson County Coroner",

    # KentuckianaWorks
    "Kentuckiana Works": "KentuckianaWorks",
    "KentuckianaWorks": "KentuckianaWorks",

    # Library
    "Library": "Louisville Free Public Library",
    "Louisville Free Public Library": "Louisville Free Public Library",

    # Louisville Fire
    "Louisville Fire": "Louisville Fire",
    "Louisville Fire Department": "Louisville Fire",

    # Louisville Metro Council
    "Louisville Metro Council": "Louisville Metro Council",
    "Metro Council": "Louisville Metro Council",

    # Louisville Metro Police
    "Louisville Metro Police": "Louisville Metro Police Department",
    "Louisville Metro Police Department": "Louisville Metro Police Department",
    "Metro Police": "Louisville Metro Police Department",

    # Louisville Zoo
    "Louisville Zoo": "Louisville Zoo",

    # Mayor's Office
    "Mayor Office": "Mayor's Office",
    "Mayor's Office": "Mayor's Office",

    # Metro Animal Services
    "Metro Animal Services": "Metro Animal Services",

    # Metro TV
    "Metro TV": "Metro TV",

    # Neighborhoods
    "Neighborhoods": "Neighborhoods",
    "Neighborhoods Department": "Neighborhoods",
    "Neighborhoods Parks & Cultural Affairs Cabinet Secre": "Neighborhoods",

    # OMB / Budget
    "OMB Finance": "OMB Finance",
    "Office of Management & Budget": "OMB Finance",

    # Office of Civic Innovation / Technology
    "Department of Information Technology": "Metro Technology Services",
    "Metro Technology Services": "Metro Technology Services",
    "Office of Civic Innovation & Technology": "Metro Technology Services",
    "Office of Civic Innovation and Technology": "Metro Technology Services",
    "Technology Services": "Metro Technology Services",
    "Technology Services Department": "Metro Technology Services",

    # Office of Equity
    "Office of Equity": "Office of Equity",

    # Office of Inspector General
    "Office of Inspector General": "Office of Inspector General",

    # Office of Internal Audit
    "Office of Internal Audit": "Office of Internal Audit",

    # Office of Performance
    "Office of Performance Improvement": "Office of Performance Improvement",
    "Office of Performance Improvement & Innovation": "Office of Performance Improvement",

    # Office of Philanthropy
    "Office of Philanthropy": "Office of Philanthropy",

    # Office of Planning
    "Office of Planning": "Office of Planning",

    # Office of Safe & Healthy Neighborhoods
    "Office for Safe & Healthy Neighborhoods": "Office for Safe & Healthy Neighborhoods",

    # Office of Strategic Initiatives
    "Office of Strategic Initiatives": "Office of Strategic Initiatives",

    # Office of Sustainability
    "Office of Sustainability": "Office of Sustainability",

    # Office of Violence Prevention
    "Office of Violence Prevention": "Office of Violence Prevention",

    # Other Statutory Obligations
    "Other Statutory Obligations": "Other Statutory Obligations",

    # Parking Authority
    "Parking Authority of River City (PARC)": "Parking Authority (PARC)",
    "Parking Authority of River City - PARC": "Parking Authority (PARC)",

    # Parks & Recreation
    "Parks & Recreation": "Parks & Recreation",

    # Policy & Strategic Planning
    "Policy & Strategic Planning": "Policy & Strategic Planning",

    # Public Health
    "Public Health & Wellness": "Public Health & Wellness",

    # Public Protection
    "Public Protection": "Public Protection",
    "Public Protection Department": "Public Protection",

    # Public Works
    "Public Works & Assets": "Public Works & Assets",
    "Public Works & Assets Department": "Public Works & Assets",

    # Records Compliance
    "Records Compliance": "Records Compliance",

    # Related Agencies
    "Related Agencies": "Related Agencies",

    # Revenue Commission
    "Revenue Commission": "Revenue Commission",

    # Suburban Fire
    "Suburban Fire Districts": "Suburban Fire Districts",

    # Waterfront
    "Waterfront Development Corp": "Waterfront Development Corporation",
    "Waterfront Development Corporation": "Waterfront Development Corporation",

    # Youth Services
    "Youth Detention Services": "Youth Detention Services",
    "Youth Transitional Services": "Youth Transitional Services",
}


def normalize_agency(name: str) -> str:
    """Return canonical agency name, or original if no mapping exists."""
    if name is None:
        return None
    return AGENCY_MAP.get(name.strip(), name.strip())
