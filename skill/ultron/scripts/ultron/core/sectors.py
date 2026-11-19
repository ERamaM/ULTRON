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
