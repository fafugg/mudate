"""Pydantic models for API request/response validation.

Provides type-safe schemas for all API endpoints, enabling:
- Runtime validation of request/response data
- Auto-generated OpenAPI documentation
- IDE autocomplete for API consumers
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field


# ── Domain Types ──────────────────────────────────────────────────────────────


class HouseDict(TypedDict, total=False):
    """Canonical house data structure.

    This is the single source of truth for all house fields.
    Created by scrapers, merged by persistence, read by the frontend.
    """
    # Identity
    internal_id: str
    search_engine_id: Optional[str]
    search_engine: str
    session_id: str
    # Property specs
    type: Optional[str]
    ambientes: Optional[int]
    dormitorios: Optional[int]
    banos: Optional[int]
    toilettes: Optional[int]
    covered_m2: Optional[float]
    total_m2: Optional[float]
    floor: Optional[str]
    parking: Optional[bool]
    amenities: List[str]
    orientation: Optional[str]
    age_years: Optional[int]
    condition: Optional[str]
    # Pricing
    price: Optional[float]
    currency: str
    price_per_m2: Optional[float]
    expenses: Optional[float]
    expenses_currency: Optional[str]
    # Location
    address: Optional[str]
    manual_address: Optional[str]
    lat: Optional[float]
    lng: Optional[float]
    geocode_failed: bool
    # Content
    description: Optional[str]
    images: List[str]
    url: str
    # Publisher
    real_estate: Optional[str]
    real_estate_phone: Optional[str]
    published_at: Optional[str]
    # Status & tracking
    status: str  # "active" | "removed"
    removed_at: Optional[str]
    review: Optional[str]  # "" | "en_duda" | "interesante" | "contactar" | "descartada" | "duplicado"
    notes: Optional[str]
    # Timestamps & history
    created_at: str
    last_updated: str
    previous_prices: List[Dict[str, Any]]


# ── Request Models ────────────────────────────────────────────────────────────


class SearchSource(BaseModel):
    """A single search URL with its provider engine."""
    engine: str = Field(..., description="Search engine: zonaprop, argenprop, mercadolibre, or remax")
    filter: str = Field(..., description="URL path filter from the search engine")


class CreateSessionRequest(BaseModel):
    """Request body for creating a new search session."""
    search_sources: List[SearchSource] = Field(..., description="List of search URLs to scrape")
    label: Optional[str] = Field(None, description="Optional custom label for the session")


class UpdateSessionRequest(BaseModel):
    """Request body for updating a session's filters or label."""
    search_sources: Optional[List[SearchSource]] = Field(None, description="Replace all search URLs")
    label: Optional[str] = Field(None, description="New label")


class UpdateHouseRequest(BaseModel):
    """Request body for updating a house's review, notes, or manual address."""
    review: Optional[str] = Field(None, description="Review status: '', en_duda, interesante, contactar, descartada, duplicado")
    notes: Optional[str] = Field(None, description="Free-text notes about the property")
    manual_address: Optional[str] = Field(None, description="Manually corrected address for geocoding")


# ── Response Models ───────────────────────────────────────────────────────────


class SessionSummary(BaseModel):
    """Summary of a session in the user's session list."""
    id: str
    created_at: str
    last_executed: Optional[str] = None
    search_sources: List[Dict[str, str]] = []
    label: str
    house_ids: List[str] = []
    active_count: int = 0


class UserResponse(BaseModel):
    """Response for GET /api/users/{username}."""
    username: str
    is_new: bool
    sessions: List[SessionSummary] = []


class SessionResponse(BaseModel):
    """Response for GET /api/users/{username}/sessions/{id}."""
    id: str
    created_at: str
    last_executed: Optional[str] = None
    search_sources: List[Dict[str, str]] = []
    label: str
    house_ids: List[str] = []
    houses: List[Dict[str, Any]] = []


class RunResponse(BaseModel):
    """Response for GET /api/runs/{id}."""
    id: str
    session_id: Optional[str] = None
    status: str
    progress: int = 0
    total: int = 0
    message: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    errors: List[str] = []
    triggered_by: str = "manual"
    same_engine_dedup: Optional[Dict[str, Any]] = None


class RunCreatedResponse(BaseModel):
    """Response for POST /api/users/{username}/sessions/{id}/run."""
    run_id: str
    already_running: bool = False


class GeocodeResponse(BaseModel):
    """Response for POST /api/users/{username}/sessions/{id}/geocode."""
    run_id: Optional[str] = None
    already_done: bool = False
    already_running: bool = False


class SchedulerResponse(BaseModel):
    """Response for GET /api/scheduler."""
    running: bool
    next_run: Optional[str] = None


class OkResponse(BaseModel):
    """Generic OK response for mutations."""
    ok: bool = True


class DupHouseInfo(BaseModel):
    """A house within a duplicate group."""
    internal_id: str
    search_engine: str
    address: Optional[str] = None
    price: Optional[float] = None
    currency: str = "USD"
    total_m2: Optional[float] = None
    covered_m2: Optional[float] = None
    review: Optional[str] = None
    notes: Optional[str] = None


class DupGroupResponse(BaseModel):
    """A group of duplicate houses."""
    keep_id: str
    keep_engine: str
    houses: List[DupHouseInfo] = []


class DedupPreviewResponse(BaseModel):
    """Response for deduplicate/preview."""
    groups: List[DupGroupResponse] = []


class DedupApplyRequest(BaseModel):
    """Request body for deduplicate/apply — optionally select which groups to process."""
    selected_groups: Optional[List[int]] = Field(None, description="Indices of groups to deduplicate (default: all)")


class DedupApplyResponse(BaseModel):
    """Response for deduplicate/apply."""
    removed_count: int = 0


class SameEngineDedupHouseInfo(BaseModel):
    """A house within a same-engine dedup group."""
    internal_id: str
    search_engine: str
    search_engine_id: str = ""
    address: str = ""
    price: Optional[float] = None
    currency: str = "USD"
    total_m2: Optional[float] = None
    covered_m2: Optional[float] = None
    review: Optional[str] = None
    url: str = ""


class SameEngineDedupGroup(BaseModel):
    """A group of same-engine duplicate houses (old removed + new active)."""
    old_id: str
    new_id: str
    engine: str
    old_house: SameEngineDedupHouseInfo
    new_house: SameEngineDedupHouseInfo


class SameEngineDedupPreviewResponse(BaseModel):
    """Response for same-engine-dedup/preview."""
    groups: List[SameEngineDedupGroup] = []


class SameEngineDedupApplyRequest(BaseModel):
    """Request body for same-engine-dedup/apply."""
    selected_groups: Optional[List[int]] = Field(
        None,
        description="Indices of groups to merge (default: all)",
    )


class SameEngineDedupApplyResponse(BaseModel):
    """Response for same-engine-dedup/apply."""
    merged_count: int = 0


class ErrorResponse(BaseModel):
    """Error response."""
    detail: str
