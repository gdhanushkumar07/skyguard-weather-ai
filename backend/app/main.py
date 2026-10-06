"""
ATHER Backend Core API
======================
FastAPI server orchestrating weather station management, anomaly detection,
Vane meteorological grid rendering data, and multi-protocol ingestion.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any

from .stations.service import station_service
from .weather.grid_service import grid_service
from .weather.open_meteo import open_meteo_service
from .ingestion.adapter import IngestionAdapter
from .anomaly.detector import detector
from .simulation import service as simulation_service
from .incidents import service as incident_service


def _publish_incident(inc):
    """Pushes an operator's incident transition to every connected dashboard."""
    from .pipeline.runtime import get_runtime
    from .pipeline.processor import incident_event_payload
    rt = get_runtime()
    if rt is not None and inc:
        rt.broker.publish("INCIDENT_UPDATED", incident_event_payload(inc))
    return inc

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the continuous pipeline (sources -> stream -> engine -> store ->
    events) with the API, and stops it cleanly on shutdown."""
    from .pipeline.runtime import PipelineRuntime, set_runtime, _flag
    rt = None
    if _flag("ATHER_PIPELINE_ENABLED"):
        rt = PipelineRuntime(detector=detector, station_service=station_service, incident_service=incident_service)
        set_runtime(rt)
        await rt.start()
    try:
        yield
    finally:
        if rt:
            await rt.stop()
            set_runtime(None)


app = FastAPI(
    title="SkyGuard AI Core API",
    description="Intelligent Weather-Station Monitoring and Anomaly-Detection Platform",
    version="2.0.0",
    lifespan=lifespan,
)

# Enable CORS for local development and demo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/api/health", methods=["GET", "HEAD"])
def health_check():
    return {
        "status": "healthy",
        "service": "SkyGuard AI Platform Backend",
        "stations_loaded": len(station_service._stations),
        "version": "1.0.0"
    }

@app.get("/api/stations")
def get_stations_geojson(
    min_lon: Optional[float] = Query(None, description="Bounding box minimum longitude"),
    min_lat: Optional[float] = Query(None, description="Bounding box minimum latitude"),
    max_lon: Optional[float] = Query(None, description="Bounding box maximum longitude"),
    max_lat: Optional[float] = Query(None, description="Bounding box maximum latitude"),
    limit: Optional[int] = Query(None, description="Max stations to return"),
    status: Optional[str] = Query(None, description="Filter by status: NORMAL, WARNING, ANOMALY, OFFLINE")
):
    """
    Returns weather stations in GeoJSON format optimized for MapLibre GPU clustering.
    Supports viewport bounding box spatial filtering to ensure fast 60fps interaction.
    """
    return station_service.get_geojson(
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        limit=limit,
        status=status
    )

@app.get("/api/stations/search")
def search_stations(q: str = Query(..., min_length=1), limit: int = 10):
    """Search stations by name, town, country, or station ID."""
    return station_service.search(q, limit=limit)

@app.get("/api/stations/{station_id}")
def get_station_details(station_id: str):
    """Returns comprehensive metadata and current telemetry for a specific station."""
    stn = station_service.get_station(station_id)
    if not stn:
        raise HTTPException(status_code=404, detail=f"Station '{station_id}' not found")
    return stn

@app.get("/api/stations/{station_id}/observations")
def get_station_observations(station_id: str, hours: int = Query(24, ge=1, le=168)):
    """Returns time-series observation trends for charts, sparklines, and diurnal analysis."""
    stn = station_service.get_station(station_id)
    if not stn:
        raise HTTPException(status_code=404, detail=f"Station '{station_id}' not found")
    from .pipeline.runtime import get_runtime
    rt = get_runtime()
    if rt is None:
        return {"station_id": station_id, "hours": hours,
                "series": station_service.get_observations_history(station_id, hours=hours)}
    # Real persisted telemetry only. Stations without a live feed get an empty
    # series and an explanation — never a synthesized curve labelled as data.
    import time as _time
    rows = rt.store.history(station_id, _time.time() - hours * 3600, max_points=300)
    series = [{
        "timestamp": int(r["observed_at"]),
        "timeLabel": _time.strftime("%H:%M", _time.gmtime(r["observed_at"])),
        "temperature": r.get("temperature"), "pressure": r.get("pressure"),
        "humidity": r.get("humidity"), "windSpeed": r.get("wind_speed"),
        "source": r.get("source"),
    } for r in rows]
    return {
        "station_id": station_id, "hours": hours, "series": series,
        "note": None if series else "No telemetry has been recorded for this station in the selected window.",
    }

@app.get("/api/weather/metadata")
def get_weather_metadata():
    """Returns grid dimensions, bounding box, and variable definitions for Vane rendering."""
    return grid_service.get_grid_metadata()

@app.get("/api/weather/grid")
def get_weather_grid(variable: str = Query("temperature", enum=["temperature", "wind", "pressure_msl"])):
    """
    Provides gridded meteorological scalar/vector data matrix for Vane MapLibre WebGL layers.
    """
    if variable == "temperature":
        return grid_service.generate_temperature_field()
    elif variable == "wind":
        return grid_service.generate_wind_field()
    else:
        raise HTTPException(status_code=400, detail=f"Variable '{variable}' not supported.")

@app.get("/api/weather/current")
def get_current_weather(
    lat: float = Query(..., description="Latitude of location"),
    lon: float = Query(..., description="Longitude of location")
):
    """
    Fetches real-time localized current weather from Open-Meteo for the specified coordinate.
    """
    from .weather.open_meteo import UpstreamError, UpstreamUnavailable
    try:
        return open_meteo_service.get_current_weather(lat, lon)
    except UpstreamUnavailable as e:
        raise HTTPException(status_code=503, detail=f"Open-Meteo reference temporarily unavailable: {e}")
    except UpstreamError as e:
        raise HTTPException(status_code=503 if e.kind in ("rate_limited", "server_error", "network") else 502,
                            detail=f"Open-Meteo reference fetch failed ({e.kind}): {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Open-Meteo weather fetch error: {str(e)}")

@app.get("/api/anomalies")
def get_anomalies():
    """Returns active anomaly metrics and breakdown for the ATHER anomaly monitoring panel."""
    return station_service.get_anomalies_summary()

@app.get("/api/stations/{station_id}/anomaly")
def get_station_anomaly(station_id: str):
    """Returns canonical §16 anomaly detection results, conformal confidence, and root cause diagnosis."""
    anomaly_data = station_service.get_station_anomaly(station_id)
    if not anomaly_data:
        raise HTTPException(status_code=404, detail=f"Station '{station_id}' not found")
    return anomaly_data

@app.get("/api/stations/{station_id}/debug")
def get_station_debug_trace(station_id: str):
    """
    Diagnostic & validation endpoint (§22) tracing raw value → normalized reading → 
    5 layer inputs & outputs → conformal fusion → root cause diagnosis.
    """
    stn = station_service.get_station(station_id)
    if not stn:
        raise HTTPException(status_code=404, detail=f"Station '{station_id}' not found")

    from schema import station_dict_to_reading
    reading = station_dict_to_reading(stn)
    alert = detector.get_station_alert(station_id)
    if not alert:
        detector.evaluate_station(stn)
        alert = detector.get_station_alert(station_id)

    return {
        "station_id": station_id,
        "station_metadata": {
            "name": stn.get("name"),
            "town": stn.get("town"),
            "coordinates": [stn.get("latitude"), stn.get("longitude")],
            "elevation": stn.get("elevation")
        },
        "raw_json_values": {
            "temperature": stn.get("temperature"),
            "pressure": stn.get("pressure"),
            "humidity": stn.get("humidity"),
            "windSpeed": stn.get("windSpeed"),
            "windDirection": stn.get("windDirection")
        },
        "normalized_reading": reading.to_dict(),
        "data_quality": reading.data_quality,
        "layer_scores": alert.layer_scores if alert else {},
        "layer_details": alert.layer_details if alert else {},
        "fusion": alert.layer_details.get("fusion") if alert else {},
        "diagnosis": {
            "status": alert.status if alert else "UNKNOWN",
            "is_anomaly": alert.is_anomaly if alert else False,
            "root_cause": alert.root_cause.value if alert else "UNKNOWN",
            "diagnosis_confidence": alert.diagnosis_confidence.value if alert else "UNKNOWN",
            "primary_signal": alert.primary_signal if alert else "",
            "evidence": alert.reasons if alert else [],
            "alternatives": alert.alternative_causes if alert else [],
            "operator_action": alert.operator_action if alert else ""
        },
        "explanation": alert.explanation if alert else "",
        "canonical_result": alert.canonical_result if alert else None
    }

@app.post("/api/ingest")
def ingest_observation(payload: Dict[str, Any]):
    """
    Multi-protocol ingestion endpoint accepting WeeWX, WOW-BE, or native ATHER packets.
    Instantly runs through the Anomaly Detection engine and updates station state.
    """
    from .pipeline.runtime import get_runtime
    try:
        stn_id, normalized_data = IngestionAdapter.parse_payload(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    rt = get_runtime()
    if rt is None:
        # Pipeline not running (e.g. ATHER_PIPELINE_ENABLED=0): legacy path.
        updated_stn = station_service.ingest_observation(stn_id, normalized_data)
        return {"status": "success", "station_id": stn_id, "station": updated_stn}
    from .pipeline.api import legacy_payload_to_observation
    obs = legacy_payload_to_observation(stn_id, normalized_data, payload)
    result = rt.process_now([obs])
    if result["rejected"]:
        raise HTTPException(status_code=400, detail=result["rejected"][0])
    outcome = result["outcomes"][0]
    return {
        "status": outcome["status"],
        "station_id": stn_id,
        "station": station_service._stations.get(stn_id),
        "detection": outcome.get("detection"),
    }


# ─────────────────────────────────────────────────────────────────
# ATHER TEST LAB — Isolated Simulation Engine (Phase 5-12)
# ─────────────────────────────────────────────────────────────────

@app.get("/api/simulation/scenarios")
def get_simulation_scenarios():
    """Lists the predefined ATHER Test Lab fault-injection scenarios."""
    return {"scenarios": simulation_service.list_scenarios()}

@app.post("/api/simulation/run")
def run_simulation(payload: Dict[str, Any]):
    """
    Runs a predefined scenario through a fresh, isolated AnomalyDetector
    instance — the SAME diagnostic engine production uses. Never touches
    real station state, the production detector singleton, or real
    incidents. See app/simulation/service.py for the isolation guarantee.
    """
    scenario_id = payload.get("scenario_id")
    if not scenario_id:
        raise HTTPException(status_code=400, detail="scenario_id is required")
    base_station_id = payload.get("base_station_id")
    try:
        return simulation_service.run_simulation(scenario_id, base_station_id=base_station_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─────────────────────────────────────────────────────────────────
# ATHER INCIDENT WORKFLOW — persistent, production-grade (SQLite-backed)
#
# DETECT -> VALIDATE -> CORRELATE -> PERSIST -> EXPLAIN -> DIAGNOSE ->
# RECOMMEND -> ACKNOWLEDGE -> INVESTIGATE -> ESCALATE -> RESOLVE
#
# Incidents are created/updated automatically by the backend pipeline
# (app/stations/service.py._sync_incident, called from live ingestion and
# on-demand evaluation) — these endpoints only render and request state
# changes on already-persisted incidents (Phase 44: backend is the
# authoritative source of truth for status/severity/confidence/evidence).
# ─────────────────────────────────────────────────────────────────

@app.get("/api/incidents")
def list_incidents(status: Optional[str] = None, station_id: Optional[str] = None, source: Optional[str] = "LIVE_AWS"):
    """Lists persisted incidents. `source` defaults to LIVE_AWS only — a
    Test Lab simulation incident is never returned here unless the caller
    explicitly asks for source=TEST_SIMULATION (Phase 36)."""
    return {"incidents": incident_service.list_all(status=status, source=source, station_id=station_id)}

@app.get("/api/incidents/active-counts")
def get_active_incident_counts(source: Optional[str] = "LIVE_AWS"):
    """Real counts for the operational incident counter (Phase 24/43) —
    derived from persisted incidents, not recomputed independently."""
    return incident_service.get_active_counts(source=source)

@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    incident = incident_service.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    return incident

@app.get("/api/incidents/{incident_id}/work-orders")
def list_work_orders(incident_id: str):
    from app.incidents import work_orders
    return {"work_orders": work_orders.list_for_incident(incident_id)}

@app.post("/api/incidents/{incident_id}/work-orders", status_code=201)
def create_work_order(incident_id: str, payload: Dict[str, Any]):
    from app.incidents import work_orders
    try:
        wo = work_orders.create(incident_id, payload.get("issue", ""), payload.get("priority", ""),
                                payload.get("team", ""), actor=payload.get("actor", "operator"), notes=payload.get("notes"))
    except incident_service.IncidentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _publish_incident(incident_service.get(incident_id))
    return wo

@app.post("/api/work-orders/{work_order_id}/status")
def advance_work_order(work_order_id: str, payload: Dict[str, Any]):
    from app.incidents import work_orders
    try:
        wo = work_orders.advance(work_order_id, payload.get("status", ""), actor=payload.get("actor", "operator"),
                                 assignee=payload.get("assignee"), note=payload.get("note"))
    except work_orders.WorkOrderNotFoundError:
        raise HTTPException(status_code=404, detail=f"Work order '{work_order_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _publish_incident(incident_service.get(wo["incident_id"]))
    return wo

@app.post("/api/incidents/{incident_id}/acknowledge")
def acknowledge_incident(incident_id: str, payload: Optional[Dict[str, Any]] = None):
    actor = (payload or {}).get("actor", "operator")
    try:
        return _publish_incident(incident_service.acknowledge(incident_id, actor=actor))
    except incident_service.IncidentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

@app.post("/api/incidents/{incident_id}/investigate")
def investigate_incident(incident_id: str, payload: Optional[Dict[str, Any]] = None):
    actor = (payload or {}).get("actor", "operator")
    try:
        return _publish_incident(incident_service.investigate(incident_id, actor=actor))
    except incident_service.IncidentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

@app.post("/api/incidents/{incident_id}/escalate")
def escalate_incident(incident_id: str, payload: Optional[Dict[str, Any]] = None):
    actor = (payload or {}).get("actor", "operator")
    try:
        return _publish_incident(incident_service.escalate(incident_id, actor=actor))
    except incident_service.IncidentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

@app.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, payload: Dict[str, Any]):
    actor = payload.get("actor", "operator")
    notes = payload.get("resolution_notes", "")
    resolution_type = payload.get("resolution_type", "")
    try:
        return _publish_incident(incident_service.resolve(incident_id, actor, notes, resolution_type))
    except incident_service.IncidentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/incidents/{incident_id}/dismiss")
def dismiss_incident(incident_id: str, payload: Dict[str, Any]):
    actor = payload.get("actor", "operator")
    reason = payload.get("dismissal_reason", "")
    try:
        return _publish_incident(incident_service.dismiss(incident_id, actor, reason))
    except incident_service.IncidentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    except incident_service.InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/incidents/{incident_id}/escalation-preview")
def get_escalation_preview(incident_id: str):
    """Builds a preview of what an escalation alert WOULD contain. This never
    sends a real email/SMS/notification — see app/incidents/service.py."""
    preview = incident_service.build_escalation_preview(incident_id)
    if not preview:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")
    return preview

@app.get("/api/stations/{station_id}/incidents")
def get_station_incidents(station_id: str, source: Optional[str] = "LIVE_AWS"):
    """Incident history for a single station (Phase 21/27: Station
    Intelligence -> incident history). Returns persisted incidents only —
    never fabricates a record for a station that has never been actionable."""
    return {"incidents": incident_service.list_all(source=source, station_id=station_id)}


# ─────────────────────────────────────────────────────────────────
# ATHER REAL-TIME PIPELINE — live stream, state, sources, lab, replay
# (see app/pipeline/api.py)
# ─────────────────────────────────────────────────────────────────
from .pipeline.api import router as realtime_router  # noqa: E402

app.include_router(realtime_router)
