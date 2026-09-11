"""Shared lookup tables: states and the NPI taxonomy names behind each
specialty label.

These used to live inside the Tkinter window class, which meant the CLI could
not see them and nothing could be tested without opening a window.
"""

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR",
}

STATE_ABBR_TO_NAME = {abbr: name for name, abbr in STATES.items()}

# Label shown in the UI -> the taxonomy description the NPI registry indexes on.
SPECIALTIES = {
    "Family Medicine": "Family Medicine",
    "Internal Medicine": "Internal Medicine",
    "Pediatrics": "Pediatrics",
    "Psychiatry": "Psychiatry",
    "Psychologist": "Psychologist",
    "Clinical Social Worker": "Clinical Social Worker",
    "Nurse Practitioner": "Nurse Practitioner",
    "Nurse Practitioner - Family": "Nurse Practitioner, Family",
    "Nurse Practitioner - Psychiatric/Mental Health":
        "Nurse Practitioner, Psychiatric/Mental Health",
    "Mental Health Counselor": "Counselor, Mental Health",
    "Mental Health Clinic":
        "Clinic/Center, Mental Health (Including Community Mental Health Center)",
    "Orthopedics": "Orthopaedic Surgery",
    "Cardiology": "Cardiovascular Disease",
    "OB-GYN": "Obstetrics & Gynecology",
    "Physical Therapy": "Physical Therapist",
    "Chiropractic": "Chiropractor",
    "Urgent Care": "Clinic/Center, Urgent Care",
    "General Practice": "General Practice",
    "Neurology": "Neurology",
    "Dermatology": "Dermatology",
    "Gastroenterology": "Gastroenterology",
}


def resolve_state(value):
    """Accept "Texas", "texas" or "TX" and return (full_name, abbreviation)."""
    if not value:
        return None, None
    text = str(value).strip()
    for name, abbr in STATES.items():
        if text.lower() == name.lower():
            return name, abbr
    upper = text.upper()
    if upper in STATE_ABBR_TO_NAME:
        return STATE_ABBR_TO_NAME[upper], upper
    return None, None
