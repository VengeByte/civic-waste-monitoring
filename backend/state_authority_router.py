"""
State and Municipal Authority Directory Router.

Provides fallback authority resolution for regions and cities across India
where granular ward or municipal polygon boundaries are unavailable.
Routes complaints to the respective Municipal Corporation (if city is recognized)
or to the State Directorate of Urban Local Bodies / Municipal Administration.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("backend.state_authority_router")

DATA_FILE = Path(__file__).resolve().parent / "data" / "scraped_municipal_emails.csv"

# Comprehensive official nodal directories for all 28 Indian States & Union Territories
STATE_NODAL_DIRECTORIES: dict[str, dict[str, Any]] = {
    # Northern States & UTs
    "Chandigarh": {
        "name": "Municipal Corporation Chandigarh",
        "primary_email": "mohmcc-chd@nic.in",
        "cc_emails": ["cemcmcc-chd@nic.in", "commissionermcc@gmail.com"],
        "website": "https://mcchandigarh.gov.in",
        "helpline": "0172-2787200",
    },
    "Delhi": {
        "name": "Municipal Corporation of Delhi (MCD)",
        "primary_email": "commissioner-mcd@mcd.nic.in",
        "cc_emails": ["feedback-mcd@mcd.nic.in"],
        "website": "https://mcdonline.nic.in",
        "helpline": "155305",
    },
    "Haryana": {
        "name": "Directorate of Urban Local Bodies, Haryana",
        "primary_email": "dulbhry@gmail.com",
        "cc_emails": ["ulbharyana@gmail.com"],
        "website": "https://ulbharyana.gov.in",
        "helpline": "1800-180-2026",
    },
    "Himachal Pradesh": {
        "name": "Directorate of Urban Development, Himachal Pradesh",
        "primary_email": "ud-hp@nic.in",
        "cc_emails": ["mcsml-hp@nic.in"],
        "website": "https://ud.hp.gov.in",
        "helpline": "1100",
    },
    "Jammu and Kashmir": {
        "name": "Housing & Urban Development Department, Jammu & Kashmir",
        "primary_email": "hud-jk@nic.in",
        "cc_emails": ["hudd-jk@gov.in"],
        "website": "https://jkhudd.gov.in",
        "helpline": "0191-2561705",
    },
    "Ladakh": {
        "name": "Housing & Urban Development Department, Ladakh",
        "primary_email": "secy-hud-ladakh@gov.in",
        "cc_emails": ["comsecy-utladakh@gov.in"],
        "website": "https://ladakh.nic.in",
        "helpline": "01982-255567",
    },
    "Punjab": {
        "name": "Department of Local Government, Punjab",
        "primary_email": "dlgpunjab@punjab.gov.in",
        "cc_emails": ["pmidc@punjab.gov.in"],
        "website": "https://lgpunjab.gov.in",
        "helpline": "1800-1800-0172",
    },
    "Rajasthan": {
        "name": "Local Self Government Department, Rajasthan",
        "primary_email": "dlb-rj@nic.in",
        "cc_emails": ["comm-dlb-rj@nic.in"],
        "website": "https://lsg.urban.rajasthan.gov.in",
        "helpline": "181",
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

    # Southern States & UTs
    "Karnataka": {
        "name": "Directorate of Municipal Administration, Karnataka",
        "primary_email": "dmadir.kar@nic.in",
        "cc_emails": ["dma_karnataka@yahoo.com"],
        "website": "https://municipaladmn.karnataka.gov.in",
        "helpline": "080-22864696",
    },
    "Tamil Nadu": {
        "name": "Commissioner of Municipal Administration, Tamil Nadu",
        "primary_email": "cma.tncma@nic.in",
        "cc_emails": ["commr.cma@tn.gov.in"],
        "website": "https://tnurbantree.tn.gov.in",
        "helpline": "1800-425-4144",
    },
    "Kerala": {
        "name": "Local Self Government Department, Kerala",
        "primary_email": "splsecy.lsgd@kerala.gov.in",
        "cc_emails": ["prlsecylsgd@gmail.com"],
        "website": "https://lsgkerala.gov.in",
        "helpline": "1800-425-1089",
    },
    "Telangana": {
        "name": "Commissioner and Director of Municipal Administration, Telangana",
        "primary_email": "cdmatelangana@gmail.com",
        "cc_emails": ["cdma@cdma.gov.in"],
        "website": "https://cdma.telangana.gov.in",
        "helpline": "040-23302150",
    },
    "Andhra Pradesh": {
        "name": "Commissioner and Director of Municipal Administration, Andhra Pradesh",
        "primary_email": "cdma@cdma.ap.gov.in",
        "cc_emails": ["commr.cdma@ap.gov.in"],
        "website": "https://cdma.ap.gov.in",
        "helpline": "1800-425-8878",
    },
    "Puducherry": {
        "name": "Local Administration Department, Puducherry",
        "primary_email": "lad.pon@nic.in",
        "cc_emails": ["directorlad.py@gov.in"],
        "website": "https://lad.py.gov.in",
        "helpline": "0413-2210582",
    },
    "Lakshadweep": {
        "name": "Department of Panchayats and Urban Local Bodies, Lakshadweep",
        "primary_email": "lk-dop@nic.in",
        "cc_emails": ["secy-panchayat@lakshadweep.gov.in"],
        "website": "https://lakshadweep.gov.in",
        "helpline": "04896-262256",
    },

    # Western States & UTs
    "Maharashtra": {
        "name": "Directorate of Municipal Administration, Maharashtra",
        "primary_email": "mahadma@mahadma.maharashtra.gov.in",
        "cc_emails": ["director.dma@maharashtra.gov.in"],
        "website": "https://mahadma.maharashtra.gov.in",
        "helpline": "022-22025270",
    },
    "Gujarat": {
        "name": "Urban Development & Urban Housing Department, Gujarat",
        "primary_email": "securban@gujarat.gov.in",
        "cc_emails": ["sec-hn-urban@gujarat.gov.in"],
        "website": "https://udd.gujarat.gov.in",
        "helpline": "1800-233-0000",
    },
    "Goa": {
        "name": "Directorate of Municipal Administration, Goa",
        "primary_email": "dir-dma.goa@nic.in",
        "cc_emails": ["admin-dma.goa@nic.in"],
        "website": "https://dma.goa.gov.in",
        "helpline": "0832-2432845",
    },
    "Dadra and Nagar Haveli and Daman and Diu": {
        "name": "Daman & Silvassa Municipal Administration",
        "primary_email": "dmcdaman@gmail.com",
        "cc_emails": ["smcsilvassa@gmail.com"],
        "website": "https://daman.nic.in",
        "helpline": "0260-2230879",
    },

    # Central & Eastern States
    "Madhya Pradesh": {
        "name": "Urban Administration & Development Directorate, Madhya Pradesh",
        "primary_email": "cmamp14@mpurban.gov.in",
        "cc_emails": ["lsbaghel.mpuad@gmail.com"],
        "website": "https://mpurban.gov.in",
        "helpline": "181",
    },
    "Chhattisgarh": {
        "name": "Urban Administration & Development Department, Chhattisgarh",
        "primary_email": "uad.cg@gov.in",
        "cc_emails": ["ceo-suda@cg.gov.in", "adf.uad.cg@nic.in"],
        "website": "https://uad.cg.gov.in",
        "helpline": "1100",
    },
    "Bihar": {
        "name": "Urban Development & Housing Department, Bihar",
        "primary_email": "cs-bihar@nic.in",
        "cc_emails": ["feedback.commonportal@gmail.com"],
        "website": "https://state.bihar.gov.in/urban",
        "helpline": "1800-3456-112",
    },
    "Jharkhand": {
        "name": "Urban Development & Housing Department, Jharkhand",
        "primary_email": "director.ma.goj@gmail.com",
        "cc_emails": ["ud.secy@gmail.com", "suda.goj@gmail.com"],
        "website": "https://udhd.jharkhand.gov.in",
        "helpline": "181",
    },
    "Odisha": {
        "name": "Housing & Urban Development Department, Odisha",
        "primary_email": "hudsec.or@od.gov.in",
        "cc_emails": ["dmahudd@gmail.com", "diraddlsecretary@gmail.com"],
        "website": "https://urban.odisha.gov.in",
        "helpline": "155304",
    },
    "West Bengal": {
        "name": "Urban Development & Municipal Affairs Department, West Bengal",
        "primary_email": "secy-udma@wb.gov.in",
        "cc_emails": ["prsecy.udma@gmail.com"],
        "website": "https://www.wburbanservices.gov.in",
        "helpline": "1800-345-5555",
    },

    # North-Eastern States
    "Assam": {
        "name": "Directorate of Municipal Administration, Assam",
        "primary_email": "director.ma-as@gov.in",
        "cc_emails": ["director.mad.assam@gmail.com"],
        "website": "https://mad.assam.gov.in",
        "helpline": "1800-345-3525",
    },
    "Tripura": {
        "name": "Urban Development Department, Tripura",
        "primary_email": "udd.tripura@gmail.com",
        "cc_emails": ["dir.udd-tr@nic.in"],
        "website": "https://udd.tripura.gov.in",
        "helpline": "0381-2415174",
    },
    "Meghalaya": {
        "name": "Urban Affairs Department, Meghalaya",
        "primary_email": "urbanaffairs-meg@gov.in",
        "cc_emails": ["duashillong@yahoo.co.in"],
        "website": "https://megurbanaffairs.gov.in",
        "helpline": "0364-2224177",
    },
    "Manipur": {
        "name": "Municipal Administration, Housing & Urban Development, Manipur",
        "primary_email": "mahud-mn@nic.in",
        "cc_emails": ["dir.mahud@gmail.com"],
        "website": "https://mahudmanipur.gov.in",
        "helpline": "0385-2451368",
    },
    "Nagaland": {
        "name": "Department of Urban Development, Nagaland",
        "primary_email": "urbannagaland@gmail.com",
        "cc_emails": ["dirurban-ngl@nic.in"],
        "website": "https://urbandevelopment.nagaland.gov.in",
        "helpline": "0370-2270275",
    },
    "Mizoram": {
        "name": "Urban Development & Poverty Alleviation Department, Mizoram",
        "primary_email": "udpa.mizoram@gmail.com",
        "cc_emails": ["directorudpa@gmail.com"],
        "website": "https://udpa.mizoram.gov.in",
        "helpline": "0389-2333469",
    },
    "Arunachal Pradesh": {
        "name": "Department of Urban Development, Arunachal Pradesh",
        "primary_email": "dir-ud-arn@nic.in",
        "cc_emails": ["ceud-arn@nic.in"],
        "website": "https://arunachalpradesh.gov.in",
        "helpline": "0360-2212351",
    },
    "Sikkim": {
        "name": "Urban Development Department, Sikkim",
        "primary_email": "udhd.sikkim@gmail.com",
        "cc_emails": ["secy-udd@sikkim.gov.in"],
        "website": "https://uddsikkim.org",
        "helpline": "03592-202726",
    },
    "Andaman and Nicobar Islands": {
        "name": "Port Blair Municipal Council (PBMC)",
        "primary_email": "secypbmc@gmail.com",
        "cc_emails": ["pbmc.portblair@gmail.com"],
        "website": "https://pbmc.gov.in",
        "helpline": "03192-232698",
    },
}

# City keyword mappings to specific municipality contacts
CITY_KEYWORD_DIRECTORY: list[dict[str, Any]] = [
    # Karnataka Cities
    {
        "keywords": ["mysore", "mysuru"],
        "state": "Karnataka",
        "name": "Mysuru City Corporation (MCC)",
        "email": "commissionermcc@gmail.com",
        "cc": "commr_mcc@yahoo.co.in",
        "website": "https://mysurucitycorporation.co.in",
        "helpline": "0821-2418800",
    },
    {
        "keywords": ["bengaluru", "bangalore", "bbmp"],
        "state": "Karnataka",
        "name": "Bruhat Bengaluru Mahanagara Palike (BBMP)",
        "email": "comm@bbmp.gov.in",
        "cc": "eoc@bbmp.gov.in",
        "website": "https://bbmp.gov.in",
        "helpline": "080-22660000",
    },
    {
        "keywords": ["mangalore", "mangaluru"],
        "state": "Karnataka",
        "name": "Mangaluru City Corporation",
        "email": "commissioner.mcc@gmail.com",
        "cc": "itd_mcc@yahoo.com",
        "website": "https://mangaluroo.mrc.gov.in",
        "helpline": "0824-2220306",
    },
    {
        "keywords": ["hubballi", "hubli", "dharwad"],
        "state": "Karnataka",
        "name": "Hubballi-Dharwad Municipal Corporation",
        "email": "commissionerhdmc@gmail.com",
        "cc": "dmadir.kar@nic.in",
        "website": "https://hdmc.mrc.gov.in",
        "helpline": "0836-2213888",
    },
    {
        "keywords": ["belagavi", "belgaum"],
        "state": "Karnataka",
        "name": "City Corporation Belagavi",
        "email": "commissioner.belgaum@gmail.com",
        "cc": "dmadir.kar@nic.in",
        "website": "https://belagavicitycorp.org",
        "helpline": "0831-2405300",
    },
    {
        "keywords": ["kalaburagi", "gulbarga"],
        "state": "Karnataka",
        "name": "City Corporation Kalaburagi",
        "email": "commissioner.kalaburagi@gmail.com",
        "cc": "dmadir.kar@nic.in",
        "website": "https://kalaburagicity.mrc.gov.in",
        "helpline": "08472-220130",
    },
    {
        "keywords": ["davanagere", "davangere"],
        "state": "Karnataka",
        "name": "Davanagere City Corporation",
        "email": "commissioner.dcc@gmail.com",
        "cc": "dmadir.kar@nic.in",
        "website": "https://davanagerecity.mrc.gov.in",
        "helpline": "08192-231366",
    },
    {
        "keywords": ["ballari", "bellary"],
        "state": "Karnataka",
        "name": "City Corporation Ballari",
        "email": "commissioner.ballari@gmail.com",
        "cc": "dmadir.kar@nic.in",
        "website": "https://ballaricity.mrc.gov.in",
        "helpline": "08392-272129",
    },
    {
        "keywords": ["shivamogga", "shimoga"],
        "state": "Karnataka",
        "name": "City Corporation Shivamogga",
        "email": "commissioner.smg@gmail.com",
        "cc": "dmadir.kar@nic.in",
        "website": "https://shivamoggacity.mrc.gov.in",
        "helpline": "08182-222288",
    },

    # Maharashtra Cities (outside covered polygons)
    {"keywords": ["nagpur"], "state": "Maharashtra", "name": "Nagpur Municipal Corporation", "email": "mcnagpur@gmail.com", "cc": "commissioner@nmc.gov.in"},
    {"keywords": ["nashik", "nasik"], "state": "Maharashtra", "name": "Nashik Municipal Corporation", "email": "commissioner@nmc.gov.in", "cc": "admin@nmc.gov.in"},
    {"keywords": ["aurangabad", "sambhajinagar", "chhatrapati sambhajinagar"], "state": "Maharashtra", "name": "Chhatrapati Sambhajinagar Municipal Corporation", "email": "comm_amc@yahoo.com", "cc": "amcaurangabad@gmail.com"},
    {"keywords": ["solapur", "sholapur"], "state": "Maharashtra", "name": "Solapur Municipal Corporation", "email": "smcsolapur@yahoo.co.in", "cc": "commissioner@solapurcorporation.gov.in"},
    {"keywords": ["kolhapur"], "state": "Maharashtra", "name": "Kolhapur Municipal Corporation", "email": "kmc@kolhapurcorporation.gov.in", "cc": "commissionerkmc@gmail.com"},

    # Tamil Nadu Cities
    {"keywords": ["chennai", "madras"], "state": "Tamil Nadu", "name": "Greater Chennai Corporation (GCC)", "email": "commissioner@chennaicorporation.gov.in", "cc": "cma.tncma@nic.in"},
    {"keywords": ["coimbatore", "kovai"], "state": "Tamil Nadu", "name": "Coimbatore City Municipal Corporation", "email": "commr.coimbatore@tn.gov.in", "cc": "cma.tncma@nic.in"},
    {"keywords": ["madurai"], "state": "Tamil Nadu", "name": "Madurai Corporation", "email": "mdu.corporation@gmail.com", "cc": "commr.madurai@tn.gov.in"},
    {"keywords": ["tiruchirappalli", "trichy", "tiruchi"], "state": "Tamil Nadu", "name": "Tiruchirappalli City Corporation", "email": "commr.trichy@tn.gov.in", "cc": "cma.tncma@nic.in"},
    {"keywords": ["salem"], "state": "Tamil Nadu", "name": "Salem City Municipal Corporation", "email": "commr.salem@tn.gov.in", "cc": "cma.tncma@nic.in"},

    # Telangana Cities
    {"keywords": ["hyderabad", "ghmc", "secunderabad"], "state": "Telangana", "name": "Greater Hyderabad Municipal Corporation (GHMC)", "email": "commissioner-ghmc@telangana.gov.in", "cc": "cdmatelangana@gmail.com"},
    {"keywords": ["warangal"], "state": "Telangana", "name": "Greater Warangal Municipal Corporation (GWMC)", "email": "gwmc_wgl@yahoo.co.in", "cc": "cdmatelangana@gmail.com"},

    # Andhra Pradesh Cities
    {"keywords": ["visakhapatnam", "vizag", "gvmc"], "state": "Andhra Pradesh", "name": "Greater Visakhapatnam Municipal Corporation (GVMC)", "email": "commissioner_gvmc@yahoo.co.in", "cc": "cdma@cdma.ap.gov.in"},
    {"keywords": ["vijayawada"], "state": "Andhra Pradesh", "name": "Vijayawada Municipal Corporation", "email": "ourvmc@yahoo.com", "cc": "commissionervmc@gmail.com"},

    # Rajasthan Cities
    {"keywords": ["jaipur"], "state": "Rajasthan", "name": "Jaipur Municipal Corporation", "email": "comm.nnj@rajasthan.gov.in", "cc": "dlb-rj@nic.in"},
    {"keywords": ["jodhpur"], "state": "Rajasthan", "name": "Jodhpur Municipal Corporation", "email": "comm.nnjodhpur@rajasthan.gov.in", "cc": "dlb-rj@nic.in"},
    {"keywords": ["udaipur"], "state": "Rajasthan", "name": "Udaipur Municipal Corporation", "email": "commissioner@udaipurmc.org", "cc": "dlb-rj@nic.in"},

    # Punjab Cities
    {"keywords": ["ludhiana"], "state": "Punjab", "name": "Municipal Corporation Ludhiana", "email": "commissioner.mcl@punjab.gov.in", "cc": "dlgpunjab@punjab.gov.in"},
    {"keywords": ["amritsar"], "state": "Punjab", "name": "Municipal Corporation Amritsar", "email": "cmca@punjab.gov.in", "cc": "dlgpunjab@punjab.gov.in"},

    # West Bengal Cities
    {"keywords": ["kolkata", "calcutta", "kmc"], "state": "West Bengal", "name": "Kolkata Municipal Corporation (KMC)", "email": "mpo.kmc@gmail.com", "cc": "secy-udma@wb.gov.in"},
    {"keywords": ["howrah", "hmc"], "state": "West Bengal", "name": "Howrah Municipal Corporation", "email": "hmc_adm@rediffmail.com", "cc": "secy-udma@wb.gov.in"},

    # Bihar Cities
    {"keywords": ["patna"], "state": "Bihar", "name": "Patna Municipal Corporation", "email": "patnamunicipalcorporation@gmail.com", "cc": "cs-bihar@nic.in"},
    {"keywords": ["gaya"], "state": "Bihar", "name": "Gaya Municipal Corporation", "email": "gayamunicipalcorporation@gmail.com", "cc": "cs-bihar@nic.in"},
    {"keywords": ["muzaffarpur"], "state": "Bihar", "name": "Muzaffarpur Municipal Corporation", "email": "nagarnigam.muz@gmail.com", "cc": "cs-bihar@nic.in"},
    {"keywords": ["bhagalpur"], "state": "Bihar", "name": "Bhagalpur Municipal Corporation", "email": "nagarnigambhagalpur@gmail.com", "cc": "cs-bihar@nic.in"},

    # Madhya Pradesh Cities
    {"keywords": ["indore"], "state": "Madhya Pradesh", "name": "Indore Municipal Corporation", "email": "imcindore@mpurban.gov.in", "cc": "cmamp14@mpurban.gov.in"},
    {"keywords": ["bhopal"], "state": "Madhya Pradesh", "name": "Bhopal Municipal Corporation", "email": "commissioner@bmconline.gov.in", "cc": "cmamp14@mpurban.gov.in"},
    {"keywords": ["jabalpur"], "state": "Madhya Pradesh", "name": "Jabalpur Municipal Corporation", "email": "jmcjabalpur@mpurban.gov.in", "cc": "cmamp14@mpurban.gov.in"},
    {"keywords": ["gwalior"], "state": "Madhya Pradesh", "name": "Gwalior Municipal Corporation", "email": "gwaliorcorporation@mpurban.gov.in", "cc": "cmamp14@mpurban.gov.in"},

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

# Geographic bounding boxes for major Indian states for rough coordinate fallback
# when reverse-geocoding returns no street or city text (e.g. offline/timeout)
STATE_COORDINATE_BOUNDS: list[dict[str, Any]] = [
    {"state": "Karnataka", "lat_min": 11.5, "lat_max": 18.5, "lon_min": 74.0, "lon_max": 78.6},
    {"state": "Tamil Nadu", "lat_min": 8.0, "lat_max": 13.5, "lon_min": 76.2, "lon_max": 80.4},
    {"state": "Kerala", "lat_min": 8.3, "lat_max": 12.8, "lon_min": 74.8, "lon_max": 77.4},
    {"state": "Andhra Pradesh", "lat_min": 12.6, "lat_max": 19.9, "lon_min": 76.7, "lon_max": 84.8},
    {"state": "Telangana", "lat_min": 15.8, "lat_max": 19.9, "lon_min": 77.2, "lon_max": 81.8},
    {"state": "Maharashtra", "lat_min": 15.6, "lat_max": 22.0, "lon_min": 72.6, "lon_max": 80.9},
    {"state": "Gujarat", "lat_min": 20.1, "lat_max": 24.7, "lon_min": 68.1, "lon_max": 74.5},
    {"state": "Rajasthan", "lat_min": 23.0, "lat_max": 30.2, "lon_min": 69.5, "lon_max": 78.3},
    {"state": "Uttar Pradesh", "lat_min": 23.8, "lat_max": 30.4, "lon_min": 77.0, "lon_max": 84.7},
    {"state": "Bihar", "lat_min": 24.3, "lat_max": 27.5, "lon_min": 83.3, "lon_max": 88.3},
    {"state": "West Bengal", "lat_min": 21.5, "lat_max": 27.2, "lon_min": 85.8, "lon_max": 89.9},
    {"state": "Odisha", "lat_min": 17.8, "lat_max": 22.6, "lon_min": 81.4, "lon_max": 87.5},
    {"state": "Madhya Pradesh", "lat_min": 21.1, "lat_max": 26.9, "lon_min": 74.0, "lon_max": 82.8},
    {"state": "Punjab", "lat_min": 29.5, "lat_max": 32.5, "lon_min": 73.9, "lon_max": 76.9},
    {"state": "Haryana", "lat_min": 27.6, "lat_max": 30.9, "lon_min": 74.5, "lon_max": 77.6},
    {"state": "Himachal Pradesh", "lat_min": 30.4, "lat_max": 33.2, "lon_min": 75.7, "lon_max": 79.0},
    {"state": "Uttarakhand", "lat_min": 28.7, "lat_max": 31.5, "lon_min": 77.6, "lon_max": 81.0},
    {"state": "Delhi", "lat_min": 28.4, "lat_max": 28.9, "lon_min": 76.8, "lon_max": 77.4},
    {"state": "Jammu and Kashmir", "lat_min": 32.2, "lat_max": 35.0, "lon_min": 73.7, "lon_max": 76.8},
    {"state": "Assam", "lat_min": 24.1, "lat_max": 28.0, "lon_min": 89.7, "lon_max": 96.0},
    {"state": "Goa", "lat_min": 14.9, "lat_max": 15.8, "lon_min": 73.6, "lon_max": 74.4},
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
        Attempt to resolve authority based on address metadata (state, city, district)
        or rough geographic coordinate bounding boxes across India.
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

        # 1. Check City-Level match first (e.g., Mysore, Lucknow, Surat, Raipur, Shimla)
        if full_text:
            for entry in CITY_KEYWORD_DIRECTORY:
                entry_state = entry.get("state", "")
                # If state is provided and contradicts, skip
                if entry_state and state and entry_state.lower() != state.lower():
                    continue

                for kw in entry["keywords"]:
                    if re.search(r"\b" + re.escape(kw) + r"\b", full_text):
                        logger.info("Matched city-level municipal authority: %s (%s)", entry["name"], entry["state"])
                        cc_list = [entry["cc"]] if entry.get("cc") else []
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
                            "website": entry.get("website") or (state_nodal["website"] if state_nodal else ""),
                            "helpline": entry.get("helpline") or (state_nodal["helpline"] if state_nodal else "1100"),
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

        # 2. Check State-Level Nodal Directory via text match
        if full_text:
            matched_state_key = None
            for key in STATE_NODAL_DIRECTORIES.keys():
                if re.search(r"\b" + re.escape(key.lower()) + r"\b", full_text):
                    matched_state_key = key
                    break

            if matched_state_key:
                nodal = STATE_NODAL_DIRECTORIES[matched_state_key]
                logger.info("Matched state-level urban directorate: %s", nodal["name"])
                return {
                    "municipality_name": nodal["name"],
                    "ward_number": "N/A",
                    "ward_name": f"{city.title() if city else 'General'}, {matched_state_key}",
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

        # 3. Rough Geographic Coordinate Bounding-Box Fallback
        # If reverse-geocoding was offline or gave no city/state keywords, estimate state by GPS bounds
        if lat != 0.0 or lon != 0.0:
            for b in STATE_COORDINATE_BOUNDS:
                if b["lat_min"] <= lat <= b["lat_max"] and b["lon_min"] <= lon <= b["lon_max"]:
                    st = b["state"]
                    nodal = STATE_NODAL_DIRECTORIES.get(st)
                    if nodal:
                        logger.info("Matched state via GPS coordinate bounds: %s (%.4f, %.4f)", st, lat, lon)
                        return {
                            "municipality_name": nodal["name"],
                            "ward_number": "N/A",
                            "ward_name": f"Regional Division, {st}",
                            "sanitary_inspector_email": nodal["primary_email"],
                            "moh_email": nodal["cc_emails"][0] if nodal["cc_emails"] else "",
                            "commissioner_email": nodal["cc_emails"][1] if len(nodal["cc_emails"]) > 1 else "",
                            "website": nodal["website"],
                            "helpline": nodal["helpline"],
                            "ward_matched": False,
                            "municipal_matched": True,
                            "match_type": "state_directorate",
                            "boundary_quality": f"gps_bounds_estimate ({st})",
                            "confidence": "low",
                            "latitude": float(lat),
                            "longitude": float(lon),
                            "resolved_address": resolved_address or f"Lat: {lat:.6f}, Lon: {lon:.6f}",
                            "formatted_address": resolved_address or f"Lat: {lat:.6f}, Lon: {lon:.6f}",
                        }

        return None


_STATE_RESOLVER: Optional[StateAuthorityResolver] = None


def get_state_authority_resolver() -> StateAuthorityResolver:
    global _STATE_RESOLVER
    if _STATE_RESOLVER is None:
        _STATE_RESOLVER = StateAuthorityResolver()
    return _STATE_RESOLVER
