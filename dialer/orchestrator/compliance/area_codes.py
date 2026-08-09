"""Static NANP area-code map: state + primary IANA timezone + country.

WHY static data instead of a geo API: the calling-window rule ("9am-5pm in the
PROSPECT's local time, derived from area code") is a hard compliance block, so
it must work offline, deterministically, and fail closed. An unknown area code
returns None and the gate blocks the dial (UNKNOWN_AREA_CODE) — we never guess
a timezone.

Timezone policy for area codes that span two zones: per the module contract we
pick the MORE EASTERN zone, because with a 9am-5pm window the eastern
assumption ends calls an hour early for western residents (safe) and starts
them at 8am actual-local at worst — still inside the federal TCPA 8am-9pm
bounds. Codes where the second zone is a tiny population fringe keep their
overwhelming-majority zone; each such choice carries an inline comment.

Canadian codes map to country "CA" and Caribbean NANP to "other"; both are
blocked unless `allow_non_us_nanp`. US territories (PR/VI/GU/MP/AS) are
deliberately classified "other" — they are outside the continental service
audience this dialer targets, and several sit in zones with no US-state window
override semantics.

Toll-free (800/833/844/855/866/877/888) and premium (900) codes are omitted on
purpose: they carry no geography, a prospect record should never hold one, and
omission makes the gate block them (fail closed).

Data current as of early 2026 (includes recent overlays, marked inline).
Update this table when NANPA activates new codes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .phones import last_ten_digits


@dataclass(frozen=True)
class AreaCodeInfo:
    state: str | None
    tz: str | None
    country: str  # "US" | "CA" | "other"


AREA_CODES: dict[str, AreaCodeInfo] = {}


def _put(country: str, state: str | None, tz: str | None, *codes: str) -> None:
    for code in codes:
        # Import-time self-check: a duplicate assignment means the table has a
        # data error; refuse to load rather than silently keep one entry.
        if code in AREA_CODES:
            raise ValueError(f"duplicate area code in table: {code}")
        AREA_CODES[code] = AreaCodeInfo(state=state, tz=tz, country=country)


def _us(state: str, tz: str, *codes: str) -> None:
    _put("US", state, tz, *codes)


# --- United States ---------------------------------------------------------

_us("AL", "America/Chicago", "205", "251", "256", "334", "659", "938")
# 907 excludes the tiny Aleutian America/Adak sliver; Anchorage is primary.
_us("AK", "America/Anchorage", "907")
# Arizona does not observe DST (Navajo Nation within 928 does; Phoenix rule wins as primary).
_us("AZ", "America/Phoenix", "480", "520", "602", "623", "928")
_us("AR", "America/Chicago", "327", "479", "501", "870")  # 327: 870 overlay, in service 2025
_us("CA", "America/Los_Angeles",
    "209", "213", "279", "310", "323", "341", "350", "408", "415", "424",
    "442", "510", "530", "559", "562", "619", "626", "628", "650", "657",
    "661", "669", "707", "714", "738", "747", "760", "805", "818", "820",
    "831", "837", "840", "858", "909", "916", "925", "949", "951")
    # 350: 209 overlay (2023); 738: 562 overlay; 837: 530 overlay — recent.
_us("CO", "America/Denver", "303", "719", "720", "970", "983")
_us("CT", "America/New_York", "203", "475", "860", "959")
_us("DE", "America/New_York", "302")
_us("DC", "America/New_York", "202", "771")
_us("FL", "America/New_York",
    "239", "305", "321", "352", "386", "407", "448", "561", "645", "656",
    "689", "727", "754", "772", "786", "813", "863", "904", "941", "954")
# 850 (panhandle) spans Central/Eastern (Tallahassee is Eastern) -> eastern zone per policy.
_us("FL", "America/New_York", "850")
_us("GA", "America/New_York",
    "229", "404", "470", "478", "678", "686", "706", "762", "770", "912",
    "943")  # 943: Atlanta overlay (2023); 686: 706/762 overlay — recent, verify activation
_us("HI", "Pacific/Honolulu", "808")
# Idaho spans Mountain (south) / Pacific (north panhandle) -> eastern (Mountain) per policy.
_us("ID", "America/Denver", "208", "986")
_us("IL", "America/Chicago",
    "217", "224", "309", "312", "331", "447", "464", "618", "630", "708",
    "730", "773", "779", "815", "847", "872")  # 730: 618 overlay — recent
# Indiana: America/New_York is offset-identical to America/Indiana/* since 2006.
_us("IN", "America/New_York", "260", "317", "463", "574", "765")
_us("IN", "America/Chicago", "219")  # NW Indiana (Gary) is wholly Central
# 812/930 span Central (Evansville) / Eastern (Bloomington) -> eastern per policy.
_us("IN", "America/New_York", "812", "930")
_us("IA", "America/Chicago", "319", "515", "563", "641", "712")
# 620/785 have far-west Mountain fringes -> Central (eastern) per policy.
_us("KS", "America/Chicago", "316", "620", "785", "913")
_us("KY", "America/Chicago", "270", "364")  # western Kentucky is Central
# 606 has a small Central fringe in the southeast -> Eastern per policy.
_us("KY", "America/New_York", "502", "606", "859")
_us("LA", "America/Chicago", "225", "318", "337", "504", "985")
_us("ME", "America/New_York", "207")
_us("MD", "America/New_York", "240", "301", "410", "443", "667")
_us("MA", "America/New_York",
    "339", "351", "413", "508", "617", "774", "781", "857", "978")
_us("MI", "America/New_York",
    "231", "248", "269", "313", "517", "586", "616", "679", "734", "810",
    "947", "989")  # 679: 313 overlay — recent
# 906 (Upper Peninsula) spans: four western UP counties are Central -> Eastern per policy.
_us("MI", "America/New_York", "906")
_us("MN", "America/Chicago", "218", "320", "507", "612", "651", "763", "952")
_us("MS", "America/Chicago", "228", "601", "662", "769")
_us("MO", "America/Chicago",
    "235", "314", "417", "557", "573", "636", "660", "816",
    "975")  # 235 (417 overlay), 557 (314 overlay), 975 (816 overlay) — recent
_us("MT", "America/Denver", "406")
# 308 (western panhandle) has Mountain counties -> Central (eastern) per policy.
_us("NE", "America/Chicago", "308", "402", "531")
# 775 has a tiny Mountain fringe (West Wendover) -> Pacific stays primary.
_us("NV", "America/Los_Angeles", "702", "725", "775")
_us("NH", "America/New_York", "603")
_us("NJ", "America/New_York",
    "201", "551", "609", "640", "732", "848", "856", "862", "908", "973")
_us("NM", "America/Denver", "505", "575")
_us("NY", "America/New_York",
    "212", "315", "332", "347", "363", "516", "518", "585", "607", "631",
    "646", "680", "716", "718", "838", "845", "914", "917", "929", "934")
_us("NC", "America/New_York",
    "252", "336", "472", "704", "743", "828", "910", "919", "980",
    "984")  # 472: 910 overlay — recent
# 701: southwest ND is Mountain -> Central (eastern) per policy.
_us("ND", "America/Chicago", "701")
_us("OH", "America/New_York",
    "216", "220", "234", "283", "326", "330", "380", "419", "436", "440",
    "513", "567", "614", "740", "937")  # 283 (513 overlay), 436 (440 overlay) — recent
# 580 includes the Kenton sliver (Mountain) -> Central per policy.
_us("OK", "America/Chicago", "405", "539", "572", "580", "918")  # 572: 405 overlay — recent
# 541 has a Mountain fringe (Malheur County) -> Pacific stays primary.
_us("OR", "America/Los_Angeles", "458", "503", "541", "971")
_us("PA", "America/New_York",
    "215", "223", "267", "272", "412", "445", "484", "570", "582", "610",
    "717", "724", "814", "878")
_us("RI", "America/New_York", "401")
_us("SC", "America/New_York",
    "803", "821", "839", "843", "854", "864")  # 821: 803/839 overlay — recent
# 605: western SD (Rapid City) is Mountain, a genuine split -> Central (eastern) per policy.
_us("SD", "America/Chicago", "605")
# 423 spans (Marion County is Central) -> Eastern per policy.
_us("TN", "America/New_York", "423", "865")
# 931 has a small Eastern fringe -> Central stays primary.
_us("TN", "America/Chicago", "615", "629", "731", "901", "931")
_us("TX", "America/Chicago",
    "210", "214", "254", "281", "325", "346", "361", "409", "430", "469",
    "512", "682", "713", "726", "737", "806", "817", "830", "832", "903",
    "936", "940", "945", "956", "972", "979")
# 432: far-west Mountain counties, Midland/Odessa majority -> Central (eastern) per policy.
_us("TX", "America/Chicago", "432")
_us("TX", "America/Denver", "915")  # El Paso is wholly Mountain
_us("UT", "America/Denver", "385", "435", "801")
_us("VT", "America/New_York", "802")
_us("VA", "America/New_York",
    "276", "434", "540", "571", "703", "757", "804", "826",
    "948")  # 826 (804 overlay), 948 (757 overlay) — recent
_us("WA", "America/Los_Angeles", "206", "253", "360", "425", "509", "564")
_us("WV", "America/New_York", "304", "681")
_us("WI", "America/Chicago",
    "262", "353", "414", "534", "608", "715", "920")  # 353: 608 overlay — recent
_us("WY", "America/Denver", "307")

# --- Canada (country "CA"; blocked unless allow_non_us_nanp) ---------------
# `state` carries the province for operator context; tz included so a future
# Canadian rollout inherits window enforcement instead of silently skipping it.

_put("CA", "ON", "America/Toronto",
     "226", "249", "289", "343", "365", "382", "416", "437", "519", "548",
     "613", "647", "683", "705", "742", "753", "905")
# 807 (NW Ontario) spans Central/Eastern -> eastern per policy.
_put("CA", "ON", "America/Toronto", "807")
_put("CA", "QC", "America/Toronto",
     "263", "367", "418", "438", "450", "468", "514", "579", "581", "819", "873")
_put("CA", "BC", "America/Vancouver", "236", "250", "604", "672", "778")
_put("CA", "AB", "America/Edmonton", "368", "403", "587", "780", "825")
_put("CA", "SK", "America/Regina", "306", "474", "639")  # Saskatchewan: no DST
_put("CA", "MB", "America/Winnipeg", "204", "431", "584")
_put("CA", "NB", "America/Moncton", "428", "506")
_put("CA", "NS", "America/Halifax", "782", "902")  # 782/902 also serve PE
_put("CA", "NL", "America/St_Johns", "709", "879")
# 867 spans YT/NT/NU; Mountain (Yellowknife) is the plurality zone.
_put("CA", "NT", "America/Edmonton", "867")

# --- Caribbean & other NANP (country "other") ------------------------------
# tz left None for sovereign Caribbean states: they are never dialable by this
# system, and a None tz keeps the window check failing closed even if the
# country block is ever relaxed.

_put("other", None, None,
     "242",  # Bahamas
     "246",  # Barbados
     "264",  # Anguilla
     "268",  # Antigua & Barbuda
     "284",  # British Virgin Islands
     "345",  # Cayman Islands
     "441",  # Bermuda
     "473",  # Grenada
     "649",  # Turks & Caicos
     "658",  # Jamaica (876 overlay)
     "664",  # Montserrat
     "721",  # Sint Maarten
     "758",  # St. Lucia
     "767",  # Dominica
     "784",  # St. Vincent & the Grenadines
     "809",  # Dominican Republic
     "829",  # Dominican Republic
     "849",  # Dominican Republic
     "868",  # Trinidad & Tobago
     "869",  # St. Kitts & Nevis
     "876")  # Jamaica

# US territories: technically US, classified "other" for dialing purposes —
# outside the continental service audience (see module docstring).
_put("other", "PR", "America/Puerto_Rico", "787", "939")
_put("other", "VI", "America/Puerto_Rico", "340")   # USVI: same offset, canonical zone
_put("other", "MP", "Pacific/Saipan", "670")        # Northern Mariana Islands
_put("other", "GU", "Pacific/Guam", "671")          # Guam
_put("other", "AS", "Pacific/Pago_Pago", "684")     # American Samoa


def info_for_phone(phone_e164: str) -> AreaCodeInfo | None:
    """Look up the area code of a NANP number; None when unknown/unparseable."""
    national = last_ten_digits(phone_e164) if phone_e164 else None
    if national is None:
        return None
    return AREA_CODES.get(national[:3])
