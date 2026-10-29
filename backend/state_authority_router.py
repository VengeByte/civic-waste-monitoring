"""
State and Municipal ULB Authority Directory and Fallback Resolver.

Provides countrywide municipal authority resolution based on state and district/city
extracted from OpenStreetMap Nominatim reverse geocoding.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("backend.state_authority_router")

DATA_FILE = Path(__file__).resolve().parent / "data" / "scraped_municipal_emails.csv"

# Well-known official nodal state municipal emails (curated from scraped sources)
STATE_NODAL_DIRECTORIES: dict[str, dict[str, Any]] = {
    "Bihar": {
        "name": "Urban Development & Housing Department, Bihar",
        "primary_email": "cs-bihar@nic.in",
        "cc_emails": ["feedback.commonportal@gmail.com"],
        "website": "https://state.bihar.gov.in/urban",
        "helpline": "1800-3456-112",
    },
    "Chandigarh": {
        "name": "Municipal Corporation Chandigarh",
        "primary_email": "mohmcc-chd@nic.in",
        "cc_emails": ["cemcmcc-chd@nic.in", "commissionermcc@gmail.com"],
        "website": "https://mcchandigarh.gov.in",
        "helpline": "0172-2787200",
    },
    "Chhattisgarh": {
        "name": "Urban Administration & Development Department, Chhattisgarh",
        "primary_email": "uad.cg@gov.in",
        "cc_emails": ["ceo-suda@cg.gov.in", "adf.uad.cg@nic.in"],
        "website": "https://uad.cg.gov.in",
        "helpline": "1100",
    },
    "Gujarat": {
        "name": "Urban Development & Urban Housing Department, Gujarat",
        "primary_email": "securban@gujarat.gov.in",
        "cc_emails": ["sec-hn-urban@gujarat.gov.in"],
        "website": "https://udd.gujarat.gov.in",
        "helpline": "1800-233-0000",
    },
    "Himachal Pradesh": {
        "name": "Directorate of Urban Development, Himachal Pradesh",
        "primary_email": "ud-hp@nic.in",
        "cc_emails": ["mcsml-hp@nic.in"],
        "website": "https://ud.hp.gov.in",
        "helpline": "1100",
    },
    "Jharkhand": {
        "name": "Urban Development & Housing Department, Jharkhand",
        "primary_email": "director.ma.goj@gmail.com",
        "cc_emails": ["ud.secy@gmail.com", "suda.goj@gmail.com"],
        "website": "https://udhd.jharkhand.gov.in",
        "helpline": "181",
    },
    "Kerala": {
        "name": "Local Self Government Department, Kerala",
        "primary_email": "splsecy.lsgd@kerala.gov.in",
        "cc_emails": ["prlsecylsgd@gmail.com"],
        "website": "https://lsgkerala.gov.in",
        "helpline": "1800-425-1089",
    },
    "Madhya Pradesh": {
        "name": "Urban Administration & Development Directorate, Madhya Pradesh",
        "primary_email": "cmamp14@mpurban.gov.in",
        "cc_emails": ["lsbaghel.mpuad@gmail.com"],
        "website": "https://mpurban.gov.in",
        "helpline": "181",
    },
    "Odisha": {
        "name": "Housing & Urban Development Department, Odisha",
        "primary_email": "hudsec.or@od.gov.in",
        "cc_emails": ["dmahudd@gmail.com", "diraddlsecretary@gmail.com"],
        "website": "https://urban.odisha.gov.in",
        "helpline": "155304",
    },
    "Uttar Pradesh": {
        "name": "Urban Development Department, Uttar Pradesh",
        "primary_email": "dep_urban@up.gov.in",
        "cc_emails": ["diruplb@nic.in", "minister.ude@up.gov.in"],
        "website": "https://urbandevelopment.up.nic.in",
        "helpline": "1533",
    },
    "Uttarakhand": {
        "name": "Urban Development Directorate, Uttarakhand",
        "primary_email": "email-directorudd@gmail.com",
        "cc_emails": [],
        "website": "https://udd.uk.gov.in",
        "helpline": "1905",
    },
    "Haryana": {
        "name": "Directorate of Urban Local Bodies, Haryana",
        "primary_email": "dulbhry@gmail.com",
        "cc_emails": ["ulbharyana@gmail.com"],
        "website": "https://ulbharyana.gov.in",
        "helpline": "1800-180-2026",
    },
    "Delhi": {
        "name": "Municipal Corporation of Delhi (MCD)",
        "primary_email": "commissioner-mcd@mcd.nic.in",
        "cc_emails": ["feedback-mcd@mcd.nic.in"],
        "website": "https://mcdonline.nic.in",
        "helpline": "155305",
    },
}

# City keyword mappings to specific municipality contacts
CITY_KEYWORD_DIRECTORY: list[dict[str, Any]] = [
    # Uttar Pradesh Cities
    {"keywords": ["lucknow"], "state": "Uttar Pradesh", "name": "Lucknow Municipal Corporation", "email": "nnlko@up.nic.in", "cc": "jalkalko@gmail.com"},
    {"keywords": ["kanpur"], "state": "Uttar Pradesh", "name": "Kanpur Municipal Corporation", "email": "mckanpur@yahoo.com", "cc": "gmjkkanpur@gmail.com"},
    {"keywords": ["varanasi", "benares", "kashi"], "state": "Uttar Pradesh", "name": "Varanasi Municipal Corporation", "email": "mcvns1@gmail.com", "cc": "gmjkvns@gmail.com"},
    {"keywords": ["agra"], "state": "Uttar Pradesh", "name": "Agra Municipal Corporation", "email": "amcagra@yahoo.com", "cc": "jalkalagra@gmail.com"},
    {"keywords": ["ghaziabad"], "state": "Uttar Pradesh", "name": "Ghaziabad Municipal Corporation", "email": "gzb.nagar.nigam@gmail.com", "cc": "jalkal.nng@gmail.com"},
    {"keywords": ["meerut"], "state": "Uttar Pradesh", "name": "Meerut Municipal Corporation", "email": "nnmee@nic.in", "cc": "nnmee@up.nic.in"},
    {"keywords": ["prayagraj", "allahabad"], "state": "Uttar Pradesh", "name": "Prayagraj Municipal Corporation", "email": "allahabadjalkalvibhag@rediffmail.com", "cc": "dep_urban@up.gov.in"},
    {"keywords": ["bareilly"], "state": "Uttar Pradesh", "name": "Bareilly Municipal Corporation", "email": "nagarayukt.bareilly@gmail.com", "cc": "gmjkbly@gmail.com"},
    {"keywords": ["aligarh"], "state": "Uttar Pradesh", "name": "Aligarh Municipal Corporation", "email": "nnaligarh@gmail.com", "cc": "gmjalaligarh@gmail.com"},
    {"keywords": ["moradabad"], "state": "Uttar Pradesh", "name": "Moradabad Municipal Corporation", "email": "moradabadnagarnigam@gmail.com", "cc": "dep_urban@up.gov.in"},
    {"keywords": ["gorakhpur"], "state": "Uttar Pradesh", "name": "Gorakhpur Municipal Corporation", "email": "nagarnigamgkp@gmail.com", "cc": "dep_urban@up.gov.in"},
    {"keywords": ["ayodhya", "faizabad"], "state": "Uttar Pradesh", "name": "Ayodhya Municipal Corporation", "email": "nagarnigamayodhya@gmail.com", "cc": "nagarnigamfzd@gmail.com"},
    {"keywords": ["jhansi"], "state": "Uttar Pradesh", "name": "Jhansi Municipal Corporation", "email": "nagarayukta@jnnjhansi.com", "cc": "gmjalsansthanjhansi1975@gmail.com"},
    {"keywords": ["mathura"], "state": "Uttar Pradesh", "name": "Mathura Vrindavan Municipal Corporation", "email": "nagarayuktmathura@gmail.com", "cc": "dep_urban@up.gov.in"},
    {"keywords": ["saharanpur"], "state": "Uttar Pradesh", "name": "Saharanpur Municipal Corporation", "email": "nagarnigamsaharanpur@gmail.com", "cc": "dep_urban@up.gov.in"},
    {"keywords": ["shahjahanpur"], "state": "Uttar Pradesh", "name": "Shahjahanpur Municipal Corporation", "email": "nagarnigamshahjahanpur@gmail.com", "cc": "dep_urban@up.gov.in"},

    # Gujarat Cities
    {"keywords": ["ahmedabad"], "state": "Gujarat", "name": "Ahmedabad Municipal Corporation", "email": "mc@ahmedabadcity.gov.in", "cc": "mc-amc-and@gujarat.gov.in"},
    {"keywords": ["surat"], "state": "Gujarat", "name": "Surat Municipal Corporation", "email": "commissioner@suratmunicipal.org", "cc": "securban@gujarat.gov.in"},
    {"keywords": ["vadodara", "baroda"], "state": "Gujarat", "name": "Vadodara Municipal Corporation", "email": "commissioner@vmc.gov.in", "cc": "commissioner-vpmc@gujarat.gov.in"},
    {"keywords": ["rajkot"], "state": "Gujarat", "name": "Rajkot Municipal Corporation", "email": "mc_rmc@rmc.gov.in", "cc": "securban@gujarat.gov.in"},
    {"keywords": ["gandhinagar"], "state": "Gujarat", "name": "Gandhinagar Municipal Corporation", "email": "gmc8gandhinagar@gmail.com", "cc": "mc-gmc-kut@gujarat.gov.in"},
    {"keywords": ["bhavnagar"], "state": "Gujarat", "name": "Bhavnagar Municipal Corporation", "email": "comm-bmc@gujarat.gov.in", "cc": "securban@gujarat.gov.in"},
    {"keywords": ["jamnagar"], "state": "Gujarat", "name": "Jamnagar Municipal Corporation", "email": "mcjamnagar@gmail.com", "cc": "securban@gujarat.gov.in"},
    {"keywords": ["junagadh"], "state": "Gujarat", "name": "Junagadh Municipal Corporation", "email": "municipalcorporationjund@yahoo.co.in", "cc": "securban@gujarat.gov.in"},

    # Chhattisgarh Cities
    {"keywords": ["raipur"], "state": "Chhattisgarh", "name": "Raipur Municipal Corporation", "email": "raipurjduad@gmail.com", "cc": "dc_rmc@rediffmail.com"},
    {"keywords": ["durg"], "state": "Chhattisgarh", "name": "Durg Municipal Corporation", "email": "durgmc2017@gmail.com", "cc": "jduaddurg@rediffmail.com"},
    {"keywords": ["bilaspur"], "state": "Chhattisgarh", "name": "Bilaspur Municipal Corporation", "email": "commissionerbmcbilaspur@gmail.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["bhilai"], "state": "Chhattisgarh", "name": "Bhilai Municipal Corporation", "email": "nigam_bhilai@yahoo.co.in", "cc": "uad.cg@gov.in"},
    {"keywords": ["korba"], "state": "Chhattisgarh", "name": "Korba Municipal Corporation", "email": "corporationkorba@gmail.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["rajnandgaon"], "state": "Chhattisgarh", "name": "Rajnandgaon Municipal Corporation", "email": "nigamrajnandgaon@gmail.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["jagdalpur"], "state": "Chhattisgarh", "name": "Jagdalpur Municipal Corporation", "email": "jagdalpurnagarnigam@yahoo.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["ambikapur"], "state": "Chhattisgarh", "name": "Ambikapur Municipal Corporation", "email": "nagarnigamambikapur@gmail.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["raigarh"], "state": "Chhattisgarh", "name": "Raigarh Municipal Corporation", "email": "nraigarh@ymail.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["dhamtari"], "state": "Chhattisgarh", "name": "Dhamtari Municipal Council", "email": "commissionerdhamtari@gmail.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["chirimiri"], "state": "Chhattisgarh", "name": "Chirimiri Municipal Corporation", "email": "nagarnigam_chirimiri@yahoo.com", "cc": "uad.cg@gov.in"},
    {"keywords": ["risali"], "state": "Chhattisgarh", "name": "Risali Municipal Corporation", "email": "nagarnigamrisali@gmail.com", "cc": "uad.cg@gov.in"},

    # Himachal Pradesh Cities
    {"keywords": ["shimla", "simla"], "state": "Himachal Pradesh", "name": "Shimla Municipal Corporation", "email": "mcsml-hp@nic.in", "cc": "eomcshimla@gmail.com"},
    {"keywords": ["dharamshala", "dharamsala"], "state": "Himachal Pradesh", "name": "Dharamshala Municipal Corporation", "email": "mc_dharamshala@rediffmail.com", "cc": "ud-hp@nic.in"},
    {"keywords": ["solan"], "state": "Himachal Pradesh", "name": "Solan Municipal Corporation", "email": "eomcsolan@gmail.com", "cc": "mcsolan@yahoo.com"},
    {"keywords": ["mandi"], "state": "Himachal Pradesh", "name": "Mandi Municipal Corporation", "email": "eomcmandi@gmail.com", "cc": "mcmandi09@hotmail.com"},
    {"keywords": ["palampur"], "state": "Himachal Pradesh", "name": "Palampur Municipal Corporation", "email": "mc.palampur@gmail.com", "cc": "ud-hp@nic.in"},
    {"keywords": ["kullu"], "state": "Himachal Pradesh", "name": "Kullu Municipal Council", "email": "eomckullu@gmail.com", "cc": "mckullu@yahoo.in"},
    {"keywords": ["manali"], "state": "Himachal Pradesh", "name": "Manali Municipal Council", "email": "mc1manali@gmail.com", "cc": "ud-hp@nic.in"},
    {"keywords": ["baddi"], "state": "Himachal Pradesh", "name": "Baddi Municipal Council", "email": "mcbaddi@gmail.com", "cc": "ud-hp@nic.in"},

    # Jharkhand Cities
    {"keywords": ["jamshedpur", "tatanagar"], "state": "Jharkhand", "name": "Jamshedpur Notified Area Committee", "email": "jnacjsr@gmail.com", "cc": "director.ma.goj@gmail.com"},
    {"keywords": ["adityapur"], "state": "Jharkhand", "name": "Adityapur Municipal Corporation", "email": "mcadityapur@gmail.com", "cc": "director.ma.goj@gmail.com"},
    {"keywords": ["giridih"], "state": "Jharkhand", "name": "Giridih Municipal Corporation", "email": "municipal.giridih@gmail.com", "cc": "director.ma.goj@gmail.com"},
    {"keywords": ["medininagar", "daltonganj"], "state": "Jharkhand", "name": "Medininagar Municipal Corporation", "email": "municipalcouncil.medininagar@gmail.com", "cc": "director.ma.goj@gmail.com"},
    {"keywords": ["ramgarh"], "state": "Jharkhand", "name": "Ramgarh Nagar Parishad", "email": "nagarparishadramgarh@gmail.com", "cc": "director.ma.goj@gmail.com"},
    {"keywords": ["sahibganj"], "state": "Jharkhand", "name": "Sahibganj Nagar Parishad", "email": "nagarparishadsahibganj@yahoo.in", "cc": "director.ma.goj@gmail.com"},
]


class StateAuthorityResolver:
    """Resolves municipal authority contacts when polygon containment is unavailable."""

    def __init__(self, contacts_path: Path = DATA_FILE) -> None:
        self.contacts_path = contacts_path
        self._raw_records: list[dict[str, str]] = []
        self._load_records()

    def _load_records(self) -> None:
        if not self.contacts_path.is_file():
            logger.warning("Scraped municipal contacts file not found: %s", self.contacts_path)
            return

        try:
            with open(self.contacts_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self._raw_records.append(row)
            logger.info("Loaded %d raw contacts from %s", len(self._raw_records), self.contacts_path.name)
        except Exception as exc:
            logger.error("Failed to read scraped contacts: %s", exc)

    def resolve(
        self,
        lat: float,
        lon: float,
        address_dict: Optional[dict[str, Any]] = None,
        resolved_address: str = "",
    ) -> Optional[dict[str, Any]]:
        """
        Attempt to resolve authority based on address metadata (state, city, district).
        """
        if address_dict is None:
            address_dict = {}

        state = str(address_dict.get("state", "")).strip()
        city = str(
            address_dict.get("city")
            or address_dict.get("town")
            or address_dict.get("municipality")
            or address_dict.get("state_district")
            or address_dict.get("county")
            or ""
        ).strip().lower()

        full_text = (resolved_address + " " + city + " " + state).lower().strip()
        if not full_text:
            return None

        # 1. Check City-Level match first (e.g., Lucknow, Surat, Raipur, Shimla)
        for entry in CITY_KEYWORD_DIRECTORY:
            # If state matches (or not specified) and city keyword is present
            if not entry.get("state") or entry["state"].lower() in full_text:
                for kw in entry["keywords"]:
                    if kw in full_text:
                        logger.info("Matched city-level municipal authority: %s (%s)", entry["name"], entry["state"])
                        cc_list = [entry["cc"]] if entry.get("cc") else []
                        # Add state nodal email in CC if available
                        state_nodal = STATE_NODAL_DIRECTORIES.get(entry["state"])
                        if state_nodal and state_nodal["primary_email"] not in cc_list and state_nodal["primary_email"] != entry["email"]:
                            cc_list.append(state_nodal["primary_email"])

                        return {
                            "municipality_name": entry["name"],
                            "ward_number": "N/A",
                            "ward_name": f"{entry['name']}, {entry['state']}",
                            "sanitary_inspector_email": entry["email"],
                            "moh_email": cc_list[0] if cc_list else "",
                            "commissioner_email": cc_list[1] if len(cc_list) > 1 else "",
                            "website": state_nodal["website"] if state_nodal else "",
                            "helpline": state_nodal["helpline"] if state_nodal else "1100",
                            "ward_matched": False,
                            "municipal_matched": True,
                            "match_type": "state_city_directory",
                            "boundary_quality": f"city_directory ({entry['name']})",
                            "confidence": "medium",
                            "latitude": float(lat),
                            "longitude": float(lon),
                            "resolved_address": resolved_address,
                            "formatted_address": resolved_address,
                        }

        # 2. Check State-Level Nodal Directory
        # Find matching state
        matched_state_key = None
        for key in STATE_NODAL_DIRECTORIES.keys():
            if key.lower() in full_text:
                matched_state_key = key
                break

        if matched_state_key:
            nodal = STATE_NODAL_DIRECTORIES[matched_state_key]
            logger.info("Matched state-level urban directorate: %s", nodal["name"])
            return {
                "municipality_name": nodal["name"],
                "ward_number": "N/A",
                "ward_name": f"{city.title() if city else 'Central'}, {matched_state_key}",
                "sanitary_inspector_email": nodal["primary_email"],
                "moh_email": nodal["cc_emails"][0] if nodal["cc_emails"] else "",
                "commissioner_email": nodal["cc_emails"][1] if len(nodal["cc_emails"]) > 1 else "",
                "website": nodal["website"],
                "helpline": nodal["helpline"],
                "ward_matched": False,
                "municipal_matched": True,
                "match_type": "state_directorate",
                "boundary_quality": f"state_directory ({matched_state_key})",
                "confidence": "medium",
                "latitude": float(lat),
                "longitude": float(lon),
                "resolved_address": resolved_address,
                "formatted_address": resolved_address,
            }

        return None


_STATE_RESOLVER: Optional[StateAuthorityResolver] = None


def get_state_authority_resolver() -> StateAuthorityResolver:
    global _STATE_RESOLVER
    if _STATE_RESOLVER is None:
        _STATE_RESOLVER = StateAuthorityResolver()
    return _STATE_RESOLVER
