"""Fixed sector vocabulary shared by seeds, profiles and desktop labels."""

import logging
from enum import StrEnum


class Sector(StrEnum):
    TECHNOLOGY = "technology"
    MARKETING_ADVERTISING = "marketing_advertising"
    MANUFACTURING_INDUSTRIAL = "manufacturing_industrial"
    AUTOMOTIVE = "automotive"
    AEROSPACE_DEFENSE = "aerospace_defense"
    ENERGY_UTILITIES = "energy_utilities"
    PHARMA_HEALTH = "pharma_health"
    CONSUMER_RETAIL = "consumer_retail"
    FINANCE_INSURANCE = "finance_insurance"
    LOGISTICS_TRANSPORT = "logistics_transport"
    CONSTRUCTION_ENGINEERING = "construction_engineering"
    TELECOM_MEDIA = "telecom_media"
    PUBLIC_EDUCATION = "public_education"
    PROFESSIONAL_SERVICES = "professional_services"
    OTHER = "other"


SECTOR_LABELS: dict[Sector, str] = {
    Sector.TECHNOLOGY: "Technology",
    Sector.MARKETING_ADVERTISING: "Marketing and advertising",
    Sector.MANUFACTURING_INDUSTRIAL: "Manufacturing and industry",
    Sector.AUTOMOTIVE: "Automotive",
    Sector.AEROSPACE_DEFENSE: "Aerospace and defense",
    Sector.ENERGY_UTILITIES: "Energy and utilities",
    Sector.PHARMA_HEALTH: "Pharma and health",
    Sector.CONSUMER_RETAIL: "Consumer and retail",
    Sector.FINANCE_INSURANCE: "Finance and insurance",
    Sector.LOGISTICS_TRANSPORT: "Logistics and transport",
    Sector.CONSTRUCTION_ENGINEERING: "Construction and engineering",
    Sector.TELECOM_MEDIA: "Telecommunications and media",
    Sector.PUBLIC_EDUCATION: "Public sector and education",
    Sector.PROFESSIONAL_SERVICES: "Professional services",
    Sector.OTHER: "Other",
}


def seed_sector_values(value: object, *, seed_id: str, logger: logging.Logger) -> list[Sector]:
    """Admit known user-file values and report rejected values without echoing them."""
    if not isinstance(value, (list, tuple)):
        return []
    result: list[Sector] = []
    for raw in value:
        try:
            sector = Sector(raw)
        except (ValueError, TypeError):
            logger.warning("Unknown sector dropped seed_id=%s", seed_id)
            continue
        if sector not in result:
            result.append(sector)
    return result


# Free-text sector names a profile may use, mapped to the fixed vocabulary that seeds carry.
SECTOR_ALIASES: dict[str, Sector] = {
    "tech": Sector.TECHNOLOGY,
    "software": Sector.TECHNOLOGY,
    "saas": Sector.TECHNOLOGY,
    "it": Sector.TECHNOLOGY,
    "information technology": Sector.TECHNOLOGY,
    "internet": Sector.TECHNOLOGY,
    "healthcare": Sector.PHARMA_HEALTH,
    "health care": Sector.PHARMA_HEALTH,
    "health": Sector.PHARMA_HEALTH,
    "nursing": Sector.PHARMA_HEALTH,
    "medical": Sector.PHARMA_HEALTH,
    "hospital": Sector.PHARMA_HEALTH,
    "hospitals": Sector.PHARMA_HEALTH,
    "pharma": Sector.PHARMA_HEALTH,
    "pharmaceutical": Sector.PHARMA_HEALTH,
    "pharmaceuticals": Sector.PHARMA_HEALTH,
    "life sciences": Sector.PHARMA_HEALTH,
    "biotech": Sector.PHARMA_HEALTH,
    "finance": Sector.FINANCE_INSURANCE,
    "banking": Sector.FINANCE_INSURANCE,
    "insurance": Sector.FINANCE_INSURANCE,
    "financial services": Sector.FINANCE_INSURANCE,
    "public sector": Sector.PUBLIC_EDUCATION,
    "government": Sector.PUBLIC_EDUCATION,
    "education": Sector.PUBLIC_EDUCATION,
    "retail": Sector.CONSUMER_RETAIL,
    "ecommerce": Sector.CONSUMER_RETAIL,
    "e-commerce": Sector.CONSUMER_RETAIL,
    "logistics": Sector.LOGISTICS_TRANSPORT,
    "transport": Sector.LOGISTICS_TRANSPORT,
    "energy": Sector.ENERGY_UTILITIES,
    "utilities": Sector.ENERGY_UTILITIES,
    "telecom": Sector.TELECOM_MEDIA,
    "telecommunications": Sector.TELECOM_MEDIA,
    "media": Sector.TELECOM_MEDIA,
    "manufacturing": Sector.MANUFACTURING_INDUSTRIAL,
    "construction": Sector.CONSTRUCTION_ENGINEERING,
    "consulting": Sector.PROFESSIONAL_SERVICES,
    "marketing": Sector.MARKETING_ADVERTISING,
    "advertising": Sector.MARKETING_ADVERTISING,
    "aerospace": Sector.AEROSPACE_DEFENSE,
    "defense": Sector.AEROSPACE_DEFENSE,
}
_LABEL_TO_SECTOR = {label.casefold(): sector for sector, label in SECTOR_LABELS.items()}


def profile_sector_values(value: object) -> set[str]:
    """A profile's ``criteria.sectors`` in the seed vocabulary.

    Profile sectors are free text. A value equal to a sector value, its label ("Pharma and health") or an alias
    ("healthcare", "software") becomes that sector; any other value is kept as written (it then matches no
    bundled pack seed, as before).
    """
    if not isinstance(value, (list, tuple, set)):
        return set()
    result: set[str] = set()
    for raw in value:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        key = text.casefold()
        sector = SECTOR_ALIASES.get(key) or _LABEL_TO_SECTOR.get(key)
        if sector is None:
            try:
                sector = Sector(key.replace(" ", "_").replace("-", "_"))
            except ValueError:
                result.add(text)
                continue
        result.add(sector.value)
    return result
