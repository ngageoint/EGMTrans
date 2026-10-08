"""Producer nation codes of the DSI producer code (MIL-PRF-89020B 3.13.4.1 i).

"The first two characters (left justified) indicate the producing nation and
are from FIPS 10-4 Countries, Dependencies, Areas of Special Sovereignty and
Their Principal Administrative Divisions." FIPS 10-4 codes are not ISO 3166
codes, and four ISO codes partners are likely to type are other countries in
FIPS 10-4, so the validator warns on an unknown code and warns loudly on
those collisions. The specification's own example table lists Belgium BE,
France FR, United Germany GE, Italy IT, Netherlands NL, Norway NO, Spain SP,
United Kingdom UK and United States US; GE is the pre-unification code that
FIPS 10-4 replaced by GM, and it is accepted here because the specification
prints it.

The table is the FIPS 10-4 list as last published (with the later GEC
additions such as Serbia RI, Montenegro MJ, Kosovo KV and South Sudan OD),
transcribed from the public list of FIPS country codes; it is reference
data for a warning, not an authority on borders or names.
"""

from __future__ import annotations

# Two-letter FIPS 10-4 code: entity name.
FIPS_10_4: dict[str, str] = {
    'AA': 'Aruba', 'AC': 'Antigua and Barbuda', 'AE': 'United Arab Emirates', 'AF': 'Afghanistan',
    'AG': 'Algeria', 'AJ': 'Azerbaijan', 'AL': 'Albania', 'AM': 'Armenia', 'AN': 'Andorra', 'AO': 'Angola',
    'AQ': 'American Samoa', 'AR': 'Argentina', 'AS': 'Australia', 'AT': 'Ashmore and Cartier Islands',
    'AU': 'Austria', 'AV': 'Anguilla', 'AX': 'Akrotiri', 'AY': 'Antarctica',
    'BA': 'Bahrain', 'BB': 'Barbados', 'BC': 'Botswana', 'BD': 'Bermuda', 'BE': 'Belgium', 'BF': 'Bahamas, The',
    'BG': 'Bangladesh', 'BH': 'Belize', 'BK': 'Bosnia and Herzegovina', 'BL': 'Bolivia', 'BM': 'Burma',
    'BN': 'Benin', 'BO': 'Belarus', 'BP': 'Solomon Islands', 'BQ': 'Navassa Island', 'BR': 'Brazil',
    'BS': 'Bassas da India', 'BT': 'Bhutan', 'BU': 'Bulgaria', 'BV': 'Bouvet Island', 'BX': 'Brunei',
    'BY': 'Burundi',
    'CA': 'Canada', 'CB': 'Cambodia', 'CD': 'Chad', 'CE': 'Sri Lanka', 'CF': 'Congo (Brazzaville)',
    'CG': 'Congo (Kinshasa)', 'CH': 'China', 'CI': 'Chile', 'CJ': 'Cayman Islands',
    'CK': 'Cocos (Keeling) Islands', 'CM': 'Cameroon', 'CN': 'Comoros', 'CO': 'Colombia',
    'CQ': 'Northern Mariana Islands', 'CR': 'Coral Sea Islands', 'CS': 'Costa Rica',
    'CT': 'Central African Republic', 'CU': 'Cuba', 'CV': 'Cape Verde', 'CW': 'Cook Islands', 'CY': 'Cyprus',
    'DA': 'Denmark', 'DJ': 'Djibouti', 'DO': 'Dominica', 'DQ': 'Jarvis Island', 'DR': 'Dominican Republic',
    'DX': 'Dhekelia',
    'EC': 'Ecuador', 'EG': 'Egypt', 'EI': 'Ireland', 'EK': 'Equatorial Guinea', 'EN': 'Estonia', 'ER': 'Eritrea',
    'ES': 'El Salvador', 'ET': 'Ethiopia', 'EU': 'Europa Island', 'EZ': 'Czechia',
    'FG': 'French Guiana', 'FI': 'Finland', 'FJ': 'Fiji', 'FK': 'Falkland Islands (Islas Malvinas)',
    'FM': 'Micronesia, Federated States of', 'FO': 'Faroe Islands', 'FP': 'French Polynesia', 'FQ': 'Baker Island',
    'FR': 'France', 'FS': 'French Southern and Antarctic Lands',
    'GA': 'Gambia, The', 'GB': 'Gabon', 'GG': 'Georgia', 'GH': 'Ghana', 'GI': 'Gibraltar', 'GJ': 'Grenada',
    'GK': 'Guernsey', 'GL': 'Greenland', 'GM': 'Germany', 'GO': 'Glorioso Islands', 'GP': 'Guadeloupe',
    'GQ': 'Guam', 'GR': 'Greece', 'GT': 'Guatemala', 'GV': 'Guinea', 'GY': 'Guyana', 'GZ': 'Gaza Strip',
    'HA': 'Haiti', 'HK': 'Hong Kong', 'HM': 'Heard Island and McDonald Islands', 'HO': 'Honduras',
    'HQ': 'Howland Island', 'HR': 'Croatia', 'HU': 'Hungary',
    'IC': 'Iceland', 'ID': 'Indonesia', 'IM': 'Isle of Man', 'IN': 'India', 'IO': 'British Indian Ocean Territory',
    'IP': 'Clipperton Island', 'IR': 'Iran', 'IS': 'Israel', 'IT': 'Italy', 'IV': "Cote d'Ivoire", 'IZ': 'Iraq',
    'JA': 'Japan', 'JE': 'Jersey', 'JM': 'Jamaica', 'JN': 'Jan Mayen', 'JO': 'Jordan', 'JQ': 'Johnston Atoll',
    'JU': 'Juan de Nova Island',
    'KE': 'Kenya', 'KG': 'Kyrgyzstan', 'KN': 'Korea, North', 'KQ': 'Kingman Reef', 'KR': 'Kiribati',
    'KS': 'Korea, South', 'KT': 'Christmas Island', 'KU': 'Kuwait', 'KV': 'Kosovo', 'KZ': 'Kazakhstan',
    'LA': 'Laos', 'LE': 'Lebanon', 'LG': 'Latvia', 'LH': 'Lithuania', 'LI': 'Liberia', 'LO': 'Slovakia',
    'LQ': 'Palmyra Atoll', 'LS': 'Liechtenstein', 'LT': 'Lesotho', 'LU': 'Luxembourg', 'LY': 'Libya',
    'MA': 'Madagascar', 'MB': 'Martinique', 'MC': 'Macau', 'MD': 'Moldova', 'MF': 'Mayotte', 'MG': 'Mongolia',
    'MH': 'Montserrat', 'MI': 'Malawi', 'MJ': 'Montenegro', 'MK': 'North Macedonia', 'ML': 'Mali', 'MN': 'Monaco',
    'MO': 'Morocco', 'MP': 'Mauritius', 'MQ': 'Midway Islands', 'MR': 'Mauritania', 'MT': 'Malta', 'MU': 'Oman',
    'MV': 'Maldives', 'MX': 'Mexico', 'MY': 'Malaysia', 'MZ': 'Mozambique',
    'NC': 'New Caledonia', 'NE': 'Niue', 'NF': 'Norfolk Island', 'NG': 'Niger', 'NH': 'Vanuatu', 'NI': 'Nigeria',
    'NL': 'Netherlands', 'NN': 'Sint Maarten', 'NO': 'Norway', 'NP': 'Nepal', 'NR': 'Nauru', 'NS': 'Suriname',
    'NT': 'Netherlands Antilles', 'NU': 'Nicaragua', 'NZ': 'New Zealand',
    'OD': 'South Sudan',
    'PA': 'Paraguay', 'PC': 'Pitcairn Islands', 'PE': 'Peru', 'PF': 'Paracel Islands', 'PG': 'Spratly Islands',
    'PJ': 'Etorofu, Habomai, Kunashiri, and Shikotan Islands', 'PK': 'Pakistan', 'PL': 'Poland', 'PM': 'Panama',
    'PO': 'Portugal', 'PP': 'Papua New Guinea', 'PS': 'Palau', 'PU': 'Guinea-Bissau',
    'QA': 'Qatar',
    'RE': 'Reunion', 'RI': 'Serbia', 'RM': 'Marshall Islands', 'RN': 'Saint Martin', 'RO': 'Romania',
    'RP': 'Philippines', 'RQ': 'Puerto Rico', 'RS': 'Russia', 'RW': 'Rwanda',
    'SA': 'Saudi Arabia', 'SB': 'Saint Pierre and Miquelon', 'SC': 'Saint Kitts and Nevis', 'SE': 'Seychelles',
    'SF': 'South Africa', 'SG': 'Senegal', 'SH': 'Saint Helena, Ascension, and Tristan da Cunha', 'SI': 'Slovenia',
    'SL': 'Sierra Leone', 'SM': 'San Marino', 'SN': 'Singapore', 'SO': 'Somalia', 'SP': 'Spain',
    'ST': 'Saint Lucia', 'SU': 'Sudan', 'SV': 'Svalbard', 'SW': 'Sweden',
    'SX': 'South Georgia and South Sandwich Islands', 'SY': 'Syria', 'SZ': 'Switzerland',
    'TB': 'Saint Barthelemy', 'TD': 'Trinidad and Tobago', 'TE': 'Tromelin Island', 'TH': 'Thailand',
    'TI': 'Tajikistan', 'TK': 'Turks and Caicos Islands', 'TL': 'Tokelau', 'TN': 'Tonga', 'TO': 'Togo',
    'TP': 'Sao Tome and Principe', 'TS': 'Tunisia', 'TT': 'Timor-Leste', 'TU': 'Turkey', 'TV': 'Tuvalu',
    'TW': 'Taiwan', 'TX': 'Turkmenistan', 'TZ': 'Tanzania',
    'UC': 'Curacao', 'UG': 'Uganda', 'UK': 'United Kingdom', 'UP': 'Ukraine', 'US': 'United States',
    'UV': 'Burkina Faso', 'UY': 'Uruguay', 'UZ': 'Uzbekistan',
    'VC': 'Saint Vincent and the Grenadines', 'VE': 'Venezuela', 'VI': 'British Virgin Islands', 'VM': 'Vietnam',
    'VQ': 'Virgin Islands (U.S.)', 'VT': 'Vatican City (Holy See)',
    'WA': 'Namibia', 'WE': 'West Bank', 'WF': 'Wallis and Futuna', 'WI': 'Western Sahara', 'WQ': 'Wake Island',
    'WS': 'Samoa', 'WZ': 'Eswatini',
    'YM': 'Yemen',
    'ZA': 'Zambia', 'ZI': 'Zimbabwe',
}

# Codes the specification's example table prints (3.13.4.1 i), accepted as
# producer nation codes whatever FIPS 10-4 says today.
SPEC_EXAMPLE_CODES: dict[str, str] = {
    'BE': 'Belgium', 'FR': 'France', 'GE': 'United Germany', 'IT': 'Italy', 'NL': 'Netherlands',
    'NO': 'Norway', 'SP': 'Spain', 'UK': 'United Kingdom', 'US': 'United States',
}

# ISO 3166 codes that are other countries in FIPS 10-4, or no code at all:
# (what the code means in FIPS 10-4, the FIPS 10-4 code of the country the
# producer most likely meant). The list is agreed with the partners.
COLLISIONS: dict[str, tuple[str | None, str]] = {
    'AU': ('Austria', 'Australia is AS'),
    'GB': ('Gabon', 'the United Kingdom is UK'),
    'SE': ('Seychelles', 'Sweden is SW'),
    'CH': ('China', 'Switzerland is SZ'),
    'DE': (None, "Germany is GM (the specification's example table writes GE)"),
}


def nation_code(producer_code: str) -> str:
    """The producer nation code: the first two characters of the field."""
    return (producer_code or '')[:2].upper()


def producer_code_warning(producer_code: str) -> str | None:
    """Why the nation code at the start of *producer_code* deserves a warning,
    or None when it is a FIPS 10-4 code (or one the specification prints)."""
    code = nation_code(producer_code)
    if len(code) < 2 or not code.isalpha():
        return None  # the shape is reported by the validator itself
    if code in COLLISIONS:
        meaning, intended = COLLISIONS[code]
        if meaning is None:
            return f'{code} is not a FIPS 10-4 country code; {intended} (3.13.4.1 i)'
        return (f'{code} is {meaning} in FIPS 10-4, which DTED producer codes use; {intended}. '
                f'Check the producer code (3.13.4.1 i)')
    if code in FIPS_10_4 or code in SPEC_EXAMPLE_CODES:
        return None
    return f'{code} is not a FIPS 10-4 country code (3.13.4.1 i)'


def nation_name(producer_code: str) -> str | None:
    """The entity a producer code's nation code names, when it is known."""
    code = nation_code(producer_code)
    return FIPS_10_4.get(code) or SPEC_EXAMPLE_CODES.get(code)
