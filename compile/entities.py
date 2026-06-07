#!/usr/bin/env python3
"""
Entity resolution: a buyer string -> the canonical procuring ENTITY (org or state dept).

Woodpecker navigates by entity, not by portal. The raw buyer names are messy and
fragment a single org across many strings (BHEL alone appears as BHEL EDN, BHEL BHOPAL,
"Bharat Heavy Electricals Limited", BHEL JHANSI, BHEL-HYDERABAD…). A curated alias map
collapses the major PSUs to one id each ("one BHEL pill"); everything else is
auto-normalised. We also sniff a state name out of the buyer for the choropleth.
"""
import re

# (pattern on UPPERCASED buyer, entity_id, label). Order matters — first match wins, so
# specific/overloaded names (RASHTRIYA CHEMICALS before any rail 'RCF') come first.
_ALIAS_RAW = [
    (r"RASHTRIYA CHEMICALS|DEPARTMENT OF FERTILISER", "rcf", "Rashtriya Chemicals & Fertilizers"),
    (r"BHEL|BHARAT HEAVY ELECTRICAL", "bhel", "Bharat Heavy Electricals (BHEL)"),
    (r"NUCLEAR POWER CORP|\bNPCIL\b", "npcil", "Nuclear Power Corporation (NPCIL)"),
    (r"\bNTPC\b", "ntpc", "NTPC"),
    (r"NEYVELI|\bNLC\b", "nlc", "NLC India (Neyveli Lignite)"),
    (r"COAL INDIA|COALFIELD|BHARAT COKING", "coal-india", "Coal India & subsidiaries"),
    (r"NATIONAL ALUMINIUM|\bNALCO\b", "nalco", "National Aluminium (NALCO)"),
    (r"HINDUSTAN PETROLEUM|\bHPCL\b", "hpcl", "Hindustan Petroleum (HPCL)"),
    (r"BHARAT PETROLEUM|\bBPCL\b", "bpcl", "Bharat Petroleum (BPCL)"),
    (r"INDIAN OIL|\bIOCL\b", "iocl", "Indian Oil (IOCL)"),
    (r"OIL AND NATURAL GAS|\bONGC\b", "ongc", "ONGC"),
    (r"\bGAIL\b", "gail", "GAIL (India)"),
    (r"STEEL AUTHORITY|\bSAIL\b", "sail", "Steel Authority (SAIL)"),
    (r"\bNHPC\b", "nhpc", "NHPC"),
    (r"\bTHDC\b|TEHRI", "thdc", "THDC India"),
    (r"MILITARY ENGINEER|E-IN-C BRANCH|\bMES\b", "mes", "Military Engineer Services"),
    (r"IHQ OF MOD|MINISTRY OF DEFENCE|ORDNANCE FACTOR|\bDGQA\b", "mod", "Ministry of Defence"),
    (r"BORDER ROADS", "bro", "Border Roads Organisation"),
    (r"HINDUSTAN STEEL ?WORKS|\bHSCL\b", "hscl", "Hindustan Steelworks Construction"),
    (r"INDIAN INSTITUTE OF TECHNOLOGY|\bIIT\b", "iit", "Indian Institutes of Technology"),
    (r"\bITI L(IMITE|T)D\b", "iti", "ITI Limited"),
    (r"NATIONAL RURAL ROADS|\bNRRDA\b|PMGSY", "nrrda", "Rural Roads (NRRDA / PMGSY)"),
    # rail: clear rail tokens only (avoid bare RCF/MCF — overloaded with the fertiliser co)
    (r"\bRLY\b|RAILWAY|INTEGRAL COACH|\bICF\b|DIESEL LOCO|\bDLW\b|RAIL WHEEL|\bRWF\b|\bDMW\b"
     r"|RAIL COACH|RCF\W*RBL|KAPURTHALA|MODERN COACH|RAE ?BARELI|CHITTARANJAN|\bCLW\b", "railways", "Indian Railways"),
]
_ALIAS = [(re.compile(p), i, l) for p, i, l in _ALIAS_RAW]

_STATES = {
    "ANDHRA": "Andhra Pradesh", "ARUNACHAL": "Arunachal Pradesh", "ASSAM": "Assam",
    "BIHAR": "Bihar", "CHHATTISGARH": "Chhattisgarh", "GOA": "Goa", "GUJARAT": "Gujarat",
    "HARYANA": "Haryana", "HIMACHAL": "Himachal Pradesh", "JHARKHAND": "Jharkhand",
    "KARNATAKA": "Karnataka", "KERALA": "Kerala", "MADHYA PRADESH": "Madhya Pradesh",
    "MAHARASHTRA": "Maharashtra", "MANIPUR": "Manipur", "MEGHALAYA": "Meghalaya",
    "MIZORAM": "Mizoram", "NAGALAND": "Nagaland", "ODISHA": "Odisha", "ORISSA": "Odisha",
    "PUNJAB": "Punjab", "RAJASTHAN": "Rajasthan", "SIKKIM": "Sikkim", "TAMIL NADU": "Tamil Nadu",
    "TELANGANA": "Telangana", "TRIPURA": "Tripura", "UTTAR PRADESH": "Uttar Pradesh",
    "UTTARAKHAND": "Uttarakhand", "WEST BENGAL": "West Bengal", "DELHI": "Delhi",
    "PUDUCHERRY": "Puducherry", "JAMMU": "Jammu & Kashmir",
}
_SUFFIX = re.compile(r"\b(LTD|LIMITED|PVT|PRIVATE|CORP|CORPORATION|COMPANY|CO|INDIA|OF|THE|AND)\b")
_SLUG = re.compile(r"[^a-z0-9]+")

def _state(up):
    for k, v in _STATES.items():
        if k in up:
            return v
    return None

def entity_of(buyer_name):
    """-> {id, label, central(bool), state(str|None)}."""
    name = (buyer_name or "").strip()
    up = name.upper()
    state = _state(up)
    for rx, eid, label in _ALIAS:
        if rx.search(up):
            return {"id": eid, "label": label, "central": state is None, "state": state}
    head = re.split(r"[/|,(]", name)[0]
    norm = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", _SUFFIX.sub(" ", head.upper()))).strip()
    norm = norm or up.strip() or "unknown"
    eid = "org-" + (_SLUG.sub("-", norm.lower()).strip("-") or "unknown")
    label = " ".join(w.capitalize() for w in norm.split())[:60] or name[:60]
    return {"id": eid, "label": label, "central": state is None, "state": state}


# portal id -> state, for archive/scraper sources whose buyer strings don't name the state
# (e.g. an Assam PWD record whose buyer is just "Public Works Roads Department"). The entity
# step uses this as a fallback when entity_of() couldn't sniff a state from the buyer.
PORTAL_STATE = {
    "assam": "Assam", "assam_archive": "Assam",
    "himachal": "Himachal Pradesh", "himachal_archive": "Himachal Pradesh",
    "arunachal": "Arunachal Pradesh", "bihar_pmgsy": "Bihar", "delhi": "Delhi",
    "goa": "Goa", "gujarat": "Gujarat", "haryana": "Haryana",
    "jammukashmir": "Jammu & Kashmir", "jharkhand": "Jharkhand", "kerala": "Kerala",
    "madhyapradesh": "Madhya Pradesh", "maharashtra": "Maharashtra", "manipur": "Manipur",
    "meghalaya": "Meghalaya", "mizoram": "Mizoram", "nagaland": "Nagaland",
    "odisha": "Odisha", "puducherry": "Puducherry", "punjab": "Punjab",
    "rajasthan": "Rajasthan", "sikkim": "Sikkim", "tamilnadu": "Tamil Nadu",
    "tripura": "Tripura", "uttarakhand": "Uttarakhand", "uttarpradesh": "Uttar Pradesh",
    "westbengal": "West Bengal",
}

def portal_state(portal_id):
    return PORTAL_STATE.get(portal_id)
